"""Аналитика рынка по закупке: история заказчика, снижение цены, конкуренты, рекомендуемая цена.

Источник — итоги прошлых закупок (таблица awards). Похожесть — по основам слов предмета закупки.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from statistics import median

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Award, Lot, aware
from app.services.matcher import STOP, query_stems, words
from app.sources.base import AwardData


async def upsert_awards(s: AsyncSession, items: list[AwardData]) -> int:
    if not items:
        return 0
    ids = {(it.source, it.ext_id) for it in items}
    existing = set()
    for source in {src for src, _ in ids}:
        rows = await s.execute(select(Award.source, Award.ext_id).where(
            Award.source == source, Award.ext_id.in_([e for src, e in ids if src == source])))
        existing |= set(rows.all())
    added = 0
    for it in items:
        if (it.source, it.ext_id) in existing:
            continue
        existing.add((it.source, it.ext_id))
        data = asdict(it)
        if data["awarded_at"]:
            data["awarded_at"] = aware(data["awarded_at"])
        s.add(Award(**data))
        added += 1
    await s.flush()
    return added


def _stems(text: str) -> list[str]:
    return sorted((st for st in query_stems(text) if st not in STOP and len(st) >= 4), key=lambda x: (-len(x), x))


def similarity(lot_stems: list[str], title: str) -> float:
    if not lot_stems:
        return 0.0
    w = words(title)
    return sum(1 for st in lot_stems if any(x.startswith(st) for x in w)) / len(lot_stems)


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _discount(a: Award) -> float | None:
    if a.status != "done" or not a.amount or not a.price:
        return None
    return max(0.0, 1 - a.price / a.amount)


def _award_json(a: Award) -> dict:
    d = _discount(a)
    return {
        "title": a.title, "customer": a.customer, "amount": a.amount, "price": a.price, "winner": a.winner,
        "participants": a.participants, "status": a.status, "discount": round(d * 100, 1) if d is not None else None,
        "date": aware(a.awarded_at).isoformat() if a.awarded_at else None,
    }


async def lot_insights(s: AsyncSession, lot: Lot, *, min_sim: float = 0.5) -> dict:
    stems = _stems(lot.title)
    conds = []
    if lot.customer_bin:
        conds.append(Award.customer_bin == lot.customer_bin)
    if lot.customer:
        conds.append(Award.customer == lot.customer)
    # Кандидаты: тот же заказчик + те, где в названии есть хотя бы одна основа (грубо, затем точная оценка)
    for st in stems[:6]:
        conds.append(Award.title.ilike(f"%{st}%"))
    if not conds:
        return {"available": False}
    rows = (await s.execute(select(Award).where(or_(*conds)).order_by(Award.awarded_at.desc()).limit(800))).scalars().all()

    customer_hist = [a for a in rows if (lot.customer_bin and a.customer_bin == lot.customer_bin) or a.customer == lot.customer]
    similar = [a for a in rows if similarity(stems, a.title) >= min_sim]
    if not similar and not customer_hist:
        return {"available": False}

    discounts = [d for a in similar if (d := _discount(a)) is not None]
    parts = [a.participants for a in similar if a.participants]
    failed = sum(1 for a in similar if a.status == "failed")

    wins: dict[str, dict] = defaultdict(lambda: {"wins": 0, "total": 0.0, "discounts": []})
    for a in similar:
        if a.status == "done" and a.winner:
            w = wins[a.winner]
            w["wins"] += 1
            w["total"] += a.price or 0
            if (d := _discount(a)) is not None:
                w["discounts"].append(d)
    competitors = sorted(
        ({"name": k, "wins": v["wins"], "total": v["total"],
          "avg_discount": round(sum(v["discounts"]) / len(v["discounts"]) * 100, 1) if v["discounts"] else None}
         for k, v in wins.items()),
        key=lambda x: (-x["wins"], -x["total"]))[:5]

    cust_done = [a for a in customer_hist if a.status == "done"]
    cust_winners = Counter(a.winner for a in cust_done if a.winner)
    loyal = cust_winners.most_common(1)[0] if cust_winners else None

    price = None
    if discounts and lot.amount:
        med, p75, p25 = median(discounts), _pct(discounts, 0.75), _pct(discounts, 0.25)
        price = {
            "median_discount": round(med * 100, 1), "low_discount": round(p25 * 100, 1), "high_discount": round(p75 * 100, 1),
            "recommended": round(lot.amount * (1 - med), -3),
            "range": [round(lot.amount * (1 - p75), -3), round(lot.amount * (1 - p25), -3)],
        }

    # Гистограмма снижения: шаг 5%
    buckets = [0] * 7
    for d in discounts:
        buckets[min(6, int(d * 100 // 5))] += 1

    return {
        "available": True,
        "similar_count": len(similar),
        "avg_participants": round(sum(parts) / len(parts), 1) if parts else None,
        "single_share": round(sum(1 for p in parts if p == 1) / len(parts) * 100) if parts else None,
        "failed": failed,
        "price": price,
        "histogram": {"step": 5, "counts": buckets},
        "competitors": competitors,
        "customer": {
            "total": len(customer_hist), "done": len(cust_done),
            "failed": sum(1 for a in customer_hist if a.status == "failed"),
            "loyal_winner": {"name": loyal[0], "wins": loyal[1]} if loyal and loyal[1] >= 2 and loyal[1] / max(1, len(cust_done)) >= 0.5 else None,
            "recent": [_award_json(a) for a in customer_hist[:6]],
        },
        "recent_similar": [_award_json(a) for a in similar[:8]],
        "is_demo": any(a.is_demo for a in rows),
    }
