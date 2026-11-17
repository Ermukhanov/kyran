"""Telegram-бот: онбординг, ИИ-агент в чате, уведомления."""
from __future__ import annotations

import logging
import re
from datetime import time as dtime, timedelta

from sqlalchemy import and_, select
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MenuButtonWebApp, Update, WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters,
)

from app import config, db
from app.ai import agent, client as ai
from app.api.auth import make_link_token
from app.bot import texts as T
from app.db import Lot, Match, User, utcnow
from app.services import core, monitors
from app.services.matcher import parse_keywords
from app.sources.registry import SOURCES

log = logging.getLogger(__name__)
KEYWORDS, AMOUNT, BIN = range(3)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def app_url(path: str = "") -> str:
    return f"{config.PUBLIC_URL}/app{path}" if config.PUBLIC_URL else ""


def has_data() -> bool:
    return any(s.enabled for s in SOURCES)


def lot_buttons(lot: Lot) -> InlineKeyboardMarkup:
    row = []
    if config.PUBLIC_URL:
        row.append(InlineKeyboardButton("🧠 Разбор", web_app=WebAppInfo(app_url(f"#lot/{lot.id}"))))
    row.append(InlineKeyboardButton("⭐ В воронку", callback_data=f"save:{lot.id}"))
    rows = [row, [InlineKeyboardButton("🙈 Не интересно", callback_data=f"hide:{lot.id}")]]
    if lot.url:
        rows[1].append(InlineKeyboardButton("🔗 На портале", url=lot.url))
    return InlineKeyboardMarkup(rows)


def open_app_markup(text: str = "📱 Открыть Kyran") -> InlineKeyboardMarkup | None:
    if not config.PUBLIC_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, web_app=WebAppInfo(app_url()))]])


async def reply(update: Update, text: str, **kw) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, **kw)


async def load_user(update: Update) -> User:
    tg = update.effective_user
    async with db.Session() as s:
        return await core.get_or_create_user(s, tg.id, tg.full_name, tg.username or "")


# ── Онбординг ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await load_user(update)
    await reply(update, T.WELCOME)
    return KEYWORDS


async def got_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kws = parse_keywords(update.message.text)
    if not kws:
        await reply(update, T.BAD_KEYWORDS)
        return KEYWORDS
    context.user_data["keywords"] = kws
    buttons = [[InlineKeyboardButton(label, callback_data=f"amt:{key}")] for key, (label, _, _) in T.AMOUNTS.items()]
    await reply(update, T.ASK_AMOUNT, reply_markup=InlineKeyboardMarkup(buttons))
    return AMOUNT


