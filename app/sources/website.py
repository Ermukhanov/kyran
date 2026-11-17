"""Мониторинг любого сайта по ссылке.

Скачиваем страницу, достаём видимый текст и сравниваем отпечаток с прошлым разом.
Если изменилось:
  • есть ключ ИИ → Claude вытаскивает из текста новые закупки (название, сумма, дедлайн, ссылка);
  • ключа нет → просто сообщаем «страница изменилась» со ссылкой.
Только публичные страницы. Сайты с входом по ЭЦП не трогаем.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

log = logging.getLogger(__name__)
MAX_BYTES = 2_000_000
USER_AGENT = "KyranBot/1.0 (+tender monitoring; public pages only)"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href:
            text = " ".join(self._link_text).strip()
            if text:
                self.links.append((text, self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        text = data.strip()
        if text:
            self.parts.append(text)
            if self._href is not None:
                self._link_text.append(text)


def page_text(html: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(заголовок, видимый текст, ссылки)"""
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return parser.title.strip(), text, parser.links


def fingerprint(text: str) -> str:
    # Цифры времени/счётчиков часто меняются сами — убираем их из отпечатка
    stable = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", text)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def is_public_url(url: str) -> bool:
    """Защита: только http(s) и только публичные адреса (не localhost и не внутренняя сеть)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


async def fetch_page(url: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Скачать страницу → (заголовок, текст, ссылки). Ошибки — исключением."""
    import asyncio

    if not await asyncio.to_thread(is_public_url, url):
        raise ValueError("Можно следить только за публичными сайтами (http/https)")
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as s:
        async with s.get(url, allow_redirects=True, max_redirects=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Сайт ответил {resp.status}")
            raw = await resp.content.read(MAX_BYTES)
            html = raw.decode(resp.charset or "utf-8", errors="replace")
    return page_text(html)
