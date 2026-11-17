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
    "<b>Пришлите БИН вашей компании</b> — найду закупки, которые вы уже выигрывали, и настрою подбор сам.\n\n"
    "Или напишите направления через запятую, например:\n"
    "<i>ремонт кровли, ремонт школ, канцтовары, бумага А4</i>\n\n"
    "Входите на сайт? Просто отправьте сюда 6-значный код со страницы входа."
)
ASK_AMOUNT = "Укажите интересующий диапазон сумм закупок:"
ASK_BIN = "Укажите <b>БИН</b> компании (12 цифр). Шаг можно пропустить."
BAD_KEYWORDS = "Напишите хотя бы одно направление, например: <i>ремонт кровли</i>"
BAD_BIN = "БИН/ИИН — это 12 цифр. Напишите ещё раз или нажмите «Пропустить»."
NO_DATA = (
    "Источник данных подключается. Как только поступят закупки, подходящие придут сюда автоматически."
)
HELP = (
    "<b>Команды</b>\n"
    "/find — лучшие закупки под ваш профиль\n"
    "/urgent — закупки со сроком подачи в ближайшие дни\n"
    "/app — открыть Kyran: закупки, разборы, документы, заявки\n"
    "/web — ссылка для входа с компьютера\n\n"
    "<b>Разбор ТЗ</b>\n"
    "Перешлите сюда техспецификацию или документацию закупки — PDF, Word или фото страниц. "
    "Через минуту придёт разбор, а кнопками под ним — техпредложение и другие документы в Word.\n\n"
    "<b>Мониторинг сайтов заказчиков</b>\n"
    "/watch &lt;адрес&gt; — следить за страницей закупок\n"
    "/sites — список, /unwatch &lt;номер&gt; — убрать\n\n"
    "<b>Конкуренты</b>\n"
    "/follow &lt;БИН&gt; — следить за конкурентом: сообщу о каждой его победе\n"
    "/rivals — список, /unfollow &lt;БИН&gt; — убрать\n\n"
    "<b>Отчёт</b>\n"
    "/report — что прошло мимо вас за неделю: суммы, победители, снижение цены. "
    "Приходит сам по понедельникам.\n\n"
    "<b>Аккаунт</b>\n"
    "/profile — профиль, /plan — тариф и лимиты\n"
    "/stop и /resume — пауза и включение уведомлений\n"
    "/logout_all — выйти на сайте со всех устройств\n\n"
    "Можно писать обычным текстом: <i>найди ремонт школ до 50 млн</i>. "
    "Вход на сайт: отправьте сюда 6-значный код со страницы входа."
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


VERDICT = {"go": "Рекомендуем участвовать", "consider": "Требует оценки", "skip": "Участие не рекомендуется"}
LEVEL = {"high": "высокий", "medium": "средний", "low": "низкий"}


def upload_report(lot: Lot, a: dict, limit_chars: int = 3900) -> str:
    """Разбор присланного ТЗ одним сообщением (≤ 4096 символов Telegram)."""
    score = f" · {a['score']}%" if isinstance(a.get("score"), (int, float)) else ""
    parts = [f"<b>{VERDICT.get(a.get('verdict'), VERDICT['consider'])}{score}</b>",
             f"<b>{escape(lot.title[:250])}</b>"]
    facts = []
    if lot.customer:
        facts.append(f"Заказчик: {escape(lot.customer[:160])}")
    if lot.amount:
        facts.append(f"Сумма: {money(lot.amount)}")
    if lot.deadline:
        facts.append(f"Срок: {time_left(lot.deadline)}")
    if facts:
        parts.append("\n".join(facts))
    if a.get("summary"):
        parts.append(escape(str(a["summary"])[:600]))

    def block(title: str, items: list[str], n: int) -> None:
        items = [str(x) for x in items if x][:n]
        if items:
            parts.append(f"<b>{title}</b>\n" + "\n".join(f"• {escape(x[:220])}" for x in items))

    reqs = [r.get("text", "") if isinstance(r, dict) else r for r in a.get("requirements") or []]
    risks = [(f"{r.get('text', '')} ({LEVEL.get(r.get('level'), '—')})" if isinstance(r, dict) else r) for r in a.get("risks") or []]
    block("Требования к поставщику", reqs, 6)
    block("Документы для заявки", a.get("documents") or [], 8)
    block("Риски", risks, 4)
    block("Вопросы заказчику", a.get("questions") or [], 3)
    text = ""
    for p in parts:
        if len(text) + len(p) + 2 > limit_chars:
            break
        text += ("\n\n" if text else "") + p
    return text + "\n\n<i>Разбор справочный — сверяйте с документацией.</i>"


def weekly_report(r: dict, limit_chars: int = 3900) -> str:
    m, act = r["missed"], r["activity"]
    lines = ["<b>Итоги недели</b>"]
    if m["count"]:
        lines.append(f"Мимо вас прошло <b>{m['count']}</b> подходящих закупок на <b>{money(m['total'], short=True)}</b> — "
                     "их выиграли другие поставщики.")
        if m["median_discount"] is not None:
            lines.append(f"Победители снижали цену в среднем на {m['median_discount']}%.")
        if r["winners"]:
            lines.append("\n<b>Кто забрал больше всего</b>\n" + "\n".join(
                f"• {escape(w['name'][:80])} — {w['wins']} шт., {money(w['total'], short=True)}" for w in r["winners"][:3]))
        lines.append("\n<b>Крупнейшие</b>\n" + "\n".join(
            f"• {escape(a['title'][:90])} — {money(a['price'], short=True)}{' (−' + str(a['discount']) + '%)' if a['discount'] else ''}"
            for a in m["items"][:3]))
    else:
        lines.append("За неделю подходящих итогов закупок не было.")
    if r["won"]["count"]:
        lines.append(f"\nВаши победы: {r['won']['count']} на {money(r['won']['total'], short=True)}.")
    st = act["stages"]
    done = []
    if act["new_matches"]:
        done.append(f"новых подходящих закупок — {act['new_matches']}")
    if st.get("submitted"):
        done.append(f"подано заявок — {st['submitted']}")
    if st.get("won"):
        done.append(f"побед — {st['won']}")
    if done:
        lines.append("\n<b>Ваша неделя:</b> " + ", ".join(done) + ".")
    if r["soon"]:
        lines.append("\n<b>Не упустите — закрываются в ближайшие дни</b>\n" + "\n".join(
            f"• {escape(x['title'][:90])} — {money(x['amount'], short=True)}, {time_left(datetime.fromisoformat(x['deadline']) if x['deadline'] else None)}"
            for x in r["soon"]))
    if r.get("is_demo"):
        lines.append("\n<i>По демонстрационным данным.</i>")
    text = "\n".join(lines)
    return text[:limit_chars]
