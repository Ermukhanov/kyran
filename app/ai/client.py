"""Обёртка над Anthropic API. Без ключа available() == False, и всё работает без ИИ."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import config

log = logging.getLogger(__name__)
_client = None


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа модели, даже если вокруг есть лишний текст или ```json."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("В ответе модели нет корректного JSON")


async def ask_json(model: str, system: str, prompt: str, max_tokens: int = 4000) -> Any:
    """Запрос, на который модель обязана ответить JSON. Одна повторная попытка при мусоре."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    last_error: Exception | None = None
    for _ in range(2):
        resp = await client().messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason == "max_tokens":
            log.warning("Ответ модели обрезан по max_tokens=%s", max_tokens)
        try:
            return extract_json(text)
        except ValueError as e:
            last_error = e
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Ответ должен быть только валидным JSON без пояснений. Повтори."},
            ]
    raise last_error or ValueError("Пустой ответ модели")
