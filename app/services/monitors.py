"""Слежка за сайтами: проверка изменений и (с ИИ) извлечение новых закупок со страницы."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.db import Lot, Match, Monitor, User, utcnow
from app.services.core import upsert_lots
from app.sources.base import LotData
from app.sources.goszakup import parse_dt, to_float
from app.sources.website import fetch_page, fingerprint

log = logging.getLogger(__name__)
MAX_MONITORS = 10

EXTRACT_SYSTEM = (
    "Ты извлекаешь объявления о закупках/тендерах с веб-страницы. Бери только реальные закупки "
    "(название, срок подачи, сумма, ссылка), игнорируй меню, новости и рекламу. Ничего не придумывай: "
    "если поля нет на странице — null. Ответ только JSON."
)


@dataclass
class CheckResult:
    monitor: Monitor
    changed: bool = False
    first_run: bool = False
    new_lots: list[Lot] = field(default_factory=list)
    error: str = ""


async def add_monitor(s: AsyncSession, user: User, url: str) -> Monitor:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    count = len((await s.execute(select(Monitor.id).where(Monitor.user_id == user.id, Monitor.active.is_(True)))).all())
    if count >= MAX_MONITORS:
        raise ValueError(f"Можно следить максимум за {MAX_MONITORS} сайтами")
    title, text, _ = await fetch_page(url)  # проверяем, что сайт открывается
    m = Monitor(user_id=user.id, url=url, title=(title or urlparse(url).netloc)[:300],
                last_hash=fingerprint(text), last_checked=utcnow())
    s.add(m)
    await s.commit()
    return m


async def extract_tenders(url: str, text: str, links: list[tuple[str, str]]) -> list[LotData]:
    if not ai.available():
        return []
    link_list = [{"text": t[:120], "href": urljoin(url, h)} for t, h in links[:300]]
    prompt = (
        f"Страница: {url}\n\nТекст страницы:\n{text[:30000]}\n\nСсылки:\n{json.dumps(link_list, ensure_ascii=False)[:15000]}\n\n"
        'Верни JSON: {"tenders": [{"title": "", "customer": null, "amount": null, "deadline": "YYYY-MM-DD HH:MM или null", "url": null}]}'
    )
    try:
        data = await ai.ask_json(config.AI_FAST_MODEL, EXTRACT_SYSTEM, prompt, max_tokens=3000)
    except Exception:
        log.exception("ИИ не смог разобрать страницу %s", url)
        return []
    host = urlparse(url).netloc
    out = []
    for t in data.get("tenders", [])[:50]:
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        link = t.get("url") or url
        ext = hashlib.sha1(f"{title}|{link}".encode()).hexdigest()[:20]
        out.append(LotData(
            source=f"web:{host}"[:50], ext_id=ext, title=title[:500], customer=str(t.get("customer") or "")[:300],
            amount=to_float(t.get("amount")), deadline=parse_dt(t.get("deadline")), url=str(link),
        ))
    return out


async def check(s: AsyncSession, m: Monitor) -> CheckResult:
    res = CheckResult(monitor=m, first_run=not m.last_hash)
    try:
        _, text, links = await fetch_page(m.url)
    except Exception as e:
        m.last_error, m.last_checked = str(e)[:300], utcnow()
        res.error = m.last_error
        await s.commit()
        return res
    new_hash = fingerprint(text)
    m.last_checked, m.last_error = utcnow(), ""
    if new_hash != m.last_hash:
        res.changed = not res.first_run
        m.last_hash = new_hash
        if res.changed:
            m.last_change = utcnow()
            res.new_lots = await upsert_lots(s, await extract_tenders(m.url, text, links))
            for lot in res.new_lots:
                s.add(Match(user_id=m.user_id, lot_id=lot.id, score=80, reason=f"Новое на сайте {m.title}", notified=True))
    await s.commit()
    return res


async def check_all(s: AsyncSession) -> list[CheckResult]:
    monitors = (await s.execute(select(Monitor).where(Monitor.active.is_(True)))).scalars().all()
    return [await check(s, m) for m in monitors]
