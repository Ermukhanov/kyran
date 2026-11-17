"""ИИ-агент Kyran: понимает обычную речь и сам вызывает инструменты.

«Найди ремонт школ до 50 млн и следи за ними» → search_tenders + add_keywords + set_amount_range.
Без ключа ANTHROPIC_API_KEY агент не используется — бот делает простой поиск по словам.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.ai.scoring import profile_text
from app.db import Lot, User, STAGES, aware
from app.services import core, monitors
from app.services.matcher import parse_keywords

log = logging.getLogger(__name__)
MAX_STEPS = 6

SYSTEM = """Ты — Kyran, ИИ-агент для поставщиков госзакупок Казахстана. Работаешь в Telegram.
Помогаешь находить тендеры, разбирать их и не пропускать дедлайны. Говоришь коротко, по делу, на «вы»,
на языке пользователя (русский или казахский). Деловой тон, без markdown-разметки и без эмодзи.
Когда пользователь просит найти, следить, изменить профиль или разобрать тендер — вызывай инструменты,
а не объясняй, как это сделать. Никогда не выдумывай тендеры, суммы и даты — только из результатов инструментов.
Найденные тендеры бот покажет карточками сам — в ответе перескажи главное в 1–3 строках.
Демо-лоты (is_demo) — учебные примеры, если они попались, честно скажи об этом."""

TOOLS = [
    {"name": "search_tenders", "description": "Поиск открытых тендеров по словам. Возвращает до 8 лучших.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "что ищем, например 'ремонт кровли'"},
         "min_amount": {"type": "number"}, "max_amount": {"type": "number"}}, "required": ["query"]}},
    {"name": "add_keywords", "description": "Добавить направления в профиль, чтобы агент сам присылал такие тендеры.",
     "input_schema": {"type": "object", "properties": {"keywords": {"type": "array", "items": {"type": "string"}}}, "required": ["keywords"]}},
    {"name": "remove_keywords", "description": "Убрать направления из профиля.",
     "input_schema": {"type": "object", "properties": {"keywords": {"type": "array", "items": {"type": "string"}}}, "required": ["keywords"]}},
    {"name": "set_amount_range", "description": "Задать интересный диапазон сумм в тенге (null — без ограничения).",
     "input_schema": {"type": "object", "properties": {"min_amount": {"type": ["number", "null"]}, "max_amount": {"type": ["number", "null"]}}}},
    {"name": "my_tenders", "description": "Тендеры, уже подобранные под профиль (лучшие сверху).",
     "input_schema": {"type": "object", "properties": {"only_urgent": {"type": "boolean"}}}},
    {"name": "analyze_tender", "description": "Глубокий ИИ-разбор тендера по id: вердикт, требования, документы, риски.",
     "input_schema": {"type": "object", "properties": {"lot_id": {"type": "integer"}}, "required": ["lot_id"]}},
    {"name": "set_stage", "description": f"Положить тендер в воронку. Стадии: {', '.join(STAGES)}; null — убрать.",
     "input_schema": {"type": "object", "properties": {"lot_id": {"type": "integer"}, "stage": {"type": ["string", "null"]}}, "required": ["lot_id", "stage"]}},
    {"name": "watch_website", "description": "Следить за любым сайтом с закупками по ссылке и сообщать о новых.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


def lot_brief(lot: Lot, score: int | None = None) -> dict[str, Any]:
    deadline = aware(lot.deadline)
    return {
        "id": lot.id, "title": lot.title[:200], "amount": lot.amount, "customer": lot.customer[:120],
        "region": lot.region, "deadline": deadline.astimezone(config.TZ).strftime("%d.%m %H:%M") if deadline else None,
        "score": score, "is_demo": lot.is_demo,
    }


class AgentRun:
    def __init__(self, s: AsyncSession, user: User):
        self.s, self.user = s, user
        self.shown_lots: list[int] = []
        self.analysis_lot: int | None = None

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        s, u = self.s, self.user
        if name == "search_tenders":
            p = core.profile_of(u)
            p.min_amount = args.get("min_amount", p.min_amount)
            p.max_amount = args.get("max_amount", p.max_amount)
            hits = await core.search(s, args["query"], p, limit=8)
            self.shown_lots += [l.id for l, _, _ in hits]
            return [lot_brief(l, sc) for l, sc, _ in hits] or "Ничего не найдено"
        if name in ("add_keywords", "remove_keywords"):
            kws = [k for item in args.get("keywords", []) for k in parse_keywords(item)]
            current = list(u.keywords or [])
            if name == "add_keywords":
                current += [k for k in kws if k not in current]
            else:
                current = [k for k in current if k not in kws]
            u.keywords, u.onboarded = current[:30], True
            await s.commit()
            matched = await core.rematch_user(s, u)
            return {"keywords": u.keywords, "matched_now": len(matched)}
        if name == "set_amount_range":
            u.min_amount, u.max_amount = args.get("min_amount"), args.get("max_amount")
            await s.commit()
            await core.rematch_user(s, u)
            return {"min_amount": u.min_amount, "max_amount": u.max_amount}
        if name == "my_tenders":
            rows = await core.feed(s, u, only_urgent=bool(args.get("only_urgent")), limit=8)
            self.shown_lots += [l.id for l, _, _ in rows]
            return [lot_brief(l, m.score) for l, m, _ in rows] or "Пока нет подобранных тендеров"
        if name == "analyze_tender":
            lot = await s.get(Lot, int(args["lot_id"]))
            if not lot:
                return "Тендер не найден"
            self.analysis_lot = lot.id
            from app.services.analyses import get_or_create_analysis

            a = await get_or_create_analysis(s, u, lot)
            return {k: a.data.get(k) for k in ("verdict", "score", "summary", "risks", "documents")}
        if name == "set_stage":
            stage = args.get("stage")
            if stage is not None and stage not in STAGES:
                return f"Неизвестная стадия. Доступны: {', '.join(STAGES)}"
            await core.set_stage(s, u, int(args["lot_id"]), stage)
            return "ok"
        if name == "watch_website":
            m = await monitors.add_monitor(s, u, args["url"])
            return {"watching": m.url, "title": m.title}
        return f"Неизвестный инструмент {name}"


async def respond(s: AsyncSession, user: User, history: list[dict[str, Any]], text: str) -> tuple[str, AgentRun]:
    """Ответ агента. history — прошлые сообщения [{role, content}] (только текст)."""
    run = AgentRun(s, user)
    messages = history[-10:] + [{"role": "user", "content": text}]
    system = SYSTEM + f"\n\nПрофиль пользователя:\n{profile_text(user)}"
    for _ in range(MAX_STEPS):
        resp = await ai.client().messages.create(
            model=config.AI_SMART_MODEL, max_tokens=1500, system=system, tools=TOOLS, messages=messages
        )
        if resp.stop_reason != "tool_use":
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            return reply or "Готово.", run
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = await run.call(block.name, dict(block.input))
                content, is_error = json.dumps(out, ensure_ascii=False, default=str), False
            except Exception as e:
                log.exception("Инструмент %s упал", block.name)
                content, is_error = f"Ошибка: {e}", True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content[:12000], "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "Сделал всё, что смог. Уточните запрос, если нужно что-то ещё.", run
