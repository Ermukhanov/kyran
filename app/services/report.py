"""Еженедельный отчёт «что прошло мимо вас».

Берём итоги закупок за период, которые подходят под профиль компании, но выиграли другие:
сколько денег, кто забрал, с каким снижением. Плюс что было сделано за неделю и
какие подходящие закупки закрываются в ближайшие дни. Без ИИ — 0 токенов.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Award, Lot, Match, PipelineItem, User, aware, utcnow
from app.services.core import open_condition, profile_of
from app.services.matcher import keyword_hit


async def weekly(s: AsyncSession, user: User, days: int = 7) -> dict:
    now = utcnow()
    since = now - timedelta(days=days)
    profile = profile_of(user)

    awards = (await s.execute(select(Award).where(Award.awarded_at >= since).order_by(Award.awarded_at.desc()).limit(5000))).scalars().all()
    missed, own = [], []
    for a in awards:
        if not keyword_hit(a.title, "", a.amount, a.region, profile):
            continue
        if user.company_bin and a.winner_bin == user.company_bin:
            own.append(a)
        elif a.status == "done" and a.price:
            missed.append(a)

    winners: dict[str, dict] = defaultdict(lambda: {"wins": 0, "total": 0.0})
    discounts = []
    for a in missed:
        w = winners[a.winner or "—"]
        w["wins"] += 1
        w["total"] += a.price or 0
        if a.amount and a.price:
            discounts.append(max(0.0, 1 - a.price / a.amount))
    top = sorted(({"name": k, **v} for k, v in winners.items()), key=lambda x: -x["total"])[:5]

    new_matches = (await s.execute(select(func.count(Match.id)).where(
        Match.user_id == user.id, Match.created_at >= since, Match.hidden.is_(False)))).scalar() or 0
    stages = dict((await s.execute(select(PipelineItem.stage, func.count(PipelineItem.id)).where(
        PipelineItem.user_id == user.id, PipelineItem.updated_at >= since).group_by(PipelineItem.stage))).all())

    in_pipeline = select(PipelineItem.lot_id).where(PipelineItem.user_id == user.id)
    soon = (await s.execute(
        select(Lot, Match).join(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
        .where(open_condition(now), Lot.deadline <= now + timedelta(days=7), Match.hidden.is_(False), Lot.id.not_in(in_pipeline))
        .order_by(Match.score.desc(), Lot.amount.desc()).limit(3)
    )).all()

    def award_json(a: Award) -> dict:
        return {"title": a.title, "customer": a.customer, "winner": a.winner, "price": a.price, "amount": a.amount,
                "participants": a.participants, "date": aware(a.awarded_at).isoformat() if a.awarded_at else None,
                "discount": round((1 - a.price / a.amount) * 100, 1) if a.amount and a.price else None}

    missed.sort(key=lambda a: -(a.price or 0))
    return {
        "days": days,
        "missed": {"count": len(missed), "total": sum(a.price or 0 for a in missed), "items": [award_json(a) for a in missed[:10]],
                   "median_discount": round(median(discounts) * 100, 1) if discounts else None},
        "won": {"count": len(own), "total": sum(a.price or 0 for a in own)},
        "winners": top,
        "activity": {"new_matches": int(new_matches), "stages": {k: int(v) for k, v in stages.items()}},
        "soon": [{"id": l.id, "title": l.title, "amount": l.amount, "deadline": aware(l.deadline).isoformat() if l.deadline else None,
                  "score": m.score} for l, m in soon],
        "is_demo": any(a.is_demo for a in awards),
    }
