"""Точка входа: FastAPI (API + Mini App) и Telegram-бот в одном процессе.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.ai import client as ai
from app.api.routes import router
from app.services import core
from app.sources.registry import SOURCES

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kyran")
WEB = Path(__file__).resolve().parent.parent / "web"


async def collect_loop() -> None:
    """Сбор лотов без бота (например, локально без TELEGRAM_BOT_TOKEN)."""
    while True:
        try:
            async with db.Session() as s:
                new = await core.collect(s, SOURCES)
                await core.match_new_lots(s, new)
        except Exception:
            log.exception("Сбор не удался")
        await asyncio.sleep(config.COLLECT_EVERY_MIN * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_db()
    async with db.Session() as s:
        n = await core.fix_keywords(s)
    if n:
        log.info("Исправлены направления у пользователей: %d", n)
    if not config.DEMO_MODE:
        async with db.Session() as s:
            removed = await core.purge_demo(s)
        if removed:
            log.info("Демо выключено: удалено демо-записей %d", removed)
    log.info("Kyran: источники %s, ИИ %s, веб %s",
             {s.name: s.enabled for s in SOURCES}, "вкл" if ai.available() else "выкл", config.PUBLIC_URL or "—")
    bot_app, task = None, None

    async def zakup_selftest():
        z = next((x for x in SOURCES if x.name == "zakup" and x.enabled), None)
        if z:
            log.info("Самопроверка zakup.gov.kz:\n%s", await z.diagnose("кабель", True))
            from app.services import results
            log.info("Самопроверка конкурентов: «мебель» -> %s, «ремонт кровли» -> %s",
                     await results.codes_for_query("мебель"), await results.codes_for_query("ремонт кровли"))
    selftest = asyncio.create_task(zakup_selftest())

    async def leads_once():
        if os.environ.get("KYRAN_LEADS_SCAN", "0") != "1":
            return
        await asyncio.sleep(20)
        try:
            from app.services.leads import find_aktobe_education
            await find_aktobe_education()
        except Exception:
            log.exception("Лиды: поиск не удался")
    leads_task = asyncio.create_task(leads_once())
    if config.TELEGRAM_BOT_TOKEN:
        from app.bot.bot import build, post_init

        bot_app = build()
        await bot_app.initialize()
        await post_init(bot_app)  # при ручном запуске PTB сам post_init не вызывает
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram-бот запущен")
    else:
        log.warning("TELEGRAM_BOT_TOKEN не задан — работает только веб и сбор лотов")
        task = asyncio.create_task(collect_loop())
    try:
        yield
    finally:
        if bot_app:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass


app = FastAPI(title="Kyran", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(router)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def root():
    return FileResponse(WEB / "landing.html", headers={"Cache-Control": "no-cache"})


@app.get("/app")
async def mini_app():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "sources": {s.name: s.enabled for s in SOURCES}, "ai": ai.available()})
