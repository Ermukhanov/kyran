"""Открытые данные портала госзакупок РК — ows.goszakup.gov.kz (REST v3).

Нужен токен Минфина → переменная GOSZAKUP_TOKEN. Без токена источник выключен.
Портал без токена отвечает 401, а сайт пускает только через ЭЦП — проверено 17.11.2026.

Названия полей взяты из документации v3. Их надо сверить на первом реальном ответе:
если в логах «получено 0 лотов», поправьте маппинг в item_to_lot().
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app import config
from app.sources.base import LotData

log = logging.getLogger(__name__)
SITE = "https://goszakup.gov.kz"
ANNOUNCE_URL = SITE + "/ru/announce/index/{trd_buy_id}"


def pick(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    # Время портала — Казахстан
    return dt if dt.tzinfo else dt.replace(tzinfo=config.TZ)


def to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(" ", "").replace(",", ".")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_to_lot(item: dict[str, Any]) -> LotData:
    trd_buy_id = pick(item, "trd_buy_id", "trdBuyId")
    files = pick(item, "files", "Files", default=[]) or []
    documents = [
        {"name": str(pick(f, "original_name", "name_ru", default="Документ")), "url": str(pick(f, "file_path", "url", default=""))}
        for f in files if isinstance(f, dict) and pick(f, "file_path", "url")
    ]
    return LotData(
        source="goszakup",
        ext_id=str(pick(item, "id", "lot_id", default="")),
        title=str(pick(item, "name_ru", "nameRu", "name_kz", default="")).strip(),
        description=str(pick(item, "description_ru", "descriptionRu", "description_kz", default="")).strip(),
        customer=str(pick(item, "customer_name_ru", "customerNameRu", default="")).strip(),
        customer_bin=str(pick(item, "customer_bin", "customerBin", default="")),
        amount=to_float(pick(item, "amount", "total_sum", "sum")),
        region=str(pick(item, "region_ru", "kato_ru", default="")),
        method=str(pick(item, "ref_trade_methods_name_ru", "trade_method_ru", default="")),
        deadline=parse_dt(pick(item, "end_date", "endDate", "trd_buy_end_date")),
        published_at=parse_dt(pick(item, "publish_date", "publishDate", "index_date")),
        url=ANNOUNCE_URL.format(trd_buy_id=trd_buy_id) if trd_buy_id else "",
        documents=documents,
        raw=item,
    )


class GoszakupSource:
    name = "goszakup"

    def __init__(self, token: str | None = None, base_url: str | None = None, max_pages: int = 5):
        self.token = config.GOSZAKUP_TOKEN if token is None else token
        self.base_url = (base_url or config.GOSZAKUP_API).rstrip("/")
        self.max_pages = max_pages

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _get_json(self, session, url: str) -> dict[str, Any] | None:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.error("goszakup: HTTP %s на %s", resp.status, url)
                return None
            return await resp.json(content_type=None)

    async def fetch(self) -> list[LotData]:
        if not self.enabled:
            return []
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"}
        url: str | None = f"{self.base_url}/lots?limit=200"
        lots: list[LotData] = []
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as s:
            for _ in range(self.max_pages):
                if not url:
                    break
                data = await self._get_json(s, url)
                if not data:
                    break
                lots += [item_to_lot(i) for i in data.get("items") or [] if isinstance(i, dict)]
                nxt = data.get("next_page")
                url = (nxt if str(nxt).startswith("http") else "https://ows.goszakup.gov.kz" + str(nxt)) if nxt else None
        log.info("goszakup: получено %d лотов", len(lots))
        return [lot for lot in lots if lot.ext_id and lot.title]

    async def company(self, biin: str) -> dict[str, Any] | None:
        """Карточка участника по БИН/ИИН (для онбординга). Эндпоинт сверить при получении токена."""
        if not self.enabled:
            return None
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as s:
            data = await self._get_json(s, f"{self.base_url}/subject/biin/{biin}")
        if not data:
            return None
        return {
            "name": str(pick(data, "name_ru", "full_name_ru", default="")),
            "raw": data,
        }
