"""Рост: Telegram-каналы с тендерами и партнёрская программа.

Каналы. Бот публикует в канал ниши/региона лучшие свежие тендеры дважды в день.
Под постом — кнопка в бота со ссылкой ?start=ch_<id>: так видно, сколько людей
пришло из каждого канала. Канал работает как бесплатный источник трафика.

Партнёры. Любой пользователь получает ссылку ?start=ref_<свой id>. Когда приведённый
клиент оплачивает тариф, партнёру начисляется PARTNER_SHARE от оплаты (первые 12 мес.).
Выплаты — вручную, админ видит долг в /partners.
"""
from __future__ import annotations

import html
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import Channel, ChannelPost, Lot, PartnerEarning, User, aware, utcnow
from app.services import core
from app.services.matcher import Profile, parse_keywords

log = logging.getLogger(__name__)
PARTNER_SHARE = 0.25
PARTNER_MONTHS = 12


# ── Каналы ─────────────────────────────────────────────────

def parse_channel_args(text: str) -> dict:
    """«@chat Название | ремонт, строительство | Актюбинская | 1000000» → поля канала."""
    parts = [p.strip() for p in text.split("|")]
    head = parts[0].split(maxsplit=1)
    if not head or not (head[0].startswith("@") or head[0].lstrip("-").isdigit()):
        raise ValueError("Первым укажите канал: @username")
    out = {"chat": head[0], "title": head[1] if len(head) > 1 else head[0],
           "keywords": parse_keywords(parts[1]) if len(parts) > 1 else [],
           "region": parts[2] if len(parts) > 2 else "", "min_amount": None}
    if len(parts) > 3 and parts[3].replace(" ", "").isdigit():
        out["min_amount"] = float(parts[3].replace(" ", ""))
    if not out["keywords"]:
        raise ValueError("Укажите направления канала через запятую после первой черты |")
    return out


async def pick_lots(s: AsyncSession, ch: Channel, limit: int | None = None) -> tuple[list[Lot], int, float]:
    """Лучшие ещё не опубликованные тендеры канала: свежие и с запасом времени на подачу."""
    profile = Profile(keywords=[], min_amount=ch.min_amount, regions=[ch.region] if ch.region else [])
    hits: dict[int, tuple[Lot, int]] = {}
    for kw in ch.keywords or []:
        for lot, score, _ in await core.search(s, kw, profile, limit=400, region=ch.region):
            if lot.id not in hits or hits[lot.id][1] < score:
                hits[lot.id] = (lot, score)
    posted = set((await s.execute(select(ChannelPost.lot_id).where(ChannelPost.channel_id == ch.id))).scalars())
    now = utcnow()
    fresh = [(l, sc) for l, sc in hits.values()
             if l.id not in posted and (aware(l.deadline) or now) > now + timedelta(hours=36) and not l.is_demo]
    fresh.sort(key=lambda x: (-x[1], -(x[0].amount or 0)))
    total = len(hits)
    amount = sum(l.amount or 0 for l, _ in hits.values())
    return [l for l, _ in fresh[: (limit or ch.per_post)]], total, amount


def _money(v: float | None) -> str:
    if not v:
        return "сумма не указана"
    if v >= 1e9:
        return f"{v / 1e9:.1f} млрд ₸".replace(".", ",")
    if v >= 1e6:
        return f"{v / 1e6:.1f} млн ₸".replace(".", ",")
    return f"{int(v):,} ₸".replace(",", " ")