async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    label, lo, hi = T.AMOUNTS.get(q.data.split(":", 1)[1], T.AMOUNTS["any"])
    context.user_data["amount"] = (lo, hi)
    await q.edit_message_text(f"💰 {label}")
    await reply(update, T.ASK_BIN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить →", callback_data="bin:skip")]]))
    return BIN


async def got_bin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    digits = re.sub(r"\D", "", update.message.text or "")
    if len(digits) != 12:
        await reply(update, T.BAD_BIN)
        return BIN
    return await finish(update, context, digits)


async def skip_bin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(None)
    return await finish(update, context, "")


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE, company_bin: str) -> int:
    await update.effective_chat.send_action(ChatAction.TYPING)
    lo, hi = context.user_data.get("amount", (None, None))
    tg = update.effective_user
    async with db.Session() as s:
        user = await core.get_or_create_user(s, tg.id, tg.full_name, tg.username or "")
        user.keywords, user.min_amount, user.max_amount = context.user_data.get("keywords", []), lo, hi
        user.onboarded, user.notify = True, True
        if company_bin:
            user.company_bin = company_bin
            gz = next((x for x in SOURCES if x.name == "goszakup"), None)
            info = await gz.company(company_bin) if gz and gz.enabled else None
            if info and info.get("name"):
                user.company_name = info["name"]
        await s.commit()
        await core.rematch_user(s, user)
        stats = await core.user_stats(s, user)
        top = await core.feed(s, user, limit=3)
    context.user_data.clear()

    if not has_data():
        await reply(update, "✅ Профиль готов.\n\n" + T.NO_DATA, reply_markup=open_app_markup())
        return ConversationHandler.END

    head = (
        f"✅ Готово! Под вас прямо сейчас открыто <b>{stats['open']}</b> тендеров "
        f"на <b>{T.money(stats['total_amount'], short=True)}</b>"
    )
    if stats["urgent"]:
        head += f", из них <b>{stats['urgent']}</b> с дедлайном в ближайшие {config.URGENT_DAYS} дня"
    await reply(update, head + ".", reply_markup=open_app_markup("📱 Открыть все в Kyran"))
    for lot, m, _ in top:
        await reply(update, T.card(lot, m.score, m.reason), reply_markup=lot_buttons(lot))
    await reply(update, "Дальше пришлю срочные сразу, а подборку — каждое утро в 9:00. Можете писать мне обычным текстом: <i>найди ремонт спортзалов</i>.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await reply(update, "Отменено. /start — настроить заново.")
    return ConversationHandler.END


# ── Команды ────────────────────────────────────────────────

async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    markup = open_app_markup()
    await reply(update, "Лента, разборы и воронка — в приложении:" if markup else "Приложение ещё не подключено (нужен PUBLIC_URL).", reply_markup=markup)


async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.PUBLIC_URL:
        await reply(update, "Веб-версия ещё не подключена.")
        return
    token = make_link_token(update.effective_user.id)
    await reply(update, "💻 Ссылка для компьютера (личная, не пересылайте, действует 30 дней):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Kyran в браузере", url=f"{config.PUBLIC_URL}/app?t={token}")]]))


async def show_feed(update: Update, only_urgent: bool) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        if not user.onboarded:
            await reply(update, "Сначала расскажите, что вы поставляете: /start")
            return
        rows = await core.feed(s, user, only_urgent=only_urgent, limit=5)
    if not has_data():
        await reply(update, T.NO_DATA)
        return
    if not rows:
        await reply(update, "Сейчас подходящих открытых тендеров нет. Как появятся — пришлю.")
        return
    for lot, m, _ in rows:
        await reply(update, T.card(lot, m.score, m.reason), reply_markup=lot_buttons(lot))


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_feed(update, only_urgent=False)


async def urgent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_feed(update, only_urgent=True)


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        u = await core.get_or_create_user(s, update.effective_user.id)
        stats = await core.user_stats(s, u)
    sums = next((lbl for lbl, lo, hi in T.AMOUNTS.values() if (lo, hi) == (u.min_amount, u.max_amount)), "своя настройка")
    from html import escape

    text = (
        "<b>Ваш профиль</b>\n"
        f"🔎 Направления: {escape(', '.join(u.keywords or []) or '—')}\n"
        f"💰 Суммы: {sums}\n"
        f"🏢 БИН: {u.company_bin or 'не указан'}\n"
        f"🔔 Уведомления: {'включены' if u.notify else 'на паузе'}\n\n"
        f"Открыто под вас: {stats['open']} на {T.money(stats['total_amount'], short=True)}"
    )
    await reply(update, text, reply_markup=open_app_markup("⚙️ Изменить в приложении"))


