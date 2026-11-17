"""Тексты бота и форматирование карточек (HTML-разметка Telegram)."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from app import config
from app.db import Lot, aware

AMOUNTS = {
    "any": ("Любые суммы", None, None),
    "lt5": ("До 5 млн ₸", None, 5_000_000),
    "5to50": ("5–50 млн ₸", 5_000_000, 50_000_000),
    "50to300": ("50–300 млн ₸", 50_000_000, 300_000_000),
    "gt300": ("От 300 млн ₸", 300_000_000, None),
}

WELCOME = (
    "🦅 <b>Kyran</b> — ваш ИИ-агент для госзакупок.\n\n"
    "Я каждый день просматриваю тендеры, присылаю только подходящие вам, "
    "разбираю документацию и напоминаю о дедлайнах.\n\n"
    "<b>Что вы поставляете или какие работы выполняете?</b>\n"
    "Напишите через запятую, например:\n"
    "<i>ремонт кровли, ремонт школ, канцтовары, бумага А4</i>"
)
ASK_AMOUNT = "💰 Какие суммы закупок вам интересны?"
ASK_BIN = "🏢 Напишите <b>БИН</b> компании (12 цифр) — так агент лучше поймёт ваш профиль. Можно пропустить."
BAD_KEYWORDS = "Напишите хотя бы одно направление, например: <i>ремонт кровли</i>"
BAD_BIN = "БИН/ИИН — это 12 цифр. Напишите ещё раз или нажмите «Пропустить»."
NO_DATA = (
    "⏳ Подключаем базу госзакупок. Как только данные пойдут, подходящие тендеры придут сюда сами — "
    "ничего делать не нужно."
)
HELP = (
    "<b>Что я умею</b>\n"
    "• Пишите как человеку: <i>найди ремонт школ до 50 млн</i>, <i>следи за сайтом zakup.kz</i>\n"
    "• /app — открыть Kyran (лента, разборы, воронка)\n"
    "• /urgent — тендеры с дедлайном в ближайшие дни\n"
    "• /find — лучшие тендеры под вас сейчас\n"
    "• /web — ссылка для компьютера\n"
    "• /profile — профиль, /start — настроить заново\n"
    "• /stop и /resume — пауза и включение уведомлений"
)


def money(amount: float | None, short: bool = False) -> str:
    if amount is None:
        return "сумма не указана"
    if short and amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.1f}".removesuffix(".0").replace(".", ",") + " млрд ₸"
    if short and amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}".removesuffix(".0").replace(".", ",") + " млн ₸"
    return f"{amount:,.0f}".replace(",", " ") + " ₸"


def time_left(deadline: datetime | None) -> str:
    d = aware(deadline)
    if not d:
        return "срок не указан"
    hours = (d - datetime.now(timezone.utc)).total_seconds() / 3600
    when = d.astimezone(config.TZ).strftime("%d.%m %H:%M")
    if hours < 0:
        return f"приём завершён {when}"
    if hours < 24:
        return f"🔥 осталось {int(hours)} ч (до {when})"
    days = int(hours // 24)
    mark = "⚡" if days < config.URGENT_DAYS else "⏰"
    return f"{mark} {days} дн. (до {when})"


def card(lot: Lot, score: int | None = None, reason: str = "") -> str:
    lines = []
    if lot.is_demo:
        lines.append("🧪 <i>ДЕМО-пример</i>")
    head = f"<b>{escape(lot.title[:250])}</b>"
    if score is not None:
        head = f"<b>{score}%</b> · " + head
    lines.append(head)
    lines.append(f"💰 {money(lot.amount)}  ·  {time_left(lot.deadline)}")
    if lot.customer:
        lines.append(f"🏢 {escape(lot.customer[:160])}")
    if reason:
        lines.append(f"💡 {escape(reason[:200])}")
    return "\n".join(lines)


def digest(rows: list[tuple[Lot, int]], header: str, limit_chars: int = 3800) -> str:
    text = header
    for i, (lot, score) in enumerate(rows, 1):
        demo = " 🧪" if lot.is_demo else ""
        item = (
            f"\n\n<b>{i}. {escape(lot.title[:160])}</b>{demo}\n"
            f"{score}% · {money(lot.amount, short=True)} · {time_left(lot.deadline)}"
        )
        if len(text) + len(item) > limit_chars:
            text += f"\n\n…и ещё {len(rows) - i + 1} в приложении"
            break
        text += item
    return text
