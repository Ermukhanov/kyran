"""Вход в Mini App.

1. Внутри Telegram: Mini App присылает initData, проверяем подпись токеном бота
   (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
2. На ПК в браузере: команда /web в боте выдаёт ссылку с подписанным токеном (действует 30 дней).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from app import config

INIT_DATA_MAX_AGE = 7 * 24 * 3600
LINK_TTL = 30 * 24 * 3600


def validate_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE) -> dict:
    """Возвращает данные пользователя Telegram или бросает ValueError."""
    if not bot_token:
        raise ValueError("бот не настроен")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise ValueError("нет подписи")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("неверная подпись")
    if time.time() - int(pairs.get("auth_date", "0")) > max_age:
        raise ValueError("сессия устарела")
    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("нет пользователя")
    return user


def _sign(payload: bytes) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()[:32]


def make_link_token(user_id: int, ttl: int = LINK_TTL, version: int = 0) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": user_id, "e": int(time.time()) + ttl, "v": version}).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload.encode())}"


def read_link_token(token: str) -> int:
    return read_token(token)[0]


def read_token(token: str) -> tuple[int, int]:
    """(user_id, версия токена)."""
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        raise ValueError("битый токен")
    if not hmac.compare_digest(_sign(payload.encode()), sig):
        raise ValueError("неверная подпись")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if data["e"] < time.time():
        raise ValueError("сессия истекла, войдите заново")
    return int(data["u"]), int(data.get("v", 0))


async def current_tg_user(authorization: str = Header(default="")) -> dict:
    """FastAPI-зависимость: {id, name, username} из заголовка Authorization."""
    scheme, _, value = authorization.partition(" ")
    try:
        if scheme == "tma":
            u = validate_init_data(value, config.TELEGRAM_BOT_TOKEN)
            name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
            return {"id": int(u["id"]), "name": name, "username": u.get("username", ""), "v": None}
        if scheme == "Bearer":
            uid, version = read_token(value)
            return {"id": uid, "name": "", "username": "", "v": version}
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(401, f"Нет доступа: {e}")
    raise HTTPException(401, "Требуется вход")