async def toggle_notify(update: Update, on: bool) -> None:
    async with db.Session() as s:
        u = await core.get_or_create_user(s, update.effective_user.id)
        u.notify = on
        await s.commit()
    await reply(update, "▶️ Уведомления включены." if on else "⏸ Уведомления на паузе. /resume — включить.")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_notify(update, False)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_notify(update, True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, T.HELP, reply_markup=open_app_markup())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in config.ADMIN_IDS:
        return
    from sqlalchemy import func

    async with db.Session() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar()
        onboarded = (await s.execute(select(func.count(User.id)).where(User.onboarded.is_(True)))).scalar()
        lots = (await s.execute(select(func.count(Lot.id)))).scalar()
        open_lots = (await s.execute(select(func.count(Lot.id)).where(core.open_condition()))).scalar()
    srcs = ", ".join(f"{x.name}: {'вкл' if x.enabled else 'выкл'}" for x in SOURCES)
    await reply(update, f"👥 {users} пользователей ({onboarded} настроили)\n📦 {lots} лотов ({open_lots} открыто)\n🔌 {srcs}\n🧠 ИИ: {'вкл' if ai.available() else 'выкл'}")


# ── Кнопки под карточками ──────────────────────────────────

async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action, lot_id = q.data.split(":", 1)
    async with db.Session() as s:
        user = await core.get_or_create_user(s, q.from_user.id)
        if action == "save":
            await core.set_stage(s, user, int(lot_id), "saved")
            await q.answer("⭐ Добавлено в воронку «Интересно»")
        elif action == "hide":
            m = (await s.execute(select(Match).where(Match.user_id == user.id, Match.lot_id == int(lot_id)))).scalar_one_or_none()
            if m:
                m.hidden = True
                await s.commit()
            await q.answer("Скрыто. Похожие буду показывать реже.")
            await q.edit_message_reply_markup(None)


# ── Свободный текст: ИИ-агент или поиск ────────────────────

async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    await update.effective_chat.send_action(ChatAction.TYPING)

    if ai.available():
        history = context.user_data.setdefault("history", [])
        async with db.Session() as s:
            user = await core.get_or_create_user(s, update.effective_user.id, update.effective_user.full_name)
            try:
                answer, run = await agent.respond(s, user, history, text)
            except Exception:
                log.exception("Агент упал")
                answer, run = "Не получилось ответить, попробуйте ещё раз через минуту.", None
            lots = []
            if run and run.shown_lots:
                lots = [await s.get(Lot, i) for i in dict.fromkeys(run.shown_lots)][:5]
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
        del history[:-10]
        await update.effective_message.reply_text(answer, link_preview_options=NO_PREVIEW)
        for lot in lots:
            if lot:
                await reply(update, T.card(lot), reply_markup=lot_buttons(lot))
        return

    # Без ИИ: ссылка → мониторинг сайта, иначе поиск по словам
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id, update.effective_user.full_name)
        if re.match(r"^(https?://|www\.)\S+$", text):
            try:
                m = await monitors.add_monitor(s, user, text)
                await reply(update, f"👀 Слежу за сайтом «{m.title}». Сообщу, когда там что-то изменится.")
            except Exception as e:
                await reply(update, f"Не получилось открыть сайт: {e}")
            return
        hits = await core.search(s, text, core.profile_of(user), limit=5)
    if not has_data():
        await reply(update, T.NO_DATA)
        return
    if not hits:
        await reply(update, "По этому запросу открытых тендеров нет. Добавить его в направления? Напишите /start и укажите его.")
        return
    await reply(update, f"🔎 Нашёл по запросу «{text[:60]}»:")
    for lot, score, reason in hits:
        await reply(update, T.card(lot, score, reason), reply_markup=lot_buttons(lot))


# ── Фоновые задачи ─────────────────────────────────────────

async def send_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lot: Lot, m: Match) -> bool:
    try:
        await context.bot.send_message(chat_id, T.card(lot, m.score, m.reason), parse_mode=ParseMode.HTML,
                                       link_preview_options=NO_PREVIEW, reply_markup=lot_buttons(lot))
        return True
    except Exception:
        log.exception("Не отправилось пользователю %s", chat_id)
        return False


