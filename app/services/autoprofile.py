"""Настройка профиля по БИН за 30 секунд.

По БИН находим победы компании в итогах закупок, из их предметов — направления,
из заказчиков и регионов — где компания работает, из сумм — типичный размер.
Название берём с портала госзакупок (когда подключён токен) или из итогов.

ИИ (лёгкая модель, один короткий запрос) формулирует направления точнее; без ключа
работает частотный разбор названий. Пока нет токена Минфина, в демо-режиме
незнакомому БИН сопоставляется одна из демо-компаний — честно помеченная как демо.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.ai import client as ai
from app.db import Award, User
from app.services import core
from app.services.competitors import CompetitorError, clean_bin
from app.services.matcher import STOP, parse_keywords, words
from app.sources.registry import SOURCES

log = logging.getLogger(__name__)

# Слова, которые не несут направления деятельности
NOISE = STOP | {
    "текущий", "капитальный", "средний", "повторная", "закупка", "здания", "здании", "объекта", "нужд", "для",
    "района", "города", "области", "районного", "значения", "учреждения", "административного", "кгу", "гу", "кгп",
    "услуги", "услуг", "работ", "приобретение", "поставка", "оказание", "выполнение", "организации", "по", "на", "и", "в",
}


def _demo_bin_for(bin_: str, winners: list[str]) -> str:
    """Детерминированно сопоставляет незнакомый БИН одной из демо-компаний."""
    if not winners:
        return ""
    h = int(hashlib.md5(bin_.encode()).hexdigest(), 16)
    return sorted(winners)[h % len(winners)]


def heuristic_keywords(titles: list[str], limit: int = 5) -> list[str]:
    """Направления из предметов закупок без ИИ: из каждого названия берём смысловое ядро
    («Текущий ремонт кровли здания школы» → «ремонт кровли»), самые частые ядра — направления."""
    cores: Counter = Counter()
    for t in titles:
        ws = re.findall(r"[а-яёәіңғүұқөһa-z0-9-]+", re.sub(r"\(.*?\)", " ", t.lower()))
        i = 0
        while i < len(ws) and (ws[i] in NOISE or len(ws[i]) <= 2):
            i += 1
        core_words = []
        for w in ws[i:]:
            if w in NOISE or len(w) <= 2:
                break
            core_words.append(w)
            if len(core_words) == 4:
                break
        if core_words:
            cores[" ".join(core_words)] += 1
    out: list[str] = []
    for phrase, _ in cores.most_common():
        if not any(phrase in o or o in phrase for o in out):
            out.append(phrase)
        if len(out) >= limit:
            break
    return out


async def ai_keywords(titles: list[str]) -> tuple[list[str], str] | None:
    if not ai.available() or not titles:
        return None
    try:
        data = await ai.ask_json(
            config.AI_FAST_MODEL,
            "Ты помогаешь поставщику госзакупок Казахстана настроить подбор. По предметам его выигранных закупок "
            "определи направления деятельности — короткими фразами, как их ищут в закупках (2–4 слова), без названий "
            "заказчиков и номеров. И одно предложение о компании. Ответ — только JSON.",
            "Выигранные закупки:\n" + "\n".join(f"- {t}" for t in titles[:40]) +
            '\n\nВерни JSON: {"keywords": ["направление", ...] (3–6 штук), "about": "одно предложение"}',
            max_tokens=400,
        )
        kws = parse_keywords(", ".join(str(k) for k in data.get("keywords") or []))
        return (kws[:6], str(data.get("about") or "")[:400]) if kws else None
    except Exception:
        log.exception("ИИ не сформулировал направления")
        return None


async def preview(s: AsyncSession, user: User, bin_value: str) -> dict:
    """Что Kyran узнал о компании по БИН. Профиль не меняет."""
    bin_ = clean_bin(bin_value)
    demo_of = ""
    from app.services import results

    org = await results.ensure_supplier(s, bin_, days=730)
    wins = (await s.execute(select(Award).where(Award.winner_bin == bin_, Award.status == "done")
                            .order_by(Award.awarded_at.desc()).limit(200))).scalars().all()
    if not wins and org:
        wins = (await s.execute(select(Award).where(Award.supplier_org_id == org.org_id, Award.status == "done")
                                .order_by(Award.awarded_at.desc()).limit(200))).scalars().all()
    if not wins and config.DEMO_MODE:
        demo_bins = list((await s.execute(select(Award.winner_bin).where(Award.is_demo.is_(True), Award.winner_bin != "")
                                          .distinct())).scalars())
        demo_of = _demo_bin_for(bin_, demo_bins)
        if demo_of:
            wins = (await s.execute(select(Award).where(Award.winner_bin == demo_of, Award.status == "done")
                                    .order_by(Award.awarded_at.desc()).limit(200))).scalars().all()

    name = ""
    gz = next((x for x in SOURCES if x.name == "goszakup"), None)
    if gz and gz.enabled:
        try:
            info = await gz.company(bin_)
            name = (info or {}).get("name", "")
        except Exception:
            log.exception("Карточка участника не получена")
    if not name and org:
        name = org.name
    if not name and wins and not demo_of:
        name = wins[0].winner

    titles = [a.title for a in wins]
    found = await ai_keywords(titles)
    keywords, about = found if found else (heuristic_keywords(titles), "")
    regions = [r for r, _ in Counter(a.region for a in wins if a.region).most_common(3)]
    customers = [c for c, _ in Counter(a.customer for a in wins if a.customer).most_common(3)]
    prices = sorted(a.price for a in wins if a.price)
    typical = {"median": median(prices), "min": prices[0], "max": prices[-1]} if prices else None

    ids: set[int] = set()
    open_total = 0.0
    for kw in keywords:
        for lot, _, _ in await core.search(s, kw, None, limit=300):
            if lot.id not in ids:
                ids.add(lot.id)
                open_total += lot.amount or 0
    return {
        "bin": bin_, "name": name, "found": bool(wins), "demo": bool(demo_of), "demo_company": wins[0].winner if demo_of and wins else "",
        "wins": len(wins), "total": sum(a.price or 0 for a in wins),
        "keywords": keywords, "about": about, "regions": regions, "customers": customers, "typical": typical,
        "open_now": len(ids), "open_total": open_total,
        "examples": [{"title": a.title, "customer": a.customer, "price": a.price} for a in wins[:5]],
    }


async def apply(s: AsyncSession, user: User, bin_value: str, keywords: list[str], name: str = "", about: str = "") -> User:
    bin_ = clean_bin(bin_value)
    kws = parse_keywords(", ".join(keywords))
    if not kws:
        raise CompetitorError("Укажите хотя бы одно направление.")
    user.company_bin = bin_
    if name and not user.company_name:
        user.company_name = name[:300]
    if about and not user.about:
        user.about = about[:2000]
    user.keywords, user.onboarded, user.notify = kws, True, True
    await s.commit()
    await core.fetch_for(s, kws)
    await core.rematch_user(s, user)
    return user
