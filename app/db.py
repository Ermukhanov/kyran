"""Модели и подключение к базе. PostgreSQL на Railway, SQLite локально и в тестах."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
    inspect, text,
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
    # Тариф: trial → start / pro / team. plan_until — до какого момента оплачен (для trial — конец пробного периода)
    plan: Mapped[str] = mapped_column(String(20), default="trial", server_default="trial")
    plan_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Увеличение номера разлогинивает все устройства
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Реквизиты для документов заявки
    director: Mapped[str] = mapped_column(String(200), default="", server_default="")
    director_title: Mapped[str] = mapped_column(String(100), default="Директор", server_default="Директор")
    address: Mapped[str] = mapped_column(String(400), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(60), default="", server_default="")
    email: Mapped[str] = mapped_column(String(120), default="", server_default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    iban: Mapped[str] = mapped_column(String(40), default="", server_default="")
    bik: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # Какое уведомление о тарифе уже отправлено: "", "soon:<дата>", "expired:<дата>"
    plan_notice: Mapped[str] = mapped_column(String(40), default="", server_default="")
    # Пробная неделя PRO даётся один раз на Telegram ID (новичкам — сразу при регистрации)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


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
    reminded_3d: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    reminded_1d: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


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


DOC_KINDS = {
    "charter": "Учредительные документы",
    "registration": "Справка о государственной регистрации",
    "license": "Лицензия / разрешение",
    "tax": "Справка об отсутствии налоговой задолженности",
    "bank": "Справка банка об отсутствии задолженности",
    "certificate": "Сертификат (ISO, СТ-KZ, соответствия)",
    "experience": "Подтверждение опыта (акты, договоры)",
    "staff": "Квалификация персонала",
    "equipment": "Техника и оборудование",
    "guarantee": "Банковская гарантия / обеспечение",
    "other": "Прочее",
}


class Award(Base):
    """Итог прошедшей закупки: кто выиграл и за сколько. Основа аналитики цен и конкурентов."""
    __tablename__ = "awards"
    __table_args__ = (UniqueConstraint("source", "ext_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    ext_id: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(Text)
    customer: Mapped[str] = mapped_column(String(500), default="")
    customer_bin: Mapped[str] = mapped_column(String(12), default="", index=True)
    region: Mapped[str] = mapped_column(String(200), default="")
    method: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)   # плановая сумма
    price: Mapped[float | None] = mapped_column(Float, nullable=True)    # цена договора
    winner: Mapped[str] = mapped_column(String(500), default="")
    winner_bin: Mapped[str] = mapped_column(String(12), default="")
    participants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="done")   # done / failed (не состоялась)
    awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # zakup.gov.kz: id организаций на платформе и код ЕНС ТРУ — для поиска похожих и досье
    supplier_org_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    customer_org_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    enstru: Mapped[str] = mapped_column(String(40), default="", server_default="", index=True)
    url: Mapped[str] = mapped_column(String(300), default="", server_default="")


class OrgRef(Base):
    """Организация на zakup.gov.kz: id платформы ↔ БИН."""
    __tablename__ = "org_refs"

    org_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    bin: Mapped[str] = mapped_column(String(12), default="", index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FetchMark(Base):
    """Когда последний раз загружали срез данных (чтобы не ходить на площадку при каждом открытии)."""
    __tablename__ = "fetch_marks"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    """Библиотека документов компании: хранится в базе, со сроком действия."""
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(30), default="other")
    filename: Mapped[str] = mapped_column(String(300), default="")
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    reminded_30d: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_7d: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Competitor(Base):
    """Конкурент, за которым следит пользователь (по БИН)."""
    __tablename__ = "competitors"
    __table_args__ = (UniqueConstraint("user_id", "bin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bin: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    last_award_id: Mapped[int] = mapped_column(Integer, default=0)  # о победах до этой записи уже сообщили
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GeneratedDoc(Base):
    """Документ заявки, сформированный Kyran (Word)."""
    __tablename__ = "generated_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginCode(Base):
    """Вход на сайт: сайт показывает код, пользователь подтверждает его в Telegram-боте."""
    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UpgradeRequest(Base):
    """Заявка на подключение тарифа. Подтверждает админ командой /grant в боте."""
    __tablename__ = "upgrade_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    plan: Mapped[str] = mapped_column(String(20))
    months: Mapped[int] = mapped_column(Integer, default=1)
    contact: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="new")  # new / done / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


def configure(url: str) -> None:
    """Переподключение к другой базе (для тестов)."""
    global engine, Session
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn) -> list[str]:
    """Простая миграция: добавляет новые колонки в уже существующие таблицы (create_all этого не делает)."""
    insp = inspect(conn)
    added = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(conn.dialect)}'
            if col.server_default is not None:
                default = col.server_default.arg
                if isinstance(col.type, Boolean):
                    default = "false" if conn.dialect.name == "postgresql" and default == "0" else default
                ddl += f" DEFAULT '{default}'" if not str(default).isdigit() and default not in ("false", "true") else f" DEFAULT {default}"
            conn.execute(text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        import logging

        logging.getLogger(__name__).info("Миграция: добавлены колонки %s", ", ".join(added))


async def session() -> AsyncIterator[AsyncSession]:
    async with Session() as s:
        yield s
