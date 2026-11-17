"""REST API для Mini App."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, db
from app.ai import client as ai
from app.api.auth import current_tg_user
from app.db import STAGES, Lot, Match, Monitor, PipelineItem, User, aware
from app.services import core, monitors
from app.services.analyses import get_analysis, get_or_create_analysis
from app.services.matcher import parse_keywords
from app.sources.registry import SOURCES

router = APIRouter(prefix="/api")


async def get_session():
    async with db.Session() as s:
        yield s


async def get_user(tg: dict = Depends(current_tg_user), s: AsyncSession = Depends(get_session)) -> User:
    return await core.get_or_create_user(s, tg["id"], tg.get("name", ""), tg.get("username", ""))


def lot_json(lot: Lot, match: Match | None = None, stage: str | None = None) -> dict[str, Any]:
    deadline = aware(lot.deadline)
    hours_left = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600 if deadline else None
    return {
        "id": lot.id, "source": lot.source, "title": lot.title, "description": lot.description,
        "customer": lot.customer, "amount": lot.amount, "region": lot.region, "method": lot.method,
        "deadline": deadline.isoformat() if deadline else None, "hours_left": hours_left,
        "url": lot.url, "documents": lot.documents or [], "is_demo": lot.is_demo,
        "score": match.score if match else None, "reason": match.reason if match else "",
        "by_ai": match.by_ai if match else False, "stage": stage,
    }


def user_json(u: User) -> dict[str, Any]:
    return {
        "id": u.id, "name": u.name, "company_bin": u.company_bin, "company_name": u.company_name,
        "keywords": u.keywords or [], "about": u.about, "min_amount": u.min_amount, "max_amount": u.max_amount,
        "regions": u.regions or [], "notify": u.notify, "onboarded": u.onboarded,
    }


@router.get("/me")
async def me(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    return {
        "user": user_json(u), "stats": await core.user_stats(s, u),
        "features": {
            "ai": ai.available(), "demo": config.DEMO_MODE,
            "sources": {src.name: src.enabled for src in SOURCES},
        },
    }


class ProfileIn(BaseModel):
    keywords: list[str] | str | None = None
    about: str | None = Field(default=None, max_length=2000)
    company_bin: str | None = Field(default=None, max_length=12)
    company_name: str | None = Field(default=None, max_length=300)
    min_amount: float | None = None
    max_amount: float | None = None
    regions: list[str] | None = None
    notify: bool | None = None


@router.put("/me")
async def update_me(body: ProfileIn, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    data = body.model_dump(exclude_unset=True)
    rematch = False
    if "keywords" in data:
        kw = data.pop("keywords") or []
        u.keywords = parse_keywords(kw if isinstance(kw, str) else ", ".join(kw))
        rematch = True
    for field in ("min_amount", "max_amount", "regions"):
        if field in data:
            setattr(u, field, data.pop(field))
            rematch = True
    for field, value in data.items():
        setattr(u, field, value)
    if u.keywords:
        u.onboarded = True
    await s.commit()
    if rematch:
        await core.rematch_user(s, u)
    return {"user": user_json(u), "stats": await core.user_stats(s, u)}


@router.get("/feed")
async def feed(q: str = "", urgent: bool = False, offset: int = 0,
               u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    rows = await core.feed(s, u, query=q.strip(), only_urgent=urgent, limit=40, offset=offset)
    return {"items": [lot_json(l, m, st) for l, m, st in rows]}


@router.get("/search")
async def search(q: str, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    if len(q.strip()) < 3:
        return {"items": []}
    hits = await core.search(s, q.strip(), core.profile_of(u), limit=30)
    items = []
    for lot, score, reason in hits:
        d = lot_json(lot)
        d["score"], d["reason"] = score, reason
        items.append(d)
    return {"items": items}


async def _lot_or_404(s: AsyncSession, lot_id: int) -> Lot:
    lot = await s.get(Lot, lot_id)
    if not lot:
        raise HTTPException(404, "Тендер не найден")
    return lot


@router.get("/lots/{lot_id}")
async def lot_detail(lot_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    lot = await _lot_or_404(s, lot_id)
    match = (await s.execute(select(Match).where(Match.user_id == u.id, Match.lot_id == lot_id))).scalar_one_or_none()
    item = (await s.execute(select(PipelineItem).where(PipelineItem.user_id == u.id, PipelineItem.lot_id == lot_id))).scalar_one_or_none()
    analysis = await get_analysis(s, u, lot_id)
    return {
        "lot": lot_json(lot, match, item.stage if item else None),
        "note": item.note if item else "",
        "analysis": analysis.data if analysis else None,
        "ai": ai.available(),
    }


@router.post("/lots/{lot_id}/analyze")
async def analyze(lot_id: int, refresh: bool = False, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    lot = await _lot_or_404(s, lot_id)
    try:
        a = await get_or_create_analysis(s, u, lot, refresh=refresh)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        raise HTTPException(502, "ИИ не смог разобрать тендер. Попробуйте ещё раз через минуту.")
    return {"analysis": a.data}


class StageIn(BaseModel):
    stage: str | None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/lots/{lot_id}/stage")
async def set_stage(lot_id: int, body: StageIn, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    await _lot_or_404(s, lot_id)
    if body.stage is not None and body.stage not in STAGES:
        raise HTTPException(400, f"Стадия должна быть одной из: {', '.join(STAGES)}")
    item = await core.set_stage(s, u, lot_id, body.stage, body.note)
    return {"stage": item.stage if item else None}


@router.post("/lots/{lot_id}/hide")
async def hide(lot_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    m = (await s.execute(select(Match).where(Match.user_id == u.id, Match.lot_id == lot_id))).scalar_one_or_none()
    if m:
        m.hidden = True
        await s.commit()
    return {"ok": True}


@router.get("/pipeline")
async def get_pipeline(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    rows = await core.pipeline(s, u)
    columns = {st: [] for st in STAGES}
    for lot, item, match in rows:
        d = lot_json(lot, match, item.stage)
        d["note"] = item.note
        columns.setdefault(item.stage, []).append(d)
    return {"columns": columns}


@router.get("/monitors")
async def list_monitors(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(Monitor).where(Monitor.user_id == u.id, Monitor.active.is_(True)).order_by(Monitor.created_at.desc()))).scalars().all()
    return {"items": [
        {"id": m.id, "url": m.url, "title": m.title, "last_checked": aware(m.last_checked).isoformat() if m.last_checked else None,
         "last_change": aware(m.last_change).isoformat() if m.last_change else None, "error": m.last_error}
        for m in rows
    ], "ai": ai.available()}


class MonitorIn(BaseModel):
    url: str = Field(min_length=4, max_length=2000)


@router.post("/monitors")
async def add_monitor(body: MonitorIn, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    try:
        m = await monitors.add_monitor(s, u, body.url)
    except Exception as e:
        raise HTTPException(400, f"Не получилось открыть сайт: {e}")
    return {"id": m.id, "title": m.title}


@router.delete("/monitors/{monitor_id}")
async def delete_monitor(monitor_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    m = await s.get(Monitor, monitor_id)
    if not m or m.user_id != u.id:
        raise HTTPException(404, "Не найдено")
    m.active = False
    await s.commit()
    return {"ok": True}
