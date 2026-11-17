"""Вход на сайт по коду через Telegram.

1. Сайт: start() → код из 6 цифр + секретный nonce + ссылка на бота (t.me/<бот>?start=login_<nonce>).
2. Пользователь нажимает ссылку или отправляет код боту → bind() привязывает свой Telegram ID.
3. Сайт раз в 2 секунды вызывает poll(nonce) → получает сессионный токен (один раз).
Код без nonce бесполезен: токен выдаётся только тому, кто начал вход.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import make_link_token
from app.db import LoginCode, User, aware, utcnow

TTL = timedelta(minutes=10)


async def start(s: AsyncSession) -> LoginCode:
    for _ in range(5):
        code = f"{secrets.randbelow(1_000_000):06d}"
        clash = (await s.execute(select(LoginCode.id).where(
            LoginCode.code == code, LoginCode.used.is_(False), LoginCode.expires_at > utcnow()))).first()
        if not clash:
            break
    lc = LoginCode(nonce=secrets.token_urlsafe(24), code=code, expires_at=utcnow() + TTL)
    s.add(lc)
    await s.commit()
    return lc


async def bind(s: AsyncSession, user_id: int, *, code: str | None = None, nonce: str | None = None) -> bool:
    """Подтверждение из бота. True — вход подтверждён."""
    q = select(LoginCode).where(LoginCode.used.is_(False), LoginCode.user_id.is_(None))
    q = q.where(LoginCode.nonce == nonce) if nonce else q.where(LoginCode.code == (code or ""))
    lc = (await s.execute(q.order_by(LoginCode.id.desc()))).scalars().first()
    if not lc or aware(lc.expires_at) < utcnow():
        return False
    lc.user_id = user_id
    await s.commit()
    return True


async def poll(s: AsyncSession, nonce: str) -> str | None:
    """Токен сессии, если вход подтверждён; иначе None. Выдаётся один раз."""
    lc = (await s.execute(select(LoginCode).where(LoginCode.nonce == nonce))).scalar_one_or_none()
    if not lc or lc.used or aware(lc.expires_at) < utcnow():
        raise PermissionError("Код устарел. Обновите страницу и получите новый.")
    if lc.user_id is None:
        return None
    user = await s.get(User, lc.user_id)
    if user is None:
        return None
    lc.used = True
    await s.commit()
    return make_link_token(user.id, version=user.token_version or 0)
