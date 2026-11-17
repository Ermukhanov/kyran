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
    "<b>Kyran</b> — платформа для участия в госзакупках.\n\n"
    "Kyran ежедневно отбирает открытые закупки под профиль вашей компании, "
    "готовит ИИ-заключение по документации и контролирует сроки подачи.\n\n"
    "<b>Что вы поставляете или какие работы выполняете?</b>\n"
    "Напишите через запятую, например:\n"
    "<i>ремонт кровли, ремонт школ, канцтовары, бумага А4</i>"
)
ASK_AMOUNT = "Укажите интересующий диапазон сумм закупок:"
ASK_BIN = "Укажите <b>БИН</b> компании (12 цифр). Шаг можно пропустить."
BAD_KEYWORDS = "Напишите хотя бы одно направление, например: <i>ремонт кровли</i>"
BAD_BIN = "БИН/ИИН — это 12 цифр. Напишите ещё раз или нажмите «Пропустить»."
NO_DATA = (
    "Источник данных подключается. Как только поступят закупки, подходящие придут сюда автоматически."
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
        return f"до {when}, осталось {int(hours)} ч"
    days = int(hours // 24)
    return f"до {when}, осталось {days} дн."


def card(lot: Lot, score: int | None = None, reason: str = "") -> str:
    lines = []
    if lot.is_demo:
        lines.append("<i>Демонстрационная закупка</i>")
    head = f"<b>{escape(lot.title[:250])}</b>"
    if score is not None:
        head = f"<b>{score}%</b> · " + head
    lines.append(head)
    lines.append(f"Сумма: {money(lot.amount)}")
    lines.append(f"Срок: {time_left(lot.deadline)}")
    if lot.customer:
        lines.append(f"Заказчик: {escape(lot.customer[:160])}")
    if reason:
        lines.append(f"<i>{escape(reason[:200])}</i>")
    return "\n".join(lines)


def digest(rows: list[tuple[Lot, int]], header: str, limit_chars: int = 3800) -> str:
    text = header
    for i, (lot, score) in enumerate(rows, 1):
        demo = " (демо)" if lot.is_demo else ""
        item = (
            f"\n\n<b>{i}. {escape(lot.title[:160])}</b>{demo}\n"
            f"{score}% · {money(lot.amount, short=True)} · {time_left(lot.deadline)}"
        )
        if len(text) + len(item) > limit_chars:
            text += f"\n\n…и ещё {len(rows) - i + 1} в приложении"
            break
        text += item
    return text
