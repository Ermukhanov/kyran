"""Итоги закупок (договоры) с открытого API zakup.gov.kz — без токена.

Адреса, с которых берёт данные сам сайт платформы (проверено с сервера 18.11.2026):
  /public/contracts/?supplier_organization_id=&customer_organization_id=&enstru_code__startswith=
                     &sign_date__gte=&sign_date__lte=&system_id=&limit<=100&offset<10000
  /public/contracts/{id}/          — карточка: БИН поставщика и заказчика, плановая сумма (buy_total_with_vat)
  /public/organizations/?q=<БИН>   — id организации по БИН
Текстового поиска по договорам нет: похожие ищем по коду ЕНС ТРУ, а по направлениям — через коды
открытых лотов пользователя. Загруженное кэшируется в таблице awards; повторно срез не грузим
чаще, чем раз в FRESH.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Award, FetchMark, OrgRef, aware, utcnow
from app.sources.goszakup import parse_dt, to_float

log = logging.getLogger(__name__)

BASE = os.environ.get("ZAKUP_BASE", "https://zakup.gov.kz/api/core/api")
SITE = "https://zakup.gov.kz"
PAGE = 100
FRESH = timedelta(hours=12)
DETAILS_PER_SLICE = 30   # сколько карточек договоров дочитывать ради плановой суммы и БИН
PAUSE = 0.15
HEADERS = {"Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9",
           "User-Agent": "Kyran/1.0 (поиск и аналитика закупок для поставщиков)"}
CANCELLED = ("расторг", "аннулир", "отмен")


def enabled() -> bool:
    return os.environ.get("ZAKUP_ENABLED", "true").lower() in ("1", "true", "yes")


class Client:
    def __init__(self) -> None:
        self._session = None

    async def __aenter__(self):
        import aiohttp

        self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        await self._session.close()

    async def get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(3):
            await asyncio.sleep(PAUSE if attempt == 0 else 1.5 * attempt)
            try:
                async with self._session.get(BASE + path, params={k: v for k, v in (params or {}).items() if v not in (None, "")}) as r:
                    if r.status in (429, 500, 502, 503, 504):
                        continue
                    if r.status != 200:
                        raise RuntimeError(f"zakup {path}: HTTP {r.status}")
                    return await r.json(content_type=None)
            except asyncio.TimeoutError:
                continue
        raise RuntimeError(f"zakup {path}: нет ответа")

    async def list(self, path: str, params: dict, max_items: int) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while len(items) < max_items and offset < 10000:
            data = await self.get(path, {**params, "limit": PAGE, "offset": offset})
            rows = data.get("results") if isinstance(data, dict) else data
            if not rows:
                break
            items += [r for r in rows if isinstance(r, dict)]
            if len(rows) < PAGE or not (isinstance(data, dict) and data.get("next")):
                break
            offset += PAGE
        return items[:max_items]


# ── Кэш ────────────────────────────────────────────────────

async def _fresh(s: AsyncSession, key: str, ttl: timedelta = FRESH) -> bool:
    m = await s.get(FetchMark, key)
    return bool(m and aware(m.fetched_at) > utcnow() - ttl)


async def _mark(s: AsyncSession, key: str) -> None:
    m = await s.get(FetchMark, key)
    if m:
        m.fetched_at = utcnow()
    else:
        s.add(FetchMark(key=key, fetched_at=utcnow()))


async def remember_org(s: AsyncSession, org: dict | None) -> None:
    if not isinstance(org, dict) or not org.get("id"):
        return
    bin_ = "".join(ch for ch in str(org.get("iin_bin") or org.get("bin") or "") if ch.isdigit())
    if len(bin_) != 12:
        return
    ref = await s.get(OrgRef, int(org["id"]))
    name = str(org.get("name_ru") or org.get("name") or "")[:500]
    if ref:
        ref.bin, ref.name, ref.updated_at = bin_, name or ref.name, utcnow()
    else:
        s.add(OrgRef(org_id=int(org["id"]), bin=bin_, name=name, updated_at=utcnow()))


async def bin_of(s: AsyncSession, org_id: int | None) -> str:
    if not org_id:
        return ""
    ref = await s.get(OrgRef, int(org_id))
    return ref.bin if ref else ""


async def org_by_bin(s: AsyncSession, c: Client, bin_: str) -> OrgRef | None:
    ref = (await s.execute(select(OrgRef).where(OrgRef.bin == bin_))).scalars().first()
    if ref:
        return ref
    data = await c.get("/public/organizations/", {"q": bin_, "limit": 5})
    for row in (data.get("results") if isinstance(data, dict) else data) or []:
        if isinstance(row, dict) and str(row.get("iin_bin") or row.get("bin") or "") == bin_:
            await remember_org(s, row)
            await s.flush()
            return await s.get(OrgRef, int(row["id"]))
    return None


# ── Договоры → итоги ───────────────────────────────────────

def _status(row: dict) -> str:
    name = str(row.get("status_name") or "").lower()
    return "failed" if any(w in name for w in CANCELLED) else "done"


async def upsert_contracts(s: AsyncSession, rows: list[dict]) -> list[Award]:
    if not rows:
        return []
    ids = [str(r.get("id")) for r in rows if r.get("id")]
    existing = {a.ext_id: a for a in (await s.execute(select(Award).where(Award.source == "zakup", Award.ext_id.in_(ids)))).scalars()}
    out = []
    for r in rows:
        cid = str(r.get("id") or "")
        if not cid:
            continue
        a = existing.get(cid)
        if a is None:
            a = Award(source="zakup", ext_id=cid, is_demo=False)
            s.add(a)
            existing[cid] = a
        a.title = str(r.get("announcement_name") or r.get("description_ru") or "Договор")[:2000]
        a.customer = str(r.get("customer_organization_name") or "")[:500]
        a.winner = str(r.get("supplier_organization_name") or "")[:500]
        a.price = to_float(r.get("contract_price_with_vat"))
        a.method = str(r.get("purchase_method_name") or "")[:200]
        a.awarded_at = parse_dt(r.get("sign_date")) or parse_dt(r.get("created"))
        a.status = _status(r)
        a.supplier_org_id = r.get("supplier_organization_id")
        a.customer_org_id = r.get("customer_organization_id")
        codes = r.get("enstru_code") or []
        a.enstru = str(codes[0])[:40] if codes else ""
        a.url = ""
        if not a.winner_bin:
            a.winner_bin = await bin_of(s, a.supplier_org_id)
        if not a.customer_bin:
            a.customer_bin = await bin_of(s, a.customer_org_id)
        out.append(a)
    await s.flush()
    return out


async def enrich(s: AsyncSession, c: Client, awards: list[Award], limit: int = DETAILS_PER_SLICE) -> None:
    """Карточки договоров: плановая сумма (для снижения цены) и БИН сторон. Только для свежих и без данных."""
    todo = [a for a in sorted(awards, key=lambda x: aware(x.awarded_at) or utcnow(), reverse=True) if a.amount is None][:limit]
    for a in todo:
        try:
            d = await c.get(f"/public/contracts/{a.ext_id}/")
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        await remember_org(s, d.get("supplier_organization"))
        await remember_org(s, d.get("customer_organization"))
        planned = to_float(d.get("buy_total_with_vat"))
        a.amount = planned if planned and a.price and planned >= a.price * 0.5 else (planned or 0.0)
        sup, cus = d.get("supplier_organization") or {}, d.get("customer_organization") or {}
        a.winner_bin = str(sup.get("iin_bin") or a.winner_bin or "")[:12]
        a.customer_bin = str(cus.get("iin_bin") or a.customer_bin or "")[:12]
    await s.flush()


def _since(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


async def _slice(s: AsyncSession, key: str, params: dict, max_items: int, ttl: timedelta = FRESH, details: int = DETAILS_PER_SLICE) -> int:
    if not enabled() or await _fresh(s, key, ttl):
        return 0
    try:
        async with Client() as c:
            rows = await c.list("/public/contracts/", params, max_items)
            awards = await upsert_contracts(s, rows)
            await enrich(s, c, awards, details)
        await _mark(s, key)
        await s.commit()
        log.info("zakup: %s — %d договоров", key, len(rows))
        return len(rows)
    except Exception as e:
        await s.rollback()
        log.warning("zakup: срез %s не загружен: %s", key, e)
        return 0


async def ensure_enstru(s: AsyncSession, code: str, days: int = 365) -> None:
    """Похожие договоры по коду ЕНС ТРУ (первые 6 цифр — товарная группа)."""
    prefix = "".join(ch for ch in code if ch.isdigit())[:6]
    if len(prefix) == 6:
        await _slice(s, f"enstru:{prefix}:{days}", {"enstru_code__startswith": prefix, "sign_date__gte": _since(days)}, 300)


async def ensure_customer(s: AsyncSession, org_id: int | None, days: int = 730) -> None:
    if org_id:
        await _slice(s, f"customer:{org_id}", {"customer_organization_id": org_id, "sign_date__gte": _since(days)}, 200, details=15)


async def ensure_supplier(s: AsyncSession, bin_: str, days: int = 365, ttl: timedelta = FRESH) -> OrgRef | None:
    """Договоры компании-поставщика по БИН. Возвращает организацию (или None, если на платформе не найдена)."""
    if not enabled():
        return None
    try:
        async with Client() as c:
            org = await org_by_bin(s, c, bin_)
        await s.commit()
    except Exception as e:
        log.warning("zakup: организация %s не найдена: %s", bin_, e)
        return None
    if not org:
        return None
    await _slice(s, f"supplier:{org.org_id}:{days}", {"supplier_organization_id": org.org_id, "sign_date__gte": _since(days)},
                 500, ttl=ttl, details=20)
    # победителю этих договоров точно соответствует этот БИН
    for a in (await s.execute(select(Award).where(Award.supplier_org_id == org.org_id, Award.winner_bin == ""))).scalars():
        a.winner_bin = org.bin
    await s.commit()
    return org


async def ensure_recent_for_codes(s: AsyncSession, codes: list[str], days: int = 7) -> None:
    for code in codes[:6]:
        prefix = "".join(ch for ch in code if ch.isdigit())[:6]
        if len(prefix) == 6:
            await _slice(s, f"recent:{prefix}:{days}", {"enstru_code__startswith": prefix, "sign_date__gte": _since(days)}, 300,
                         ttl=timedelta(hours=6), details=20)


# ── Коды ЕНС ТРУ по словам ─────────────────────────────────
_codes_cache: dict[str, tuple[datetime, list[str]]] = {}


async def codes_for_query(q: str, top: int = 4) -> list[str]:
    """Самые частые товарные группы (6 цифр ЕНС ТРУ) среди ВСЕХ лотов по запросу,
    включая завершённые. Работает, даже когда открытых лотов сейчас нет."""
    from collections import Counter

    q = " ".join((q or "").split()).lower()[:120]
    if not q or not enabled():
        return []
    hit = _codes_cache.get(q)
    if hit and hit[0] > utcnow() - FRESH:
        return hit[1]
    codes: Counter = Counter()
    try:
        async with Client() as c:
            data = await c.get("/public/lots/", {"q": q, "limit": PAGE, "offset": 0})
        rows = data.get("results") if isinstance(data, dict) else data
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            keys = [r.get("enstru_key")] + [e.get("code") or e.get("enstru_code") or e.get("key")
                                            for e in (r.get("enstrus") or []) if isinstance(e, dict)]
            for k in keys:
                digits = "".join(ch for ch in str(k or "") if ch.isdigit())[:6]
                if len(digits) == 6:
                    codes[digits] += 1
                    break
    except Exception as e:
        log.warning("zakup: коды для «%s» не получены: %s", q, e)
        return []
    out = [code for code, _ in codes.most_common(top)]
    _codes_cache[q] = (utcnow(), out)
    log.info("zakup: «%s» -> коды ЕНС ТРУ %s", q, out)
    return out
