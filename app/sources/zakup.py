"""Единая платформа закупок zakup.gov.kz — открытый поиск лотов без токена.

Платформа объединяет площадки: Госзакупки, Самрук-Қазына, Митворк (Евразийский
электронный портал) и ФСМС. Поиск лотов, планов и объявлений на ней открыт без входа;
сайт сам берёт данные из публичного JSON:

    GET https://zakup.gov.kz/api/core/api/public/lots/?q=<запрос>&limit=50&offset=0
        &offer_end_date__gte=<ISO>   → {"results": [...], "next": "<url>"|null}

Это не официально документированный API, поэтому: вежливая частота (пауза между
запросами), запросы только по направлениям пользователей, понятные логи и выключатель
ZAKUP_ENABLED. Когда придёт токен Минфина, официальный источник goszakup работает рядом.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app import config
from app.sources.base import LotData
from app.sources.goszakup import parse_dt, pick, to_float

log = logging.getLogger(__name__)

API = os.environ.get("ZAKUP_API", "https://zakup.gov.kz/api/core/api/public/lots/")
SITE = "https://zakup.gov.kz"
PAGE = 50
MAX_PAGES = int(os.environ.get("ZAKUP_MAX_PAGES", "4"))        # на одно направление
MAX_QUERIES = int(os.environ.get("ZAKUP_MAX_QUERIES", "60"))    # направлений за один сбор
PAUSE = float(os.environ.get("ZAKUP_PAUSE", "0.6"))             # секунд между запросами
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "User-Agent": "Kyran/1.0 (+https://kyran-production.up.railway.app; поиск закупок для поставщиков)",
    "Referer": SITE + "/",
}


def _first(items: Any, *keys: str) -> str:
    """Первое непустое значение из списка словарей/строк (customers, delivery_addresses)."""
    if not isinstance(items, list):
        return ""
    for it in items:
        if isinstance(it, str) and it.strip():
            return it.strip()
        if isinstance(it, dict):
            v = pick(it, *keys)
            if isinstance(v, dict):
                v = pick(v, "name_ru", "address_ru", "full_name_ru")
            if v not in (None, ""):
                return str(v).strip()
    return ""


def row_to_lot(row: dict[str, Any]) -> LotData | None:
    lot_id = pick(row, "id", "external_id")
    title = " ".join(str(pick(row, "name_ru", "description_ru", "announcement_name_ru", default="")).split())
    if not lot_id or not title:
        return None
    system = row.get("system") if isinstance(row.get("system"), dict) else {}
    ann = pick(row, "announcement_id")
    enstru = "; ".join(dict.fromkeys(
        str(e.get("short_description_ru")) for e in (row.get("enstrus") or []) if isinstance(e, dict) and e.get("short_description_ru")))
    desc_parts = [str(pick(row, "description_ru", default="")), enstru, str(pick(row, "announcement_name_ru", default=""))]
    description = " · ".join(dict.fromkeys(p.strip() for p in desc_parts if p and p.strip() and p.strip() != title))
    return LotData(
        source="zakup",
        ext_id=str(lot_id),
        title=title[:500],
        description=description[:2000],
        customer=str(pick(row, "organization_name", "customer_name", default="") or _first(row.get("customers"), "name_ru", "organization_name"))[:500],
        customer_bin=(str(pick(row, "organization_bin", "customer_bin", default="")) or _first(row.get("customers"), "bin", "biin", "iin"))[:12],
        amount=to_float(pick(row, "total_price", "amount", "sum")),
        region=(str(pick(row, "delivery_place", "kato_name", "region_name", default=""))
                or _first(row.get("delivery_addresses"), "kato_name_ru", "kato", "address_ru", "address", "name_ru"))[:200],
        method=str(pick(row, "purchase_method_name", default=""))[:200],
        deadline=parse_dt(pick(row, "offer_end_date")),
        published_at=parse_dt(pick(row, "announcement_publish_date", "publish_date")),
        url=f"{SITE}/announcement/{ann}#lot-{lot_id}" if ann else SITE,
        raw={"platform": system.get("name") or "", "platform_id": system.get("id"), "lot_number": pick(row, "lot_number"),
             "enstru": str(pick(row, "enstru_key", default="")),
             "customer_org_id": next((c.get("id") for c in (row.get("customers") or []) if isinstance(c, dict) and c.get("id")), None),
             "announcement_number": pick(row, "announcement_number"), "status": pick(row, "status_name")},
        is_demo=False,
    )


def is_open(row: dict[str, Any]) -> bool:
    status = row.get("status")
    if isinstance(status, dict) and status.get("is_active") is False:
        return False
    end = parse_dt(row.get("offer_end_date"))
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=config.TZ)
    return end >= datetime.now(timezone.utc)


class ZakupSource:
    """Открытый поиск zakup.gov.kz. Ищет по направлениям всех пользователей (keywords задаёт core.collect)."""
    name = "zakup"
    wants_keywords = True

    def __init__(self) -> None:
        self.keywords: list[str] = []
        self.last_ok: datetime | None = None
        self.last_error: str = ""

    @property
    def enabled(self) -> bool:
        return os.environ.get("ZAKUP_ENABLED", "true").lower() in ("1", "true", "yes")

    async def _page(self, session, url: str | None, q: str, offset: int) -> dict[str, Any]:
        if url is None:
            params = {"q": q, "limit": PAGE, "offset": offset, "offer_end_date__gte": datetime.now(timezone.utc).isoformat()}
            if not getattr(self, "no_ordering", False):
                params["ordering"] = "-announcement_publish_date"  # новые сверху
            async with session.get(API, params=params) as resp:
                if resp.status == 400 and "ordering" in params:
                    # площадка не принимает сортировку — работаем без неё
                    self.no_ordering = True
                    params.pop("ordering")
                    async with session.get(API, params=params) as resp2:
                        return await self._json(resp2)
                return await self._json(resp)
        async with session.get(url) as resp:
            return await self._json(resp)

    async def _json(self, resp) -> dict[str, Any]:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    async def search(self, queries: list[str], max_pages: int = MAX_PAGES) -> list[LotData]:
        if not self.enabled or not queries:
            return []
        import aiohttp

        found: dict[str, LotData] = {}
        errors = 0
        async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=40)) as s:
            for q in queries[:MAX_QUERIES]:
                url, offset = None, 0
                for _ in range(max_pages):
                    try:
                        data = await self._page(s, url, q, offset)
                    except Exception as e:
                        errors += 1
                        self.last_error = f"{type(e).__name__}: {e}"[:300]
                        log.warning("zakup.gov.kz: «%s» — %s", q, self.last_error)
                        break
                    rows = data.get("results") or []
                    if offset == 0:
                        log.info("zakup.gov.kz: «%s» — строк на первой странице: %d, всего: %s", q, len(rows), data.get("count"))
                    for row in rows:
                        if isinstance(row, dict) and is_open(row):
                            lot = row_to_lot(row)
                            if lot:
                                found.setdefault(lot.ext_id, lot)
                    nxt = data.get("next")
                    if not rows or not nxt or urlparse(str(nxt)).netloc != "zakup.gov.kz":
                        break
                    url, offset = str(nxt), offset + PAGE
                    await asyncio.sleep(PAUSE)
                await asyncio.sleep(PAUSE)
                if errors >= 3 and not found:
                    log.error("zakup.gov.kz недоступен — прекращаю сбор до следующего раза")
                    break
        if found:
            self.last_ok, self.last_error = datetime.now(timezone.utc), ""
        log.info("zakup.gov.kz: %d открытых лотов по %d запросам", len(found), min(len(queries), MAX_QUERIES))
        return list(found.values())

    async def fetch_fresh(self) -> list[LotData]:
        """Быстрый проход: только первая страница по каждому направлению."""
        lots = await self.search(self.keywords, max_pages=1)
        pubs = [l.published_at for l in lots if l.published_at]
        if pubs:
            log.info("zakup.gov.kz быстрый проход: %d лотов, самый свежий опубликован %s", len(lots), max(pubs).isoformat())
        return lots

    async def diagnose(self, q: str, with_date: bool = True) -> str:
        """Для админа: что реально отвечает площадка на запрос."""
        import aiohttp

        params = {"q": q, "limit": 5, "offset": 0}
        if with_date:
            params["offer_end_date__gte"] = datetime.now(timezone.utc).isoformat()
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=40)) as s:
                async with s.get(API, params=params) as resp:
                    body = await resp.text()
                    status, ctype = resp.status, resp.headers.get("Content-Type", "")
        except Exception as e:
            return f"Ошибка соединения: {type(e).__name__}: {e}"
        lines = [f"HTTP {status}, {ctype}, {len(body)} байт"]
        try:
            import json

            data = json.loads(body)
        except ValueError:
            return "\n".join(lines + ["Не JSON: " + body[:300]])
        if isinstance(data, dict):
            rows = data.get("results") or []
            lines.append(f"ключи: {', '.join(list(data.keys())[:10])}; count={data.get('count')}; строк: {len(rows)}")
            for r in rows[:3]:
                if isinstance(r, dict):
                    lines.append(f"• {str(r.get('name_ru'))[:60]} | конец: {r.get('offer_end_date')} | статус: {r.get('status_name') or r.get('status')} | открыт: {is_open(r)}")
            if rows and isinstance(rows[0], dict):
                lines.append("поля: " + ", ".join(sorted(rows[0].keys()))[:900])
        else:
            lines.append(f"Ответ — {type(data).__name__}")
        return "\n".join(lines)

    async def fetch(self) -> list[LotData]:
        return await self.search(self.keywords)


BASE = API.split("/public/")[0]  # https://zakup.gov.kz/api/core/api


async def probe() -> str:
    """Разведка открытых адресов платформы: договоры, организации, предметы. Только для логов."""
    import json

    import aiohttp

    out: list[str] = []

    async def get(path: str, params: dict | None = None):
        async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(BASE + path, params=params or {}) as r:
                body = await r.text()
                try:
                    return r.status, json.loads(body)
                except ValueError:
                    return r.status, body[:200]

    def keys(x) -> str:
        if isinstance(x, dict):
            return ", ".join(sorted(x.keys()))[:700]
        return type(x).__name__

    def short(x, n=500) -> str:
        return json.dumps(x, ensure_ascii=False, default=str)[:n]

    try:
        st, lots = await get("/public/lots/", {"q": "кабель", "limit": 1, "offer_end_date__gte": datetime.now(timezone.utc).isoformat()})
        row = (lots.get("results") or [{}])[0] if isinstance(lots, dict) else {}
        out.append(f"LOT customers={short(row.get('customers'), 300)} enstru_key={row.get('enstru_key')} delivery={short(row.get('delivery_addresses'), 200)}")
        st, cl = await get("/public/contracts/", {"limit": 2, "system_id": 1, "sign_date__gte": "2026-11-01"})
        out.append(f"CONTRACTS {st} count={cl.get('count') if isinstance(cl, dict) else '?'}")
        c = (cl.get("results") or [{}])[0] if isinstance(cl, dict) else {}
        out.append("contract keys: " + keys(c))
        out.append("contract sample: " + short(c, 900))
        if c.get("id"):
            st, det = await get(f"/public/contracts/{c['id']}/")
            out.append(f"DETAIL {st} keys: " + keys(det))
            out.append("detail orgs: " + short({k: det.get(k) for k in ("supplier_organization", "customer_organization", "name_ru", "contract_number")} if isinstance(det, dict) else det, 600))
            st, sub = await get(f"/public/contracts/{c['id']}/subjects/")
            first = (sub.get("results") if isinstance(sub, dict) else sub) or [{}]
            out.append(f"SUBJECTS {st} first: " + short(first[0], 700))
            sup = c.get("supplier_organization_id")
            if sup:
                st, bysup = await get("/public/contracts/", {"limit": 1, "supplier_organization_id": sup})
                out.append(f"BY SUPPLIER {st} count={bysup.get('count') if isinstance(bysup, dict) else bysup}")
            codes = c.get("enstru_code") or []
            if codes:
                st, byc = await get("/public/contracts/", {"limit": 1, "enstru_code__startswith": str(codes[0])[:6], "sign_date__gte": "2026-01-01"})
                out.append(f"BY ENSTRU {str(codes[0])[:6]} {st} count={byc.get('count') if isinstance(byc, dict) else byc}")
        st, q = await get("/public/contracts/", {"limit": 1, "q": "кабель", "sign_date__gte": "2026-01-01"})
        out.append(f"CONTRACTS q=кабель {st} count={q.get('count') if isinstance(q, dict) else q}")
        st, q2 = await get("/public/contracts/", {"limit": 1, "search": "кабель", "sign_date__gte": "2026-01-01"})
        out.append(f"CONTRACTS search=кабель {st} count={q2.get('count') if isinstance(q2, dict) else q2}")
        st, org = await get("/public/organizations/", {"q": "180940012345", "limit": 1})
        out.append(f"ORGS {st} sample: " + short(org, 400))
    except Exception as e:
        out.append(f"ошибка: {type(e).__name__}: {e}")
    return "\n".join(out)
