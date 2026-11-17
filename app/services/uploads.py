"""Разбор ТЗ, присланного боту: PDF, Word, текст или фото страниц.

Файл превращается в «личную» закупку (source="upload"), видимую только владельцу:
к ней работают все функции сайта — разбор, документы заявки, вопросы, воронка.
Текстовые PDF и Word читаются как текст (дёшево); сканы и фото отправляются
модели как документ или изображения (дороже, поэтому есть лимит страниц).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.ai.analysis import SCHEMA_HINT, SYSTEM, extract_text
from app.ai.scoring import profile_text
from app.db import Analysis, Lot, Match, User, utcnow
from app.services import billing, core

log = logging.getLogger(__name__)

MAX_FILE = 20 * 1024 * 1024          # лимит Telegram на скачивание ботом
MAX_TEXT = 40_000                    # символов текста в модель
MAX_SCAN_PAGES = 20                  # страниц скана в модель
MIN_TEXT = 400                       # меньше — считаем PDF сканом
TEXT_EXT = (".pdf", ".docx", ".txt", ".rtf", ".md", ".csv")
IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}

LOT_HINT = """,
  "lot": {
    "title": "краткий предмет закупки, как в объявлении",
    "customer": "заказчик или ''",
    "amount": число в тенге или null,
    "deadline": "дата и время окончания приёма заявок в формате ГГГГ-ММ-ДДTЧЧ:ММ или null",
    "region": "регион или ''",
    "method": "способ закупки или ''",
    "number": "номер объявления или ''"
  }
}"""


class UploadError(Exception):
    """Понятная пользователю причина, почему файл не разобран."""


@dataclass
class Prepared:
    names: list[str] = field(default_factory=list)
    text: str = ""
    blocks: list[dict] = field(default_factory=list)  # pdf/изображения для модели
    pages: int = 0


def _scan_pdf(content: bytes) -> tuple[bytes, int]:
    """Первые MAX_SCAN_PAGES страниц PDF (скан) — чтобы не сжечь токены на 300-страничном файле."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(content))
    total = len(reader.pages)
    if total <= MAX_SCAN_PAGES:
        return content, total
    w = PdfWriter()
    for p in reader.pages[:MAX_SCAN_PAGES]:
        w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), MAX_SCAN_PAGES


def prepare(files: list[tuple[str, bytes, str]]) -> Prepared:
    """files: [(имя, содержимое, mime)]. Бросает UploadError с понятным текстом."""
    out = Prepared()
    texts = []
    for name, content, mime in files:
        lower = name.lower()
        if len(content) > MAX_FILE:
            raise UploadError(f"Файл «{name}» больше 20 МБ — Telegram не даёт боту скачать его. Пришлите его частями или ссылкой на сайте.")
        if lower.endswith(".doc") and not lower.endswith(".docx"):
            raise UploadError(f"«{name}» — старый формат Word (.doc). Сохраните его как .docx или PDF и пришлите снова.")
        if mime in IMAGE_MIME or lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            if sum(1 for b in out.blocks if b["type"] == "image") >= MAX_SCAN_PAGES:
                continue
            out.blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime if mime in IMAGE_MIME else "image/jpeg",
                                                           "data": base64.b64encode(content).decode()}})
            out.names.append(name)
            out.pages += 1
            continue
        if not lower.endswith(TEXT_EXT) and content[:4] != b"%PDF" and content[:2] != b"PK":
            raise UploadError(f"Не умею читать «{name}». Пришлите ТЗ в PDF, Word (.docx), текстом или фотографиями страниц.")
        text = extract_text(content, name)
        is_pdf = lower.endswith(".pdf") or content[:4] == b"%PDF"
        if is_pdf and len(text.strip()) < MIN_TEXT:
            pdf, pages = _scan_pdf(content)
            out.blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": base64.b64encode(pdf).decode()}})
            out.pages += pages
        elif not text.strip():
            raise UploadError(f"В файле «{name}» не нашлось текста.")
        else:
            texts.append(f"=== {name} ===\n{text}")
        out.names.append(name)
    out.text = "\n\n".join(texts)[:MAX_TEXT]
    if not out.text and not out.blocks:
        raise UploadError("В присланных файлах нет текста ТЗ.")
    return out


