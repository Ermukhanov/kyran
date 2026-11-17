"""ИИ-разбор тендера под компанию: вердикт, требования, документы, риски.

Модель читает карточку лота и, если есть, тексты документации (PDF/DOCX).
Каждый пункт по возможности ссылается на источник («Техспецификация, п. 3.2»),
чтобы клиент мог проверить и ИИ не выдумывал.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone

from app import config
from app.ai import client
from app.ai.scoring import profile_text
from app.db import Lot, User, aware

log = logging.getLogger(__name__)
MAX_DOC_CHARS = 60_000
MAX_DOCS = 5

SYSTEM = (
    "Ты — опытный тендерный специалист в Казахстане (закон «О государственных закупках»). "
    "Разбираешь закупку для конкретной компании-поставщика и помогаешь решить: участвовать или нет. "
    "Опирайся только на данные закупки и документацию. Если чего-то в данных нет — так и пиши "
    "(«в документации не указано»), не придумывай. Для требований и рисков указывай источник, если он есть. "
    "Пиши по-русски, коротко и конкретно, на «вы». Ответ — только JSON."
)

SCHEMA_HINT = """{
  "verdict": "go | consider | skip",
  "score": 0-100,
  "summary": "2–3 предложения: что закупают и подходит ли вам",
  "scope": ["объём работ/поставки — пунктами"],
  "requirements": [{"text": "требование к поставщику", "source": "где указано или ''"}],
  "documents": ["документ, который нужно подготовить"],
  "risks": [{"text": "риск или подводный камень", "level": "high | medium | low"}],
  "questions": ["вопрос, который стоит задать заказчику"],
  "next_steps": ["что сделать сейчас, по шагам"]
}"""


def extract_text(content: bytes, name: str) -> str:
    """Текст из PDF/DOCX/TXT. Ошибки не роняют разбор."""
    lower = name.lower()
    try:
        if lower.endswith(".pdf") or content[:4] == b"%PDF":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:60])
        if lower.endswith(".docx") or content[:2] == b"PK":
            import docx

            d = docx.Document(io.BytesIO(content))
            rows = [p.text for p in d.paragraphs]
            for table in d.tables:
                for row in table.rows:
                    rows.append(" | ".join(c.text for c in row.cells))
            return "\n".join(rows)
        return content.decode("utf-8", errors="replace")
    except Exception:
        log.exception("Не удалось прочитать документ %s", name)
        return ""


async def load_documents(lot: Lot) -> list[tuple[str, str]]:
    """Скачивает документы лота → [(имя, текст)]. Для goszakup нужен токен."""
    docs = (lot.documents or [])[:MAX_DOCS]
    if not docs:
        return []
    import aiohttp

    headers = {"Authorization": f"Bearer {config.GOSZAKUP_TOKEN}"} if lot.source == "goszakup" and config.GOSZAKUP_TOKEN else {}
    out: list[tuple[str, str]] = []
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as s:
        for d in docs:
            url, name = d.get("url", ""), d.get("name", "Документ")
            if not url.startswith("http"):
                continue
            try:
                async with s.get(url) as resp:
                    if resp.status == 200:
                        text = extract_text(await resp.read(), name)
                        if text.strip():
                            out.append((name, text))
            except Exception:
                log.exception("Не скачался документ %s", url)
    return out


def lot_card(lot: Lot) -> dict:
    deadline = aware(lot.deadline)
    days_left = (deadline - datetime.now(timezone.utc)).days if deadline else None
    return {
        "Название": lot.title, "Описание": lot.description, "Заказчик": lot.customer,
        "Сумма, ₸": lot.amount, "Регион": lot.region, "Способ закупки": lot.method,
        "Дедлайн подачи": deadline.astimezone(config.TZ).strftime("%d.%m.%Y %H:%M") if deadline else None,
        "Дней до дедлайна": days_left,
    }


async def analyze(user: User, lot: Lot) -> dict:
    """Возвращает разбор (dict по SCHEMA_HINT). Бросает RuntimeError, если ИИ не подключён."""
    if not client.available():
        raise RuntimeError("ИИ-разбор заработает после подключения ключа ANTHROPIC_API_KEY")

    docs = await load_documents(lot)
    budget = MAX_DOC_CHARS
    doc_blocks = []
    for name, text in docs:
        chunk = text[:budget]
        budget -= len(chunk)
        doc_blocks.append(f"=== {name} ===\n{chunk}")
        if budget <= 0:
            break
    docs_part = "\n\n".join(doc_blocks) if doc_blocks else "Документация не загружена — разбирай по карточке закупки и прямо скажи, чего не хватает."

    prompt = (
        f"Компания:\n{profile_text(user)}\n\n"
        f"Закупка:\n{json.dumps(lot_card(lot), ensure_ascii=False, indent=1)}\n\n"
        f"Документация:\n{docs_part}\n\n"
        f"Верни JSON строго такого вида:\n{SCHEMA_HINT}"
    )
    data = await client.ask_json(config.AI_SMART_MODEL, SYSTEM, prompt, max_tokens=4000)
    if data.get("verdict") not in {"go", "consider", "skip"}:
        data["verdict"] = "consider"
    data["docs_used"] = [name for name, _ in docs]
    return data
