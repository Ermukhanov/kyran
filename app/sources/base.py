"""Единый формат лота. Любой источник (API, сайт, демо) отдаёт список LotData."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class LotData:
    source: str
    ext_id: str
    title: str
    description: str = ""
    customer: str = ""
    customer_bin: str = ""
    amount: float | None = None
    region: str = ""
    method: str = ""
    deadline: datetime | None = None
    published_at: datetime | None = None
    url: str = ""
    documents: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    is_demo: bool = False


class Source(Protocol):
    name: str

    @property
    def enabled(self) -> bool: ...

    async def fetch(self) -> list[LotData]: ...
