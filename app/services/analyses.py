"""Кэш ИИ-разборов: один разбор на пару (компания, тендер), повторно не тратим токены."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import analysis as ai_analysis
from app.db import Analysis, Lot, Match, User, utcnow


async def get_analysis(s: AsyncSession, user: User, lot_id: int) -> Analysis | None:
    return (await s.execute(select(Analysis).where(Analysis.user_id == user.id, Analysis.lot_id == lot_id))).scalar_one_or_none()


async def get_or_create_analysis(s: AsyncSession, user: User, lot: Lot, refresh: bool = False) -> Analysis:
    existing = await get_analysis(s, user, lot.id)
    if existing and not refresh:
        return existing
    from app.services.billing import check

    await check(s, user, "analyses")  # LimitError, если лимит тарифа исчерпан
    data = await ai_analysis.analyze(user, lot)  # RuntimeError, если ИИ не подключён
    if existing:
        existing.data, existing.verdict, existing.model = data, data["verdict"], config.AI_SMART_MODEL
        existing.created_at = utcnow()  # повторный разбор тоже расходует лимит
        a = existing
    else:
        a = Analysis(user_id=user.id, lot_id=lot.id, data=data, verdict=data["verdict"], model=config.AI_SMART_MODEL)
        s.add(a)
    # Оценка из разбора точнее — обновляем совпадение
    match = (await s.execute(select(Match).where(Match.user_id == user.id, Match.lot_id == lot.id))).scalar_one_or_none()
    if match and isinstance(data.get("score"), (int, float)):
        match.score, match.by_ai = int(data["score"]), True
    await s.commit()
    return a
