"""Конкуренты по БИН: досье по итогам закупок и уведомления о новых победах.

Без ИИ — 0 токенов. Данные — таблица awards (итоги закупок: победитель, цена, заказчик).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import Award, Competitor, User, aware, utcnow
from app.services import billing
from app.services.core import profile_of
from app.services.matcher import keyword_hit

BIN_RE = re.compile(r"^\d{12}$")


class CompetitorError(Exception):
    pass


def clean_bin(value: str) -> str:
    b = re.sub(r"\D", "", value or "")
    if not BIN_RE.match(b):
        raise CompetitorError("БИН — это 12 цифр. Его можно найти в карточке компании на портале госзакупок или в отчёте Kyran.")
    return b


async def known_name(s: AsyncSession, bin_: str) -> str:
    name = (await s.execute(select(Award.winner).where(Award.winner_bin == bin_, Award.winner != "")
                            .order_by(Award.awarded_at.desc()).limit(1))).scalar()
    return name or ""


async def follow(s: AsyncSession, user: User, bin_value: str, name: str = "") -> Competitor:
    bin_ = clean_bin(bin_value)
    if user.company_bin and bin_ == user.company_bin:
        raise CompetitorError("Это БИН вашей компании.")
    existing = (await s.execute(select(Competitor).where(Competitor.user_id == user.id, Competitor.bin == bin_))).scalar_one_or_none()
    if existing:
        return existing
    try:
        await billing.check(s, user, "competitors")
    except billing.LimitError as e:
        raise CompetitorError(str(e))
    from app.services import results

    org = await results.ensure_supplier(s, bin_)
    if org and not name:
        name = org.name
    last = (await s.execute(select(func.max(Award.id)).where(Award.winner_bin == bin_))).scalar() or 0
    c = Competitor(user_id=user.id, bin=bin_, name=(name.strip() or await known_name(s, bin_))[:500], last_award_id=last)
    s.add(c)
    await s.commit()
    return c


async def unfollow(s: AsyncSession, user: User, bin_value: str) -> bool:
    bin_ = re.sub(r"\D", "", bin_value or "")
    c = (await s.execute(select(Competitor).where(Competitor.user_id == user.id, Competitor.bin == bin_))).scalar_one_or_none()
    if not c:
        return False
    await s.delete(c)
    await s.commit()
    return True


def _award_json(a: Award) -> dict:
    return {"id": a.id, "title": a.title, "customer": a.customer, "region": a.region, "amount": a.amount, "price": a.price,
            "participants": a.participants, "date": aware(a.awarded_at).isoformat() if a.awarded_at else None,
            "discount": round((1 - a.price / a.amount) * 100, 1) if a.amount and a.price else None}


async def dossier(s: AsyncSession, user: User, bin_: str, days: int = 365) -> dict:
    """Досье: победы за период, суммы, снижение, заказчики, регионы, помесячно, пересечение с вашими направлениями."""
    from app.services import results

    org = await results.ensure_supplier(s, bin_, days=max(days, 365))
    since = utcnow() - timedelta(days=days)
    wins = (await s.execute(select(Award).where(Award.winner_bin == bin_, Award.status == "done", Award.awarded_at >= since)
                            .order_by(Award.awarded_at.desc()))).scalars().all()
    if any(not a.is_demo for a in wins):
        wins = [a for a in wins if not a.is_demo]
    profile = profile_of(user)
    overlap = [a for a in wins if keyword_hit(a.title, "", a.amount, a.region, profile)]
    discounts = [1 - a.price / a.amount for a in wins if a.amount and a.price]
    customers = Counter(a.customer for a in wins if a.customer)
    regions = Counter(a.region for a in wins if a.region)
    months: dict[str, float] = defaultdict(float)
    for a in wins:
        if a.awarded_at:
            months[aware(a.awarded_at).astimezone(config.TZ).strftime("%Y-%m")] += a.price or 0
    # последние 12 месяцев подряд, включая пустые
    now = utcnow().astimezone(config.TZ)
    series = []
    y, m = now.year, now.month
    for _ in range(12):
        key = f"{y:04d}-{m:02d}"
        series.append({"month": key, "total": months.get(key, 0.0)})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    series.reverse()
    name = (org.name if org else "") or (wins[0].winner if wins else await known_name(s, bin_))
    return {
        "bin": bin_, "name": name, "days": days,
        "wins": len(wins), "total": sum(a.price or 0 for a in wins),
        "median_discount": round(median(discounts) * 100, 1) if discounts else None,
        "avg_participants": round(sum(a.participants or 0 for a in wins) / len(wins), 1) if wins else None,
        "overlap": {"wins": len(overlap), "total": sum(a.price or 0 for a in overlap)},
        "customers": [{"name": k, "wins": v} for k, v in customers.most_common(5)],
        "regions": [{"name": k, "wins": v} for k, v in regions.most_common(5)],
        "months": series,
        "recent": [_award_json(a) for a in wins[:15]],
        "is_demo": any(a.is_demo for a in wins),
    }


async def list_for(s: AsyncSession, user: User) -> list[dict]:
    rows = (await s.execute(select(Competitor).where(Competitor.user_id == user.id).order_by(Competitor.created_at))).scalars().all()
    since = utcnow() - timedelta(days=365)
    out = []
    for c in rows:
        stats = (await s.execute(select(func.count(Award.id), func.coalesce(func.sum(Award.price), 0), func.max(Award.awarded_at))
                                 .where(Award.winner_bin == c.bin, Award.status == "done", Award.awarded_at >= since))).one()
        out.append({"bin": c.bin, "name": c.name or "Компания " + c.bin, "wins": int(stats[0] or 0), "total": float(stats[1] or 0),
                    "last_win": aware(stats[2]).isoformat() if stats[2] else None})
    return out


async def suggest(s: AsyncSession, user: User, days: int = 180, limit: int = 8) -> list[dict]:
    """Кто чаще всего выигрывает в ваших направлениях — кандидаты для слежки (и потенциальные клиенты).

    Похожесть: код ЕНС ТРУ из подходящих вам открытых лотов или слова направлений в предмете договора.
    БИН недостающих победителей дочитывается из карточек договоров.
    """
    from collections import Counter as _C

    from app.db import Lot, Match
    from app.services import results

    lots = (await s.execute(select(Lot).join(Match, Match.lot_id == Lot.id)
                            .where(Match.user_id == user.id, Lot.source == "zakup").limit(300))).scalars().all()
    codes = [c for c, _ in _C("".join(ch for ch in str((l.raw or {}).get("enstru") or "") if ch.isdigit())[:6]
                               for l in lots).most_common(6) if len(c) == 6]
    if codes:
        await results.ensure_recent_for_codes(s, codes, days=90)
    code_set = set(codes)
    since = utcnow() - timedelta(days=days)
    followed = set((await s.execute(select(Competitor.bin).where(Competitor.user_id == user.id))).scalars())
    awards = (await s.execute(select(Award).where(Award.status == "done", Award.awarded_at >= since).limit(20000))).scalars().all()
    if any(not a.is_demo for a in awards):
        awards = [a for a in awards if not a.is_demo]
    profile = profile_of(user)
    agg: dict[str, dict] = {}
    for a in awards:
        if not a.winner:
            continue
        if not ((a.enstru or "")[:6] in code_set or keyword_hit(a.title, "", a.amount, a.region, profile)):
            continue
        key = a.winner_bin or f"org:{a.supplier_org_id}" if (a.winner_bin or a.supplier_org_id) else f"name:{a.winner}"
        d = agg.setdefault(key, {"bin": a.winner_bin, "name": a.winner, "wins": 0, "total": 0.0, "org_id": a.supplier_org_id, "award": a})
        d["wins"] += 1
        d["total"] += a.price or 0
    top = sorted(agg.values(), key=lambda x: (-x["total"], -x["wins"]))
    out = []
    missing = [d for d in top[:limit * 2] if not d["bin"] and d["award"].source == "zakup"]
    if missing and results.enabled():
        try:
            async with results.Client() as c:
                await results.enrich(s, c, [d["award"] for d in missing], limit=len(missing))
            await s.commit()
            for d in missing:
                d["bin"] = d["award"].winner_bin or await results.bin_of(s, d["org_id"])
        except Exception:
            pass
    for d in top:
        if d["bin"] and d["bin"] not in followed and d["bin"] != user.company_bin:
            out.append({"bin": d["bin"], "name": d["name"], "wins": d["wins"], "total": d["total"]})
        if len(out) >= limit:
            break
    return out


async def new_wins(s: AsyncSession) -> list[tuple[int, Competitor, list[Award]]]:
    """Новые победы всех отслеживаемых конкурентов (и сдвигает отметку last_award_id)."""
    from app.services import results

    out = []
    for c in (await s.execute(select(Competitor))).scalars().all():
        await results.ensure_supplier(s, c.bin, days=30, ttl=timedelta(minutes=50))
        wins = (await s.execute(select(Award).where(Award.winner_bin == c.bin, Award.id > c.last_award_id, Award.status == "done")
                                .order_by(Award.id))).scalars().all()
        if wins:
            c.last_award_id = wins[-1].id
            if not c.name:
                c.name = wins[-1].winner
            out.append((c.user_id, c, wins))
    await s.commit()
    return out
