"""Тарифы, пробный период и лимиты."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import Analysis, Document, User, aware, utcnow

TRIAL_DAYS = 14


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price: int            # ₸ в месяц
    analyses: int         # ИИ-разборов в месяц
    documents: int        # документов в библиотеке
    seats: int            # пользователей
    features: tuple[str, ...]


PLANS: dict[str, Plan] = {
    "trial": Plan("trial", "Пробный период", 0, 10, 30, 1, (
        "Все функции на 14 дней", "10 ИИ-разборов", "До 30 документов в библиотеке")),
    "start": Plan("start", "Старт", 14_900, 10, 30, 1, (
        "Подбор закупок и уведомления в Telegram", "10 ИИ-разборов в месяц", "Библиотека до 30 документов",
        "Воронка, сроки и напоминания")),
    "pro": Plan("pro", "Профи", 29_900, 50, 200, 1, (
        "Всё из «Старт»", "50 ИИ-разборов в месяц", "Библиотека до 200 документов",
        "Готовность к заявке по каждой закупке", "Мониторинг сайтов заказчиков")),
    "team": Plan("team", "Команда", 69_900, 200, 1000, 5, (
        "Всё из «Профи»", "200 ИИ-разборов в месяц", "До 5 сотрудников", "Приоритетная поддержка")),
}
PAID = ("start", "pro", "team")


def current_plan(user: User, now: datetime | None = None) -> tuple[Plan, bool, datetime | None]:
    """(тариф, активен ли, до какого числа). Истёкший платный тариф и пробный период — неактивны."""
    now = now or utcnow()
    code = user.plan or "trial"
    until = aware(user.plan_until)
    if code == "trial" and until is None:
        until = aware(user.created_at or now) + timedelta(days=TRIAL_DAYS)
    active = until is None or until > now
    return PLANS.get(code, PLANS["trial"]), active, until


def month_start(now: datetime | None = None) -> datetime:
    now = (now or utcnow()).astimezone(config.TZ)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def usage(s: AsyncSession, user: User) -> dict[str, int]:
    analyses = (await s.execute(select(func.count(Analysis.id)).where(
        Analysis.user_id == user.id, Analysis.created_at >= month_start()))).scalar() or 0
    documents = (await s.execute(select(func.count(Document.id)).where(Document.user_id == user.id))).scalar() or 0
    return {"analyses": int(analyses), "documents": int(documents)}


class LimitError(Exception):
    """Лимит тарифа исчерпан или тариф неактивен — текст показывается пользователю."""


async def check(s: AsyncSession, user: User, what: str) -> None:
    plan, active, _ = current_plan(user)
    if not active:
        raise LimitError("Срок действия тарифа истёк. Продлите подписку в разделе «Профиль → Тариф».")
    used = await usage(s, user)
    limit = {"analyses": plan.analyses, "documents": plan.documents}[what]
    if used[what] >= limit:
        label = {"analyses": "ИИ-разборов в этом месяце", "documents": "документов в библиотеке"}[what]
        raise LimitError(f"Достигнут лимит тарифа «{plan.name}»: {limit} {label}. Перейдите на тариф выше.")


def grant(user: User, plan: str, months: int, now: datetime | None = None) -> None:
    """Подключить тариф: продлевает от текущей даты окончания, если она в будущем."""
    if plan not in PLANS:
        raise ValueError(f"Нет такого тарифа: {plan}")
    now = now or utcnow()
    base = aware(user.plan_until) if user.plan == plan and user.plan_until and aware(user.plan_until) > now else now
    user.plan, user.plan_until = plan, base + timedelta(days=30 * months)


def plan_json(user: User, used: dict[str, int]) -> dict:
    plan, active, until = current_plan(user)
    return {
        "code": plan.code, "name": plan.name, "active": active, "until": until.isoformat() if until else None,
        "limits": {"analyses": plan.analyses, "documents": plan.documents, "seats": plan.seats},
        "usage": used,
        "catalog": [
            {"code": p.code, "name": p.name, "price": p.price, "features": list(p.features)} for p in PLANS.values() if p.code in PAID
        ],
        "payment_info": config.PAYMENT_INFO,
    }
