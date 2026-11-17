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
        customer=str(pick(row, "organization_name", "customer_name", default=""))[:500],
        customer_bin=str(pick(row, "organization_bin", "customer_bin", "bin", default=""))[:12],
        amount=to_float(pick(row, "total_price", "amount", "sum")),
        region=str(pick(row, "delivery_place", "kato_name", "region_name", default=""))[:200],
        method=str(pick(row, "purchase_method_name", default=""))[:200],
        deadline=parse_dt(pick(row, "offer_end_date")),
        published_at=parse_dt(pick(row, "announcement_publish_date", "publish_date")),
        url=f"{SITE}/announcement/{ann}#lot-{lot_id}" if ann else SITE,
        raw={"platform": system.get("name") or "", "platform_id": system.get("id"), "lot_number": pick(row, "lot_number"),
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
            async with session.get(API, params=params) as resp:
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

    async def fetch(self) -> list[LotData]:
        return await self.search(self.keywords)
