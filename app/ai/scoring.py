"""ИИ-оценка лотов 0–100 под профиль компании (быстрая модель, пачками)."""
from __future__ import annotations

import json
import logging

from app import config
from app.ai import client
from app.db import Lot, User

log = logging.getLogger(__name__)
BATCH = 20

SYSTEM = (
    "Ты — эксперт по госзакупкам Казахстана. Оцени, насколько каждая закупка подходит компании-поставщику. "
    "Учитывай смысл, а не только слова: «канцтовары» подходят к «бумага А4», «ремонт кровли» не подходит к «поставка кровельных материалов», "
    "если компания делает работы, а не поставки. 90–100 — точно их профиль; 70–89 — скорее подходит; 40–69 — частично; ниже 40 — не подходит. "
    "Причину пиши коротко по-русски (до 12 слов), обращаясь к компании на «вы». Ответ — только JSON."
)


def profile_text(user: User) -> str:
    parts = [f"Направления: {', '.join(user.keywords or []) or 'не указаны'}"]
    if user.about:
        parts.append(f"О компании: {user.about}")
    if user.company_name:
        parts.append(f"Компания: {user.company_name}")
    if user.min_amount or user.max_amount:
        parts.append(f"Интересные суммы: от {user.min_amount or 0:,.0f} до {user.max_amount or 0:,.0f} ₸".replace(",", " "))
    if user.regions:
        parts.append(f"Регионы: {', '.join(user.regions)}")
    return "\n".join(parts)


async def score_lots(user: User, lots: list[Lot]) -> dict[int, tuple[int, str]]:
    """{lot_id: (оценка, причина)}. Пустой словарь, если ИИ недоступен или ошибся."""
    if not client.available() or not lots:
        return {}
    result: dict[int, tuple[int, str]] = {}
    for i in range(0, len(lots), BATCH):
        batch = lots[i : i + BATCH]
        items = [
            {"id": l.id, "title": l.title[:300], "description": (l.description or "")[:400],
             "amount": l.amount, "region": l.region, "customer": l.customer[:150]}
            for l in batch
        ]
        prompt = (
            f"Профиль компании:\n{profile_text(user)}\n\nЗакупки:\n{json.dumps(items, ensure_ascii=False)}\n\n"
            'Верни JSON: {"scores": [{"id": <id>, "score": <0-100>, "reason": "<почему>"}]}'
        )
        try:
            data = await client.ask_json(config.AI_FAST_MODEL, SYSTEM, prompt, max_tokens=3000)
            for row in data.get("scores", []):
                lot_id, score = int(row["id"]), max(0, min(100, int(row["score"])))
                result[lot_id] = (score, str(row.get("reason", ""))[:200])
        except Exception:
            log.exception("ИИ-оценка не удалась, остаёмся на подборе по словам")
    return result
