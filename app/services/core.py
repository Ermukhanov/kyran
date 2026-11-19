"""Основная логика: сохранение лотов, подбор под клиентов, лента, воронка, статистика."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.ai.scoring import score_lots
from app.db import Lot, Match, PipelineItem, User, aware, utcnow
from app.services.matcher import Profile, keyword_hit
from app.sources.base import LotData, Source

log = logging.getLogger(__name__)
AI_MIN_SCORE = 40


def profile_of(user: User) -> Profile:
    return Profile(
        keywords=list(user.keywords or []), min_amount=user.min_amount,
        max_amount=user.max_amount, regions=list(user.regions or []),
    )


def open_condition(now: datetime | None = None):
    now = now or utcnow()
    return or_(Lot.deadline.is_(None), Lot.deadline > now)


# ── Лоты ───────────────────────────────────────────────────

async def upsert_lots(s: AsyncSession, items: list[LotData]) -> list[Lot]:
    """Сохраняет лоты, возвращает только НОВЫЕ."""
    if not items:
        return []
    by_source: dict[str, set[str]] = {}
    for it in items:
        by_source.setdefault(it.source, set()).add(it.ext_id)
    existing: set[tuple[str, str]] = set()
    for source, ids in by_source.items():
        rows = await s.execute(select(Lot.source, Lot.ext_id).where(Lot.source == source, Lot.ext_id.in_(ids)))
        existing |= {(a, b) for a, b in rows.all()}

    new: list[Lot] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        key = (it.source, it.ext_id)
        if key in existing or key in seen:
            continue
        seen.add(key)
        data = asdict(it)
        for field in ("deadline", "published_at"):  # храним в UTC — одинаково в SQLite и PostgreSQL
            if data[field] is not None:
                data[field] = aware(data[field]).astimezone(timezone.utc)
        lot = Lot(**data)
        s.add(lot)
        new.append(lot)
    await s.flush()
    return new


async def fetch_for(s: AsyncSession, keywords: list[str], timeout: float = 25) -> int:
    """Сразу подтянуть открытые лоты по новым направлениям (без ожидания часового сбора)."""
    import asyncio

    from app.sources.registry import SOURCES

    src = next((x for x in SOURCES if getattr(x, "wants_keywords", False) and x.enabled), None)
    if not src or not keywords:
        return 0
    try:
        items = await asyncio.wait_for(src.search(keywords, max_pages=2), timeout=timeout)
    except Exception:
        log.warning("Быстрый поиск по направлениям не удался", exc_info=True)
        return 0
    new = await upsert_lots(s, items)
    await s.commit()
    if new:
        await match_new_lots(s, new)
        await s.commit()
    return len(new)


async def all_keywords(s: AsyncSession) -> list[str]:
    """Направления всех настроенных пользователей — по ним ищем на открытых площадках. Частые — первыми."""
    from collections import Counter

    rows = (await s.execute(select(User.keywords).where(User.onboarded.is_(True)))).scalars().all()
    counter: Counter = Counter()
    for kws in rows:
        for k in kws or []:
            k = str(k).strip().lower()
            if len(k) >= 3:
                counter[k] += 1
    return [k for k, _ in counter.most_common()]


async def collect(s: AsyncSession, sources: list[Source]) -> list[Lot]:
    """Забрать лоты из всех включённых источников. Сломанный источник не роняет остальные."""
    new: list[Lot] = []
    for src in sources:
        if not src.enabled:
            continue
        if getattr(src, "wants_keywords", False):
            src.keywords = await all_keywords(s)
        try:
            new += await upsert_lots(s, await src.fetch())
        except Exception:
            log.exception("Источник %s упал", src.name)
        if hasattr(src, "fetch_awards"):
            try:
                from app.services.insights import upsert_awards

                await upsert_awards(s, await src.fetch_awards())
            except Exception:
                log.exception("Итоги закупок из %s не загрузились", src.name)
    await s.commit()
    return new


async def collect_fresh(s: AsyncSession, sources: list[Source]) -> list[Lot]:
    """Быстрый сбор: первые страницы источников, умеющих fetch_fresh (раз в несколько минут)."""
    new: list[Lot] = []
    for src in sources:
        if not src.enabled or not hasattr(src, "fetch_fresh"):
            continue
        if getattr(src, "wants_keywords", False):
            src.keywords = await all_keywords(s)
            if not src.keywords:
                continue
        try:
            new += await upsert_lots(s, await src.fetch_fresh())
        except Exception:
            log.exception("Быстрый сбор %s упал", src.name)
    await s.commit()
    return new


# ── Подбор ─────────────────────────────────────────────────

async def match_user(s: AsyncSession, user: User, lots: list[Lot]) -> list[Match]:
    """Подбирает лоты пользователю: сначала по словам, потом (если есть ключ) уточняет ИИ."""
    profile = profile_of(user)
    already = set(
        (await s.execute(select(Match.lot_id).where(Match.user_id == user.id, Match.lot_id.in_([l.id for l in lots])))).scalars()
    )
    candidates: list[tuple[Lot, int, str]] = []
    for lot in lots:
        if lot.id in already:
            continue
        hit = keyword_hit(lot.title, lot.description, lot.amount, lot.region, profile)
        if hit:
            candidates.append((lot, hit.score, hit.reason))

    # Экономия токенов: ИИ уточняет только лучшие совпадения и только при активном тарифе
    ai_scores: dict = {}
    if ai.available():
        from app.services.billing import current_plan

        if current_plan(user)[1]:
            top = sorted(candidates, key=lambda c: -c[1])[: config.AI_SCORE_LIMIT]
            ai_scores = await score_lots(user, [c[0] for c in top])
    created: list[Match] = []
    for lot, score, reason in candidates:
        by_ai = lot.id in ai_scores
        if by_ai:
            score, reason = ai_scores[lot.id]
            if score < AI_MIN_SCORE:
                continue
        m = Match(user_id=user.id, lot_id=lot.id, score=score, reason=reason, by_ai=by_ai)
        s.add(m)
        created.append(m)
    await s.flush()
    return created


async def match_new_lots(s: AsyncSession, lots: list[Lot]) -> dict[int, list[Match]]:
    """Новые лоты → подбор для всех настроенных пользователей. {user_id: [Match]}"""
    if not lots:
        return {}
    users = (await s.execute(select(User).where(User.onboarded.is_(True)))).scalars().all()
    out: dict[int, list[Match]] = {}
    for u in users:
        ms = await match_user(s, u, lots)
        if ms:
            out[u.id] = ms
    await s.commit()
    return out


async def rematch_user(s: AsyncSession, user: User, limit: int = 3000) -> list[Match]:
    """После смены профиля: убираем старые подборки (кроме тех, что в воронке) и подбираем заново."""
    in_pipeline = select(PipelineItem.lot_id).where(PipelineItem.user_id == user.id)
    old = (await s.execute(select(Match).where(Match.user_id == user.id, Match.lot_id.not_in(in_pipeline)))).scalars().all()
    for m in old:
        await s.delete(m)
    await s.flush()
    lots = (await s.execute(select(Lot).where(open_condition()).order_by(Lot.fetched_at.desc()).limit(limit))).scalars().all()
    created = await match_user(s, user, list(lots))
    await s.commit()
    return created


# ── Лента и статистика ─────────────────────────────────────

async def feed(
    s: AsyncSession, user: User, *, query: str = "", only_urgent: bool = False,
    include_closed: bool = False, limit: int = 50, offset: int = 0,
) -> list[tuple[Lot, Match, str | None]]:
    """Подобранные лоты: [(лот, совпадение, стадия в воронке или None)], лучшие и срочные сверху."""
    stmt = (
        select(Lot, Match, PipelineItem.stage)
        .join(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
        .outerjoin(PipelineItem, and_(PipelineItem.lot_id == Lot.id, PipelineItem.user_id == user.id))
        .where(Match.hidden.is_(False))
    )
    if not include_closed:
        stmt = stmt.where(open_condition())
    if only_urgent:
        stmt = stmt.where(Lot.deadline <= utcnow() + timedelta(days=config.URGENT_DAYS))
    if query:
        like = f"%{query.lower()}%"
        stmt = stmt.where(or_(func.lower(Lot.title).like(like), func.lower(Lot.customer).like(like)))
    stmt = stmt.order_by(Match.score.desc(), Lot.deadline.asc()).limit(limit).offset(offset)
    return [(l, m, st) for l, m, st in (await s.execute(stmt)).all()]


async def search(s: AsyncSession, text: str, profile: Profile | None = None, limit: int = 10) -> list[tuple[Lot, int, str]]:
    """Разовый поиск по открытым лотам (не сохраняется)."""
    p = profile or Profile()
    p = Profile(keywords=[text], min_amount=p.min_amount, max_amount=p.max_amount, regions=p.regions)
    lots = (await s.execute(select(Lot).where(open_condition(), Lot.source != "upload")
                             .order_by(Lot.fetched_at.desc()).limit(5000))).scalars().all()
    hits = []
    for lot in lots:
        h = keyword_hit(lot.title, lot.description, lot.amount, lot.region, p)
        if h:
            hits.append((lot, h.score, h.reason))
    hits.sort(key=lambda x: (-x[1], aware(x[0].deadline) or datetime.max.replace(tzinfo=timezone.utc)))
    return hits[:limit]


async def user_stats(s: AsyncSession, user: User) -> dict:
    base = (
        select(func.count(Lot.id), func.coalesce(func.sum(Lot.amount), 0))
        .join(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
        .where(open_condition(), Match.hidden.is_(False))
    )
    count, total = (await s.execute(base)).one()
    urgent = (await s.execute(base.where(Lot.deadline <= utcnow() + timedelta(days=config.URGENT_DAYS)))).one()[0]
    stages = dict((await s.execute(
        select(PipelineItem.stage, func.count()).where(PipelineItem.user_id == user.id).group_by(PipelineItem.stage)
    )).all())
    return {"open": int(count), "total_amount": float(total or 0), "urgent": int(urgent), "pipeline": stages}


# ── Воронка ────────────────────────────────────────────────

async def set_stage(s: AsyncSession, user: User, lot_id: int, stage: str | None, note: str | None = None) -> PipelineItem | None:
    """stage=None — убрать из воронки."""
    item = (await s.execute(select(PipelineItem).where(PipelineItem.user_id == user.id, PipelineItem.lot_id == lot_id))).scalar_one_or_none()
    if stage is None:
        if item:
            await s.delete(item)
        await s.commit()
        return None
    if item is None:
        item = PipelineItem(user_id=user.id, lot_id=lot_id, stage=stage, note=note or "")
        s.add(item)
    else:
        item.stage = stage
        if note is not None:
            item.note = note
    await s.commit()
    return item


async def pipeline(s: AsyncSession, user: User) -> list[tuple[Lot, PipelineItem, Match | None]]:
    stmt = (
        select(Lot, PipelineItem, Match)
        .join(PipelineItem, and_(PipelineItem.lot_id == Lot.id, PipelineItem.user_id == user.id))
        .outerjoin(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
        .order_by(PipelineItem.updated_at.desc())
    )
    return [(l, p, m) for l, p, m in (await s.execute(stmt)).all()]


async def get_or_create_user(s: AsyncSession, tg_id: int, name: str = "", username: str = "") -> User:
    user = await s.get(User, tg_id)
    if user is None:
        user = User(id=tg_id, name=name, username=username or "")
        s.add(user)
        await s.commit()
    elif name and user.name != name:
        user.name = name
        await s.commit()
    return user


async def fix_keywords(s: AsyncSession) -> int:
    """Чистит направления, сохранённые обрывками (по одной цифре/букве) или строкой вместо списка."""
    from app.services.matcher import parse_keywords

    fixed = 0
    for u in (await s.execute(select(User))).scalars():
        raw = u.keywords or []
        clean = parse_keywords(raw)
        if not isinstance(raw, list) or clean != list(raw):
            u.keywords = clean
            fixed += 1
    if fixed:
        await s.commit()
    return fixed


async def purge_demo(s: AsyncSession) -> int:
    """Демо выключено — убираем вымышленные закупки и итоги вместе со ссылками на них."""
    from sqlalchemy import delete

    from app.db import Analysis, Award, GeneratedDoc, Match, PipelineItem

    ids = list((await s.execute(select(Lot.id).where(Lot.is_demo.is_(True)))).scalars())
    if ids:
        for model in (Match, PipelineItem, Analysis, GeneratedDoc):
            await s.execute(delete(model).where(model.lot_id.in_(ids)))
        await s.execute(delete(Lot).where(Lot.id.in_(ids)))
    awards = (await s.execute(delete(Award).where(Award.is_demo.is_(True)))).rowcount or 0
    await s.commit()
    return len(ids) + awards