async def analyze_prepared(user: User, prep: Prepared) -> dict:
    content: list[dict] = list(prep.blocks)
    content.append({"type": "text", "text": (
        f"Компания:\n{profile_text(user)}\n\n"
        "Пользователь прислал документацию закупки (ТЗ, техническая спецификация, конкурсная документация)"
        f"{' — текст ниже' if prep.text else ' — во вложении'}.\n\n"
        f"{('Документация:' + chr(10) + prep.text) if prep.text else ''}\n\n"
        "Сначала определи сведения о закупке, затем разбери её для компании.\n"
        f"Верни JSON строго такого вида:\n{SCHEMA_HINT.rstrip().rstrip('}')}{LOT_HINT}"
    )})
    resp = await ai.client().messages.create(
        model=config.AI_SMART_MODEL, max_tokens=4000, system=SYSTEM, messages=[{"role": "user", "content": content}])
    ai.log_usage(config.AI_SMART_MODEL, resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        data = ai.extract_json(text)
    except ValueError:
        raise UploadError("Не получилось разобрать документ. Попробуйте ещё раз или пришлите файл в другом формате.")
    if data.get("verdict") not in {"go", "consider", "skip"}:
        data["verdict"] = "consider"
    data["docs_used"] = prep.names
    return data


def _parse_deadline(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T")[:16])
    except ValueError:
        return None
    return dt.replace(tzinfo=config.TZ) if dt.tzinfo is None else dt


def _amount(value) -> float | None:
    try:
        v = float(str(value).replace(" ", "").replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None
    return v if v and v > 0 else None


async def process(s: AsyncSession, user: User, files: list[tuple[str, bytes, str]]) -> tuple[Lot, dict]:
    """Полный цикл: проверки → разбор → личная закупка + разбор + «Интересно» в воронке."""
    if not ai.available():
        raise UploadError("Разбор ТЗ заработает после подключения ИИ-модуля.")
    try:
        await billing.check(s, user, "analyses")
    except billing.LimitError as e:
        raise UploadError(str(e))
    prep = prepare(files)
    data = await analyze_prepared(user, prep)
    info = data.pop("lot", None) or {}
    title = str(info.get("title") or "").strip() or (prep.names[0].rsplit(".", 1)[0] if prep.names else "Закупка из ТЗ")
    lot = Lot(
        source="upload", ext_id=str(info.get("number") or "").strip()[:60] or secrets.token_hex(6),
        title=title[:500], description="; ".join(str(x) for x in (data.get("scope") or [])[:5])[:2000],
        customer=str(info.get("customer") or "")[:500], amount=_amount(info.get("amount")),
        region=str(info.get("region") or "")[:200], method=str(info.get("method") or "")[:200],
        deadline=_parse_deadline(info.get("deadline")), published_at=utcnow(), url="", documents=[],
        raw={"owner": user.id, "files": prep.names, "text": prep.text, "pages": prep.pages}, is_demo=False,
    )
    # ext_id уникален в паре с source: номер объявления может повториться у разных пользователей
    lot.ext_id = f"{user.id}:{lot.ext_id}:{secrets.token_hex(3)}"
    s.add(lot)
    await s.flush()
    score = int(data["score"]) if isinstance(data.get("score"), (int, float)) else 60
    s.add(Match(user_id=user.id, lot_id=lot.id, score=max(0, min(100, score)), reason="Разбор присланного ТЗ", by_ai=True))
    s.add(Analysis(user_id=user.id, lot_id=lot.id, data=data, verdict=data["verdict"], model=config.AI_SMART_MODEL))
    await s.commit()
    await core.set_stage(s, user, lot.id, "saved")
    return lot, data


def owner_of(lot: Lot) -> int | None:
    return (lot.raw or {}).get("owner") if lot.source == "upload" else None
