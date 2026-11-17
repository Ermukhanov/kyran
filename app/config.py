"""Все настройки — из переменных окружения (Railway → Variables)."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./kyran.db")
    # Railway отдаёт postgresql://… — SQLAlchemy нужен async-драйвер
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


# ── Telegram ──
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# ── Веб ──
# Railway сам задаёт RAILWAY_PUBLIC_DOMAIN, когда у сервиса есть домен (Settings → Networking → Generate Domain)
PUBLIC_URL = os.environ.get("PUBLIC_URL") or (
    f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else ""
)
PORT = int(os.environ.get("PORT", "8000"))
SECRET_KEY = os.environ.get("SECRET_KEY") or TELEGRAM_BOT_TOKEN or "dev-secret-change-me"

# ── База ──
DATABASE_URL = _database_url()

# ── Источники ──
GOSZAKUP_TOKEN = os.environ.get("GOSZAKUP_TOKEN", "")          # ← сюда токен Минфина
GOSZAKUP_API = os.environ.get("GOSZAKUP_API", "https://ows.goszakup.gov.kz/v3")
DEMO_MODE = _bool("DEMO_MODE", False)                           # демо-лоты с пометкой «ДЕМО»

# ── ИИ ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")    # ← сюда ключ Anthropic
AI_FAST_MODEL = os.environ.get("AI_FAST_MODEL", "claude-haiku-5-5")     # оценка лотов
AI_SMART_MODEL = os.environ.get("AI_SMART_MODEL", "claude-sonnet-5-5")  # разбор и агент

# ── Расписание ──
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Almaty"))
COLLECT_EVERY_MIN = int(os.environ.get("COLLECT_EVERY_MIN", "60"))
WEBSITE_EVERY_MIN = int(os.environ.get("WEBSITE_EVERY_MIN", "180"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
DIGEST_SIZE = int(os.environ.get("DIGEST_SIZE", "8"))
URGENT_DAYS = int(os.environ.get("URGENT_DAYS", "3"))
