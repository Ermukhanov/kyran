"""Тарифы, пробный период и лимиты."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import Analysis, Competitor, Document, GeneratedDoc, User, aware, utcnow

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
    generations: int = 20  # документов заявки, подготовленных ИИ, в месяц
    competitors: int = 3   # конкурентов под наблюдением
    keywords: int = 30     # направлений в профиле
    chat_per_day: int = 200  # вопросов ИИ-агенту в день
    instant: bool = True   # уведомление о новом тендере в течение 5 минут после публикации
    digest_size: int = 10  # тендеров в утренней подборке
    ai_scoring: bool = True  # ИИ-оценка совпадения 0–100 с объяснением
    feed_limit: int = 0    # сколько тендеров видно в ленте (0 — все)


PLANS: dict[str, Plan] = {
    # Бесплатно навсегда: полезно каждый день, но медленнее и меньше — как бесплатный ChatGPT/Claude.
    # Платят за скорость (тендер через 5 минут, а не завтра утром), полный список и ИИ.
    "free": Plan("free", "Бесплатный", 0, 1, 5, 1, (
        "Утренняя подборка: 3 лучших тендера в день", "До 3 направлений", "5 вопросов ИИ-агенту в день",
        "1 ИИ-разбор и 1 документ заявки в месяц", "Воронка и напоминания о сроках"),
        generations=1, competitors=0, keywords=3, chat_per_day=5, instant=False, digest_size=3, ai_scoring=False, feed_limit=10),
    "trial": Plan("trial", "Пробный период", 0, 10, 30, 1, (
        "Все функции на 14 дней", "10 ИИ-разборов", "До 30 документов в библиотеке"), 10, 5),
    "start": Plan("start", "Старт", 14_900, 10, 30, 1, (
        "Новый тендер в Telegram через 5 минут после публикации", "Все подходящие тендеры, а не 3 в день",
        "ИИ-оценка каждого тендера 0–100 с объяснением", "До 30 направлений, ИИ-агент без ограничений",
        "10 ИИ-разборов и 20 документов заявки в месяц", "Слежка за 3 конкурентами"), 20, 3),
    "pro": Plan("pro", "Профи", 29_900, 50, 200, 1, (
        "Всё из «Старт»", "50 ИИ-разборов в месяц", "Библиотека до 200 документов",
        "Документы заявки в Word: 100 в месяц", "Слежка за 20 конкурентами", "Готовность к заявке по каждой закупке",
        "Мониторинг сайтов заказчиков"), 100, 20),
    "team": Plan("team", "Команда", 69_900, 200, 1000, 5, (
        "Всё из «Профи»", "200 ИИ-разборов в месяц", "Документы заявки в Word: 400 в месяц", "До 5 сотрудников",
        "Слежка за 50 конкурентами", "Приоритетная поддержка"), 400, 50),
}
PAID = ("start", "pro", "team")


def raw_plan(user: User, now: datetime | None = None) -> tuple[Plan, bool, datetime | None]:
    """Оплаченный тариф или пробный период как есть: (тариф, активен ли, до какого числа)."""
    now = now or utcnow()
    code = user.plan or "trial"
    until = aware(user.plan_until)
    if code == "trial" and until is None:
        until = aware(user.created_at or now) + timedelta(days=TRIAL_DAYS)
    active = until is None or until > now
    return PLANS.get(code, PLANS["trial"]), active, until


def current_plan(user: User, now: datetime | None = None) -> tuple[Plan, bool, datetime | None]:
    """Действующий тариф. Закончился пробный период или оплата — человек НЕ теряет Kyran,
    а переходит на бесплатный тариф с ограничениями (раньше бот просто замолкал)."""
    plan, active, until = raw_plan(user, now)
    if not active or plan.code == "free":
        return PLANS["free"], True, None
    return plan, True, until


def is_paid(user: User) -> bool:
    """Платный тариф или пробный период (всё открыто)."""
    return current_plan(user)[0].code != "free"


UPGRADE_HINT = "Тариф «Старт» — 14 900 ₸/мес: Профиль → Тариф."


def month_start(now: datetime | None = None) -> datetime:
    now = (now or utcnow()).astimezone(config.TZ)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def usage(s: AsyncSession, user: User) -> dict[str, int]:
    analyses = (await s.execute(select(func.count(Analysis.id)).where(
        Analysis.user_id == user.id, Analysis.created_at >= month_start()))).scalar() or 0
    documents = (await s.execute(select(func.count(Document.id)).where(Document.user_id == user.id))).scalar() or 0
    generations = (await s.execute(select(func.count(GeneratedDoc.id)).where(
        GeneratedDoc.user_id == user.id, GeneratedDoc.by_ai.is_(True), GeneratedDoc.created_at >= month_start()))).scalar() or 0
    competitors = (await s.execute(select(func.count(Competitor.id)).where(Competitor.user_id == user.id))).scalar() or 0
    return {"analyses": int(analyses), "documents": int(documents), "generations": int(generations), "competitors": int(competitors)}


class LimitError(Exception):
    """Лимит тарифа исчерпан или тариф неактивен — текст показывается пользователю."""


async def check(s: AsyncSession, user: User, what: str) -> None:
    plan, _, _ = current_plan(user)
    used = await usage(s, user)
    limit = {"analyses": plan.analyses, "documents": plan.documents, "generations": plan.generations,
             "competitors": plan.competitors}[what]
    if used[what] >= limit:
        label = {"analyses": "ИИ-разборов в этом месяце", "documents": "документов в библиотеке",
                 "generations": "документов заявки от ИИ в этом месяце", "competitors": "конкурентов под наблюдением"}[what]
        if plan.code == "free":
            what_ru = {"analyses": "ИИ-разбор", "documents": "документы в библиотеке", "generations": "документ заявки от ИИ",
                       "competitors": "слежка за конкурентами"}[what]
            raise LimitError(f"На бесплатном тарифе {what_ru} — {limit if limit else 'недоступно'}"
                             f"{' в месяц' if what in ('analyses', 'generations') and limit else ''}. {UPGRADE_HINT}")
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
        "limits": {"analyses": plan.analyses, "documents": plan.documents, "seats": plan.seats, "generations": plan.generations,
                   "competitors": plan.competitors, "keywords": plan.keywords, "chat_per_day": plan.chat_per_day,
                   "feed_limit": plan.feed_limit, "instant": plan.instant},
        "usage": used,
        "catalog": [
            {"code": p.code, "name": p.name, "price": p.price, "features": list(p.features)} for p in PLANS.values() if p.code in ("free",) + PAID
        ],
        "payment_info": config.PAYMENT_INFO,
    }
