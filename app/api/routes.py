"""REST API для Mini App."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, db
from app.ai import client as ai
from app.api.auth import current_tg_user
from app.db import DOC_KINDS, STAGES, Analysis, Document, Lot, Match, Monitor, PipelineItem, UpgradeRequest, User, aware, utcnow
from app import runtime
from app.services import billing, documents as docsvc, login
from app.services import core, monitors
from app.services.analyses import get_analysis, get_or_create_analysis
from app.services.matcher import parse_keywords
from app.sources.registry import SOURCES

router = APIRouter(prefix="/api")


async def get_session():
    async with db.Session() as s:
        yield s


async def get_user(tg: dict = Depends(current_tg_user), s: AsyncSession = Depends(get_session)) -> User:
    user = await core.get_or_create_user(s, tg["id"], tg.get("name", ""), tg.get("username", ""))
    if tg.get("v") is not None and tg["v"] != (user.token_version or 0):
        raise HTTPException(401, "Сессия завершена, войдите заново")
    return user


# ── Вход по коду ───────────────────────────────────────────

@router.get("/auth/config")
async def auth_config():
    return {"bot": runtime.bot_username}


@router.post("/auth/start")
async def auth_start(s: AsyncSession = Depends(get_session)):
    if not runtime.bot_username:
        raise HTTPException(503, "Вход временно недоступен: Telegram-бот не подключён")
    lc = await login.start(s)
    return {"nonce": lc.nonce, "code": lc.code, "bot": runtime.bot_username,
            "link": f"https://t.me/{runtime.bot_username}?start=login_{lc.nonce}", "expires_in": int(login.TTL.total_seconds())}


@router.get("/auth/poll")
async def auth_poll(nonce: str, s: AsyncSession = Depends(get_session)):
    try:
        token = await login.poll(s, nonce)
    except PermissionError as e:
        raise HTTPException(410, str(e))
    return {"token": token}


@router.post("/auth/logout-all")
async def logout_all(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    u.token_version = (u.token_version or 0) + 1
    await s.commit()
    return {"ok": True}


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
        "id": u.id, "name": u.name, "username": u.username, "company_bin": u.company_bin, "company_name": u.company_name,
        "keywords": u.keywords or [], "about": u.about, "min_amount": u.min_amount, "max_amount": u.max_amount,
        "regions": u.regions or [], "notify": u.notify, "onboarded": u.onboarded,
    }


@router.get("/me")
async def me(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    return {
        "user": user_json(u), "stats": await core.user_stats(s, u),
        "plan": billing.plan_json(u, await billing.usage(s, u)),
        "bot": runtime.bot_username,
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
    except billing.LimitError as e:
        raise HTTPException(402, str(e))
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


# ── Разборы ────────────────────────────────────────────────

@router.get("/analyses")
async def list_analyses(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(
        select(Analysis, Lot).join(Lot, Lot.id == Analysis.lot_id).where(Analysis.user_id == u.id).order_by(Analysis.created_at.desc())
    )).all()
    return {"items": [
        {"lot": lot_json(l), "verdict": a.verdict, "score": a.data.get("score"), "summary": a.data.get("summary", ""),
         "created_at": aware(a.created_at).isoformat()} for a, l in rows
    ]}


@router.get("/lots/{lot_id}/readiness")
async def lot_readiness(lot_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    lot = await _lot_or_404(s, lot_id)
    a = await get_analysis(s, u, lot_id)
    required = [str(x) for x in (a.data.get("documents") or [])] if a else []
    return await docsvc.readiness(s, u.id, required, lot.deadline)


# ── Документы компании ─────────────────────────────────────

def _parse_date(value: str | None):
    if not value:
        return None
    from datetime import datetime as dt

    try:
        return dt.fromisoformat(value).replace(hour=12, tzinfo=config.TZ)
    except ValueError:
        raise HTTPException(400, "Дата в формате ГГГГ-ММ-ДД")


@router.get("/documents")
async def list_documents(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    docs = await docsvc.list_docs(s, u.id)
    return {"items": [docsvc.doc_json(d) for d in docs], "kinds": DOC_KINDS}


@router.post("/documents")
async def upload_document(
    file: UploadFile | None = File(default=None), title: str = Form(...), kind: str = Form("other"),
    issued_at: str = Form(""), expires_at: str = Form(""), note: str = Form(""),
    u: User = Depends(get_user), s: AsyncSession = Depends(get_session),
):
    try:
        await billing.check(s, u, "documents")
    except billing.LimitError as e:
        raise HTTPException(402, str(e))
    content, filename, mime = None, "", "application/octet-stream"
    if file is not None and file.filename:
        import os

        filename = os.path.basename(file.filename)[:300]
        if os.path.splitext(filename)[1].lower() not in docsvc.ALLOWED_EXT:
            raise HTTPException(400, "Допустимы PDF, Word, Excel, изображения и архивы")
        content = await file.read(docsvc.MAX_SIZE + 1)
        if len(content) > docsvc.MAX_SIZE:
            raise HTTPException(400, "Файл больше 15 МБ")
        mime = file.content_type or mime
    d = Document(user_id=u.id, title=title.strip()[:300] or filename or "Документ", kind=kind if kind in DOC_KINDS else "other",
                 filename=filename, mime=mime, size=len(content or b""), content=content,
                 issued_at=_parse_date(issued_at), expires_at=_parse_date(expires_at), note=note[:2000])
    s.add(d)
    await s.commit()
    return docsvc.doc_json(d)


class DocumentIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    kind: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None
    note: str | None = Field(default=None, max_length=2000)


async def _own_doc(s: AsyncSession, u: User, doc_id: int) -> Document:
    d = await s.get(Document, doc_id)
    if not d or d.user_id != u.id:
        raise HTTPException(404, "Документ не найден")
    return d


@router.put("/documents/{doc_id}")
async def update_document(doc_id: int, body: DocumentIn, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    d = await _own_doc(s, u, doc_id)
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"]:
        d.title = data["title"]
    if "kind" in data and data["kind"] in DOC_KINDS:
        d.kind = data["kind"]
    if "note" in data:
        d.note = data["note"] or ""
    for f in ("issued_at", "expires_at"):
        if f in data:
            setattr(d, f, _parse_date(data[f]))
            if f == "expires_at":
                d.reminded_30d = d.reminded_7d = False
    await s.commit()
    return docsvc.doc_json(d)


@router.get("/documents/{doc_id}/file")
async def download_document(doc_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    d = await _own_doc(s, u, doc_id)
    if not d.content:
        raise HTTPException(404, "Файл не прикреплён")
    from urllib.parse import quote

    return Response(d.content, media_type=d.mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(d.filename or 'document')}"})


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    d = await _own_doc(s, u, doc_id)
    await s.delete(d)
    await s.commit()
    return {"ok": True}


# ── Сроки ──────────────────────────────────────────────────

@router.get("/deadlines")
async def deadlines(u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    rows = await core.pipeline(s, u)
    lots = [lot_json(l, m, p.stage) for l, p, m in rows if p.stage in ("saved", "preparing") and l.deadline]
    lots.sort(key=lambda x: x["deadline"])
    docs = [docsvc.doc_json(d) for d in await docsvc.list_docs(s, u.id) if docsvc.doc_status(d) in ("expiring", "expired")]
    return {"lots": lots, "documents": docs}


# ── Тарифы ─────────────────────────────────────────────────

class UpgradeIn(BaseModel):
    plan: str
    months: int = Field(default=1, ge=1, le=12)
    contact: str = Field(default="", max_length=200)


@router.post("/billing/request")
async def billing_request(body: UpgradeIn, u: User = Depends(get_user), s: AsyncSession = Depends(get_session)):
    if body.plan not in billing.PAID:
        raise HTTPException(400, "Неизвестный тариф")
    r = UpgradeRequest(user_id=u.id, plan=body.plan, months=body.months, contact=body.contact)
    s.add(r)
    await s.commit()
    p = billing.PLANS[body.plan]
    who = f"{u.name or ''} @{u.username}" if u.username else (u.name or str(u.id))
    await runtime.notify_admins(
        f"Заявка на тариф #{r.id}\n{who} (ID {u.id})\nКомпания: {u.company_name or '—'}, БИН {u.company_bin or '—'}\n"
        f"Тариф: {p.name}, {body.months} мес., {p.price * body.months:,} ₸\nКонтакт: {body.contact or '—'}\n\n"
        f"Подключить: /grant {u.id} {body.plan} {body.months}".replace(",", " ")
    )
    return {"ok": True, "id": r.id, "payment_info": config.PAYMENT_INFO}