def channel_post_text(ch: Channel, lots: list[Lot], total: int, amount: float, bot: str) -> str:
    esc = html.escape
    title = esc(ch.title) + (f" · {esc(ch.region)}" if ch.region else "")
    lines = [f"<b>Тендеры дня — {title}</b>", ""]
    for i, l in enumerate(lots, 1):
        dl = aware(l.deadline)
        when = dl.astimezone(config.TZ).strftime("%d.%m %H:%M") if dl else "—"
        link = f'<a href="{esc(l.url)}">{esc(l.title[:140])}</a>' if l.url else esc(l.title[:140])
        lines.append(f"{i}. {link}\n💰 {_money(l.amount)} · ⏳ до {when}\n🏛 {esc((l.customer or '')[:90])}"
                     + (f"\n📍 {esc(l.region[:60])}" if l.region else ""))
        lines.append("")
    lines.append(f"Сейчас открыто {total} подходящих тендеров на {_money(amount)}.")
    lines.append(f"Kyran присылает такие тендеры под вашу компанию через 5 минут после публикации, "
                 f"разбирает документацию и готовит документы заявки. Неделя PRO бесплатно: @{bot}")
    return "\n".join(lines)


async def mark_posted(s: AsyncSession, ch: Channel, lots: list[Lot]) -> None:
    for l in lots:
        s.add(ChannelPost(channel_id=ch.id, lot_id=l.id))
    await s.commit()


async def channel_stats(s: AsyncSession) -> list[tuple[Channel, int, int]]:
    """(канал, пришло пользователей, из них платят)."""
    out = []
    for ch in (await s.execute(select(Channel).order_by(Channel.id))).scalars().all():
        users = (await s.execute(select(User).where(User.source == f"ch_{ch.id}"))).scalars().all()
        from app.services.billing import PAID, raw_plan

        paying = sum(1 for u in users if u.plan in PAID and raw_plan(u)[1])
        out.append((ch, len(users), paying))
    return out


# ── Источник и партнёры ────────────────────────────────────

async def attribute(s: AsyncSession, user: User, arg: str) -> str:
    """Запоминает, откуда пришёл человек (только для новых, ещё не настроенных). Возвращает ''/'channel'/'partner'."""
    if not arg or user.onboarded or user.source:
        return ""
    if arg.startswith("ch_") and arg[3:].isdigit():
        user.source = arg[:40]
        await s.commit()
        return "channel"
    if arg.startswith("ref_") and arg[4:].isdigit():
        partner_id = int(arg[4:])
        if partner_id != user.id and await s.get(User, partner_id):
            user.source, user.referred_by = arg[:40], partner_id
            await s.commit()
            return "partner"
    return ""


async def on_payment(s: AsyncSession, client: User, plan_code: str, months: int) -> PartnerEarning | None:
    """Вызывается при подключении оплаченного тарифа (/grant): начисляет партнёру его долю."""
    from app.services.billing import PLANS

    if not client.referred_by or plan_code not in PLANS:
        return None
    first = (await s.execute(select(func.min(PartnerEarning.created_at)).where(PartnerEarning.client_id == client.id))).scalar()
    if first and aware(first) < utcnow() - timedelta(days=30 * PARTNER_MONTHS):
        return None  # партнёр получает долю только первые 12 месяцев клиента
    amount = PLANS[plan_code].price * months
    e = PartnerEarning(partner_id=client.referred_by, client_id=client.id, plan=plan_code, months=months,
                       amount=amount, commission=int(round(amount * PARTNER_SHARE)))
    s.add(e)
    await s.commit()
    return e


async def partner_summary(s: AsyncSession, partner: User) -> dict:
    invited = (await s.execute(select(func.count(User.id)).where(User.referred_by == partner.id))).scalar() or 0
    rows = (await s.execute(select(PartnerEarning).where(PartnerEarning.partner_id == partner.id))).scalars().all()
    return {
        "invited": int(invited),
        "paying": len({r.client_id for r in rows}),
        "earned": sum(r.commission for r in rows),
        "to_pay": sum(r.commission for r in rows if not r.paid_out),
    }


async def partners_debt(s: AsyncSession) -> list[tuple[User, int]]:
    rows = (await s.execute(select(PartnerEarning.partner_id, func.sum(PartnerEarning.commission))
                            .where(PartnerEarning.paid_out.is_(False)).group_by(PartnerEarning.partner_id))).all()
    out = []
    for pid, total in rows:
        u = await s.get(User, pid)
        if u:
            out.append((u, int(total or 0)))
    return out