async def collect_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Забрать новые лоты, подобрать всем, срочные отправить сразу."""
    async with db.Session() as s:
        new = await core.collect(s, SOURCES)
        if not new:
            return
        matches = await core.match_new_lots(s, new)
        log.info("Новых лотов: %d, подборок для %d пользователей", len(new), len(matches))
        for user_id, ms in matches.items():
            user = await s.get(User, user_id)
            if not user or not user.notify:
                continue
            urgent_until = utcnow() + timedelta(days=config.URGENT_DAYS)
            sent = 0
            for m in sorted(ms, key=lambda x: -x.score):
                lot = await s.get(Lot, m.lot_id)
                deadline = db.aware(lot.deadline)
                if deadline and deadline <= urgent_until and sent < 5:
                    if await send_card(context, user_id, lot, m):
                        m.notified, sent = True, sent + 1
        await s.commit()


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утренняя подборка: лучшие ещё не отправленные тендеры."""
    async with db.Session() as s:
        users = (await s.execute(select(User).where(User.onboarded.is_(True), User.notify.is_(True)))).scalars().all()
        for user in users:
            rows = (await s.execute(
                select(Lot, Match).join(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
                .where(Match.notified.is_(False), Match.hidden.is_(False), core.open_condition())
                .order_by(Match.score.desc()).limit(config.DIGEST_SIZE)
            )).all()
            if not rows:
                continue
            text = T.digest([(l, m.score) for l, m in rows], "☀️ <b>Тендеры на сегодня</b>")
            try:
                await context.bot.send_message(user.id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                               reply_markup=open_app_markup("📱 Смотреть и разобрать"))
                for _, m in rows:
                    m.notified = True
            except Exception:
                log.exception("Дайджест не отправился %s", user.id)
        await s.commit()


async def monitors_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        results = await monitors.check_all(s)
    for r in results:
        if not r.changed:
            continue
        m = r.monitor
        if r.new_lots:
            lines = "\n".join(f"• {l.title[:120]}" for l in r.new_lots[:8])
            text = f"🆕 На сайте «{m.title}» новые закупки:\n{lines}"
        else:
            text = f"👀 На сайте «{m.title}» что-то изменилось."
        try:
            await context.bot.send_message(
                m.user_id, text, link_preview_options=NO_PREVIEW,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть сайт", url=m.url)]]),
            )
        except Exception:
            log.exception("Уведомление о сайте не отправилось")


# ── Сборка приложения ──────────────────────────────────────

async def post_init(application: Application) -> None:
    if config.PUBLIC_URL:
        try:
            await application.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Kyran", WebAppInfo(app_url())))
        except Exception:
            log.exception("Не удалось поставить кнопку меню")
    await application.bot.set_my_commands([
        ("app", "Открыть Kyran"), ("find", "Лучшие тендеры сейчас"), ("urgent", "Срочные"),
        ("profile", "Мой профиль"), ("web", "Ссылка для компьютера"), ("start", "Настроить заново"), ("help", "Помощь"),
    ])


def build() -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_keywords)],
            AMOUNT: [CallbackQueryHandler(got_amount, pattern=r"^amt:")],
            BIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_bin), CallbackQueryHandler(skip_bin, pattern=r"^bin:skip$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    application.add_handler(onboarding)
    for name, fn in [("app", app_cmd), ("web", web_cmd), ("find", find_cmd), ("urgent", urgent_cmd), ("profile", profile_cmd),
                     ("stop", stop_cmd), ("resume", resume_cmd), ("help", help_cmd), ("stats", stats_cmd)]:
        application.add_handler(CommandHandler(name, fn))
    application.add_handler(CallbackQueryHandler(on_card_button, pattern=r"^(save|hide):\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    jq = application.job_queue
    jq.run_repeating(collect_job, interval=config.COLLECT_EVERY_MIN * 60, first=5)
    jq.run_repeating(monitors_job, interval=config.WEBSITE_EVERY_MIN * 60, first=120)
    jq.run_daily(digest_job, time=dtime(hour=config.DIGEST_HOUR, tzinfo=config.TZ))
    return application
