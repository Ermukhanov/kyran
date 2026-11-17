"""Документы заявки в Word: сопроводительное письмо, опись, запрос разъяснений,
техническое предложение, справка об опыте.

ИИ используется только там, где без него никак (техническое предложение; запрос
разъяснений — если ещё нет разбора). Остальное собирается из данных без затрат.
Пустые реквизиты остаются строками «________», чтобы их было легко заполнить.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import datetime

from docx import Document as Docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.ai.analysis import MAX_DOC_CHARS, load_documents, lot_card
from app.ai.scoring import profile_text
from app.db import Analysis, Award, Document, GeneratedDoc, Lot, User, aware, utcnow
from app.services import documents as docsvc

BLANK = "________________"


@dataclass(frozen=True)
class Kind:
    code: str
    title: str
    description: str
    ai: bool  # требует ли ИИ (расходует лимит)


KINDS: dict[str, Kind] = {k.code: k for k in [
    Kind("tech", "Техническое предложение", "Объём, порядок выполнения, сроки, гарантии и соответствие требованиям ТЗ", True),
    Kind("clarify", "Запрос разъяснений", "Официальное письмо заказчику с вопросами по документации", False),
    Kind("cover", "Сопроводительное письмо", "Письмо к заявке на участие с перечнем приложений", False),
    Kind("inventory", "Опись документов", "Перечень документов заявки с отметкой о готовности", False),
    Kind("experience", "Справка об опыте", "Таблица аналогичных договоров за последние годы", False),
]}


class GenerationError(Exception):
    pass


# ── Оформление ─────────────────────────────────────────────

def _font(run, size=12, bold=False, italic=False, color=None):
    run.font.name = "Times New Roman"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    run.font.size = Pt(size)
    run.bold, run.italic = bold, italic
    if color:
        run.font.color.rgb = RGBColor(*color)
    return run


def _p(doc, text="", *, size=12, bold=False, italic=False, align=None, space_after=4, indent=False, color=None):
    p = doc.add_paragraph()
    if align:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.line_spacing = 1.15
    if indent:
        p.paragraph_format.first_line_indent = Cm(1.25)
    if text:
        _font(p.add_run(text), size, bold, italic, color)
    return p


def _cell(cell, text, *, bold=False, size=11, align=None):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    if align:
        p.alignment = align
    _font(p.add_run(str(text)), size, bold)


def _shade(cell, hex_color="F2F2F2"):
    tc_pr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _table(doc, header: list[str], rows: list[list[str]], widths: list[float]):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        _cell(t.rows[0].cells[i], h, bold=True, size=10.5, align=WD_ALIGN_PARAGRAPH.CENTER)
        _shade(t.rows[0].cells[i])
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r):
            _cell(cells[i], v, size=10.5, align=WD_ALIGN_PARAGRAPH.CENTER if i == 0 else None)
    t.autofit = False
    for i, w in enumerate(widths):
        t.columns[i].width = Cm(w)
    grid = t._tbl.tblGrid
    for i, gc in enumerate(grid.findall(qn("w:gridCol"))):
        if i < len(widths):
            gc.set(qn("w:w"), str(int(Cm(widths[i]).twips)))
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Cm(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def _new_doc() -> Docx:
    doc = Docx()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21)
    sec.left_margin, sec.right_margin = Cm(2.5), Cm(1.5)
    sec.top_margin, sec.bottom_margin = Cm(2), Cm(2)
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    return doc


def _v(value: str | None) -> str:
    return value.strip() if value and value.strip() else BLANK


def money_kz(x: float) -> str:
    whole, frac = f"{x:,.2f}".split(".")
    return f"{whole.replace(',', ' ')},{frac}"


def _today() -> str:
    return datetime.now(config.TZ).strftime("%d.%m.%Y")


def _letterhead(doc, u: User, lot: Lot, *, addressee: bool = True):
    """Шапка: реквизиты компании, исходящий номер, адресат."""
    name = _v(u.company_name)
    _p(doc, name, size=13, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
    req = [f"БИН {_v(u.company_bin)}"]
    if u.address:
        req.append(u.address)
    contacts = ", ".join(x for x in [u.phone, u.email] if x)
    if contacts:
        req.append(contacts)
    _p(doc, " · ".join(req), size=10, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, color=(80, 80, 80))
    line = doc.add_paragraph()
    p_pr = line._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "808080")):
        bottom.set(qn(k), v)
    bdr.append(bottom)
    p_pr.append(bdr)
    line.paragraph_format.space_after = Pt(10)

    t = doc.add_table(rows=1, cols=2)
    left, right = t.rows[0].cells
    left.width, right.width = Cm(8), Cm(9)
    _cell(left, f"Исх. № {BLANK}\nот {_today()}", size=11)
    if addressee:
        _cell(right, f"{lot.customer or 'Заказчику'}", size=11, bold=True)
        right.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _p(doc, space_after=8)


def _subject(lot: Lot) -> str:
    ext = f" № {lot.ext_id}" if lot.ext_id and lot.source != "demo" else ""
    return f"закупке{ext} «{lot.title}»"


def _signature(doc, u: User):
    _p(doc, space_after=14)
    t = doc.add_table(rows=1, cols=3)
    a, b, c = t.rows[0].cells
    _cell(a, f"{u.director_title or 'Директор'}\n{_v(u.company_name)}", size=12)
    _cell(b, "\n____________________", size=12, align=WD_ALIGN_PARAGRAPH.CENTER)
    _cell(c, f"\n{_v(u.director)}", size=12, align=WD_ALIGN_PARAGRAPH.RIGHT)
    a.width, b.width, c.width = Cm(7), Cm(5), Cm(5)
    _p(doc, "М.П. (при наличии)", size=10, italic=True, color=(110, 110, 110))


def _to_bytes(doc: Docx) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── Данные ─────────────────────────────────────────────────

async def _analysis(s: AsyncSession, u: User, lot: Lot) -> dict | None:
    a = (await s.execute(select(Analysis).where(Analysis.user_id == u.id, Analysis.lot_id == lot.id))).scalar_one_or_none()
    return a.data if a else None


async def _docs_context(lot: Lot) -> str:
    docs = await load_documents(lot)
    budget, blocks = MAX_DOC_CHARS, []
    for name, text in docs:
        chunk = text[:budget]
        budget -= len(chunk)
        blocks.append(f"=== {name} ===\n{chunk}")
        if budget <= 0:
            break
    return "\n\n".join(blocks) if blocks else "Документация не загружена — опирайся на карточку закупки."


def _list(x) -> list:
    return [i for i in (x or []) if i] if isinstance(x, list) else []


# ── Сборка документов ─────────────────────────────────────

async def build_cover(s, u, lot, analysis) -> bytes:
    doc = _new_doc()
    _letterhead(doc, u, lot)
    _p(doc, "Сопроводительное письмо", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _p(doc, f"{_v(u.company_name)} (БИН {_v(u.company_bin)}) направляет заявку на участие в {_subject(lot)}.", indent=True)
    if lot.amount:
        _p(doc, f"Выделенная сумма закупки: {money_kz(lot.amount)} тенге.", indent=True)
    _p(doc, "Настоящим подтверждаем, что ознакомлены с условиями закупки и технической спецификацией, согласны с ними "
            "и обязуемся исполнить договор в соответствии с требованиями документации.", indent=True)
    _p(doc, "Контактное лицо по вопросам заявки: "
            f"{_v(u.director)}, тел. {_v(u.phone)}, e-mail {_v(u.email)}.", indent=True, space_after=10)
    items = _list(analysis.get("documents") if analysis else None)
    if items:
        _p(doc, "Приложения:", bold=True)
        for i, d in enumerate(items, 1):
            _p(doc, f"{i}. {d}", space_after=1)
    _signature(doc, u)
    return _to_bytes(doc)


async def build_inventory(s, u, lot, analysis) -> bytes:
    required = [str(x) for x in _list(analysis.get("documents") if analysis else None)]
    if required:
        ready = await docsvc.readiness(s, u.id, required, lot.deadline)
        rows = []
        for i, x in enumerate(ready["items"], 1):
            d = x["document"]
            note = {"ready": "Есть", "expired": "Просрочен — обновить", "missing": "Подготовить"}.get(x["status"], "")
            rows.append([str(i), x["requirement"], d["title"] if d else "", note])
    else:
        lib = await docsvc.list_docs(s, u.id)
        rows = [[str(i), d.title, d.filename or "", ""] for i, d in enumerate(lib, 1)]
    doc = _new_doc()
    _letterhead(doc, u, lot, addressee=False)
    _p(doc, "Опись документов заявки", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _p(doc, f"на участие в {_subject(lot)}", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    if not rows:
        rows = [[str(i), "", "", ""] for i in range(1, 9)]
    _table(doc, ["№", "Документ по требованию закупки", "Документ компании", "Отметка"], rows, [1, 7.5, 5.5, 3])
    _p(doc, "Количество листов: ______", space_after=2)
    _signature(doc, u)
    return _to_bytes(doc)


async def build_clarify(s, u, lot, analysis) -> tuple[bytes, bool]:
    questions = [str(q) for q in _list(analysis.get("questions") if analysis else None)]
    used_ai = False
    if not questions:
        if not ai.available():
            raise GenerationError("Сначала выполните ИИ-разбор закупки — вопросы к заказчику берутся из него.")
        data = await ai.ask_json(
            config.AI_SMART_MODEL,
            "Ты тендерный специалист в Казахстане. Сформулируй вопросы заказчику по неясным местам документации "
            "закупки: объём, сроки, место, характеристики, приёмка, оплата. Вопросы — официальным деловым языком, "
            "каждый самостоятельный и конкретный. Не выдумывай фактов. Ответ — только JSON.",
            f"Закупка:\n{json.dumps(lot_card(lot), ensure_ascii=False)}\n\nДокументация:\n{await _docs_context(lot)}\n\n"
            'Верни JSON: {"questions": ["вопрос", ...]} — от 3 до 7 вопросов.',
            max_tokens=1200,
        )
        questions, used_ai = [str(q) for q in _list(data.get("questions"))], True
    doc = _new_doc()
    _letterhead(doc, u, lot)
    _p(doc, "Запрос о разъяснении положений документации", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _p(doc, f"{_v(u.company_name)}, являясь потенциальным поставщиком по {_subject(lot)}, в соответствии с "
            "законодательством Республики Казахстан о государственных закупках просит разъяснить следующие положения документации:",
       indent=True, space_after=8)
    for i, q in enumerate(questions, 1):
        _p(doc, f"{i}. {q}", indent=False, space_after=4)
    _p(doc, "Просим направить разъяснения в установленный срок и разместить их на веб-портале государственных закупок.",
       indent=True, space_after=4)
    _p(doc, "Запрос подаётся через веб-портал государственных закупок в разделе обсуждения документации.",
       size=10, italic=True, color=(110, 110, 110))
    _signature(doc, u)
    return _to_bytes(doc), used_ai


TECH_SYSTEM = (
    "Ты — тендерный специалист в Казахстане и готовишь техническое предложение поставщика к заявке на госзакупку. "
    "Опирайся только на карточку закупки, техническую спецификацию и профиль компании. "
    "Нельзя выдумывать количества, марки, сроки и характеристики: если их нет в документации, пиши «[уточнить]». "
    "Нельзя приписывать компании опыт, лицензии и технику, которых нет в профиле. "
    "Стиль — официально-деловой, от лица поставщика («Поставщик обязуется…»). Ответ — только JSON."
)
TECH_SCHEMA = """{
  "intro": "1–2 предложения: что поставщик предлагает выполнить/поставить",
  "scope": [{"name": "позиция или вид работ", "qty": "количество или [уточнить]", "unit": "ед. изм.", "note": "характеристики/требования"}],
  "method": ["порядок выполнения работ / поставки — этапами"],
  "timeline": "срок выполнения по документации или [уточнить]",
  "resources": ["персонал, техника, материалы — только из профиля компании или [уточнить]"],
  "quality": ["меры контроля качества и безопасности"],
  "warranty": "гарантийные обязательства по документации или [уточнить]",
  "compliance": [{"requirement": "требование ТЗ", "response": "как поставщик его выполняет"}]
}"""


async def build_tech(s, u, lot, analysis) -> tuple[bytes, bool]:
    if not ai.available():
        raise GenerationError("Техническое предложение готовит ИИ — подключите ключ ANTHROPIC_API_KEY.")
    prompt = (
        f"Компания:\n{profile_text(u)}\n\nЗакупка:\n{json.dumps(lot_card(lot), ensure_ascii=False, indent=1)}\n\n"
        f"Разбор (если есть):\n{json.dumps(analysis, ensure_ascii=False) if analysis else 'нет'}\n\n"
        f"Документация:\n{await _docs_context(lot)}\n\nВерни JSON строго такого вида:\n{TECH_SCHEMA}"
    )
    d = await ai.ask_json(config.AI_SMART_MODEL, TECH_SYSTEM, prompt, max_tokens=3500)

    doc = _new_doc()
    _letterhead(doc, u, lot)
    _p(doc, "Техническое предложение", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _p(doc, f"по {_subject(lot)}", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    if d.get("intro"):
        _p(doc, str(d["intro"]), indent=True, space_after=8)

    def section(n, title):
        _p(doc, f"{n}. {title}", bold=True, space_after=4)

    n = 1
    scope = [x for x in _list(d.get("scope")) if isinstance(x, dict)]
    if scope:
        section(n, "Объём поставки / работ"); n += 1
        _table(doc, ["№", "Наименование", "Кол-во", "Ед.", "Характеристики"],
               [[str(i), x.get("name", ""), x.get("qty", ""), x.get("unit", ""), x.get("note", "")] for i, x in enumerate(scope, 1)],
               [1, 5.6, 2.4, 1.6, 6.4])
    for key, title in (("method", "Порядок выполнения"), ("resources", "Ресурсы поставщика"), ("quality", "Контроль качества и безопасность")):
        items = _list(d.get(key))
        if items:
            section(n, title); n += 1
            for i, x in enumerate(items, 1):
                _p(doc, f"{n - 1}.{i}. {x}", space_after=2)
            _p(doc, space_after=4)
    for key, title in (("timeline", "Сроки выполнения"), ("warranty", "Гарантийные обязательства")):
        if d.get(key):
            section(n, title); n += 1
            _p(doc, str(d[key]), indent=True, space_after=8)
    comp = [x for x in _list(d.get("compliance")) if isinstance(x, dict)]
    if comp:
        section(n, "Соответствие требованиям технической спецификации"); n += 1
        _table(doc, ["№", "Требование", "Предложение поставщика"],
               [[str(i), x.get("requirement", ""), x.get("response", "")] for i, x in enumerate(comp, 1)], [1, 8, 8])
    _p(doc, "Пункты с отметкой «[уточнить]» заполните по технической спецификации перед подачей заявки.",
       size=10, italic=True, color=(110, 110, 110))
    _signature(doc, u)
    return _to_bytes(doc), True


async def build_experience(s, u, lot, analysis) -> bytes:
    rows: list[list[str]] = []
    if u.company_bin:
        won = (await s.execute(select(Award).where(Award.winner_bin == u.company_bin, Award.status == "done")
                               .order_by(Award.awarded_at.desc()).limit(30))).scalars().all()
        for i, a in enumerate(won, 1):
            date = aware(a.awarded_at).astimezone(config.TZ).strftime("%d.%m.%Y") if a.awarded_at else ""
            rows.append([str(i), a.title, a.customer, date, f"{(a.price or 0):,.0f}".replace(",", " ")])
    lib = [d for d in await docsvc.list_docs(s, u.id) if d.kind == "experience"]
    doc = _new_doc()
    _letterhead(doc, u, lot, addressee=False)
    _p(doc, "Справка об опыте работы", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _p(doc, f"{_v(u.company_name)} по {_subject(lot)}", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _p(doc, "Сведения о выполненных аналогичных договорах:", space_after=6)
    if not rows:
        rows = [[str(i), "", "", "", ""] for i in range(1, 7)]
    _table(doc, ["№", "Предмет договора", "Заказчик", "Дата", "Сумма, ₸"], rows, [1, 6.5, 4.5, 2.2, 2.8])
    if lib:
        _p(doc, "Подтверждающие документы:", bold=True, space_after=2)
        for i, d in enumerate(lib, 1):
            _p(doc, f"{i}. {d.title}", space_after=1)
    _p(doc, "Достоверность сведений подтверждаем.", indent=True, space_after=4)
    _signature(doc, u)
    return _to_bytes(doc)


def _filename(kind: Kind, lot: Lot) -> str:
    short = re.sub(r"[^\w\- ]+", "", lot.title)[:50].strip().replace(" ", "_")
    return f"{kind.title.replace(' ', '_')}_{short}.docx"


async def generate(s: AsyncSession, u: User, lot: Lot, kind_code: str) -> GeneratedDoc:
    kind = KINDS.get(kind_code)
    if not kind:
        raise GenerationError("Неизвестный тип документа")
    analysis = await _analysis(s, u, lot)
    needs_ai = kind.code == "tech" or (kind.code == "clarify" and not _list((analysis or {}).get("questions")))
    if needs_ai:
        from app.services.billing import check

        await check(s, u, "generations")  # LimitError, если лимит исчерпан
    used_ai = False
    if kind.code == "tech":
        content, used_ai = await build_tech(s, u, lot, analysis)
    elif kind.code == "clarify":
        content, used_ai = await build_clarify(s, u, lot, analysis)
    elif kind.code == "cover":
        content = await build_cover(s, u, lot, analysis)
    elif kind.code == "inventory":
        content = await build_inventory(s, u, lot, analysis)
    else:
        content = await build_experience(s, u, lot, analysis)
    g = GeneratedDoc(user_id=u.id, lot_id=lot.id, kind=kind.code, title=kind.title, filename=_filename(kind, lot),
                     content=content, by_ai=used_ai, created_at=utcnow())
    s.add(g)
    await s.commit()
    return g


def missing_requisites(u: User) -> list[str]:
    fields = [("company_name", "название компании"), ("company_bin", "БИН"), ("director", "ФИО руководителя"),
              ("address", "адрес"), ("phone", "телефон")]
    return [label for f, label in fields if not (getattr(u, f) or "").strip()]
