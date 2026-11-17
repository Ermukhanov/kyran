"""Модели и подключение к базе. PostgreSQL на Railway, SQLite локально и в тестах."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite возвращает даты без часового пояса — считаем их UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str] = mapped_column(String(100), default="")
    company_bin: Mapped[str] = mapped_column(String(12), default="")
    company_name: Mapped[str] = mapped_column(String(300), default="")
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    about: Mapped[str] = mapped_column(Text, default="")  # свободное описание компании для ИИ
    min_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    regions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lot(Base):
    __tablename__ = "lots"
    __table_args__ = (UniqueConstraint("source", "ext_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50))
    ext_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    customer: Mapped[str] = mapped_column(Text, default="")
    customer_bin: Mapped[str] = mapped_column(String(12), default="")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    region: Mapped[str] = mapped_column(String(200), default="")
    method: Mapped[str] = mapped_column(String(200), default="")  # способ закупки
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    url: Mapped[str] = mapped_column(Text, default="")
    documents: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{"name", "url"}]
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Match(Base):
    """Лот, подобранный пользователю, с оценкой 0–100."""
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("user_id", "lot_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    lot_id: Mapped[int] = mapped_column(Integer, ForeignKey("lots.id", ondelete="CASCADE"))
    score: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


STAGES = ["saved", "preparing", "submitted", "won", "lost"]


class PipelineItem(Base):
    """Воронка: Интересно → Готовим → Подали → Выиграли / Проиграли."""
    __tablename__ = "pipeline"
    __table_args__ = (UniqueConstraint("user_id", "lot_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    lot_id: Mapped[int] = mapped_column(Integer, ForeignKey("lots.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(20), default="saved")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Analysis(Base):
    """ИИ-разбор тендера под конкретную компанию."""
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("user_id", "lot_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    lot_id: Mapped[int] = mapped_column(Integer, ForeignKey("lots.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(String(20), default="")  # go / consider / skip
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Monitor(Base):
    """Слежка за любым сайтом по ссылке."""
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), default="")
    last_hash: Mapped[str] = mapped_column(String(64), default="")
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_change: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


def configure(url: str) -> None:
    """Переподключение к другой базе (для тестов)."""
    global engine, Session
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def session() -> AsyncIterator[AsyncSession]:
    async with Session() as s:
        yield s
