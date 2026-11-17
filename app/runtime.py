"""Состояние процесса, общее для веба и бота (заполняется при запуске)."""
from __future__ import annotations

from typing import Any

bot: Any = None            # telegram.Bot, если бот запущен
import os

bot_username: str = os.environ.get("BOT_USERNAME", "").lstrip("@")  # имя бота без @ — для ссылок входа


async def notify_admins(text: str) -> None:
    """Сообщение администраторам (заявки на тариф и т.п.). Тихо пропускает, если бота нет."""
    from app import config

    if not bot:
        return
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except Exception:
            pass
