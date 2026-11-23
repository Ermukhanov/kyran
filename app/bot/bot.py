"""Telegram-бот: онбординг, ИИ-агент в чате, уведомления."""
from __future__ import annotations

import logging
import re
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import and_, func, select
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MenuButtonWebApp, Update, WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from app import config, db, runtime
from app.ai import agent, client as ai
from app.api.auth import make_link_token
from app.bot import texts as T
from app.db import Channel, Document, Lot, Match, Monitor, PartnerEarning, PipelineItem, UpgradeRequest, User, aware, utcnow
from app.services import billing, competitors as rivals, core, docgen, growth, login, monitors, report, uploads
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
        row.append(InlineKeyboardButton("Разбор", web_app=WebAppInfo(app_url(f"#lot/{lot.id}"))))
    row.append(InlineKeyboardButton("В воронку", callback_data=f"save:{lot.id}"))
    rows = [row, [InlineKeyboardButton("📄 Документы заявки", callback_data=f"docs:{lot.id}")],
            [InlineKeyboardButton("Не интересно", callback_data=f"hide:{lot.id}")]]
    if lot.url:
        rows[2].append(InlineKeyboardButton("На портале", url=lot.url))
    return InlineKeyboardMarkup(rows)


def pro_markup(user: User | None) -> InlineKeyboardMarkup | None:
    """Кнопки под сообщением об ограничении: пробная неделя (если ещё не было) и тарифы."""
    rows = []
    if user is not None and billing.trial_available(user):
        rows.append([InlineKeyboardButton("🎁 PRO 7 дней бесплатно", callback_data="trial:start")])
    if config.PUBLIC_URL:
        rows.append([InlineKeyboardButton("Активировать PRO", web_app=WebAppInfo(app_url("#profile/plan")))])
    return InlineKeyboardMarkup(rows) if rows else None


def open_app_markup(text: str = "Открыть Kyran") -> InlineKeyboardMarkup | None:
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

async def confirm_login(update: Update, *, code: str | None = None, nonce: str | None = None) -> bool:
    user = await load_user(update)
    async with db.Session() as s:
        ok = await login.bind(s, user.id, code=code, nonce=nonce)
    if ok:
        await reply(update, "Вход на сайт подтверждён. Вернитесь в браузер — страница откроется сама.\n\n"
                            "Если входили не вы, нажмите /logout_all — это завершит все сессии на сайте.")
    else:
        await reply(update, "Код не найден или устарел. Обновите страницу входа на сайте и попробуйте снова.")
    return ok


LOGIN_CODE = re.compile(r"^\s*(\d{3})\s*-?\s*(\d{3})\s*$")


async def login_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start login_<nonce> — переход с сайта. Работает в любом состоянии диалога."""
    arg = context.args[0] if context.args else ""
    ok = await confirm_login(update, nonce=arg[6:])
    user = await load_user(update)
    if ok and not user.onboarded:
        await reply(update, "Чтобы получать подходящие закупки в Telegram, настройте подбор: /start")
    raise ApplicationHandlerStop


async def login_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """6 цифр с сайта (можно с пробелом или дефисом). Работает в любом состоянии диалога."""
    m = LOGIN_CODE.match(update.message.text or "")
    await confirm_login(update, code=m.group(1) + m.group(2))
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = await load_user(update)
    arg = context.args[0] if context.args else ""
    if arg:
        async with db.Session() as s:
            u = await s.get(User, user.id)
            how = await growth.attribute(s, u, arg) if u else ""
        if how == "partner" and u and billing.current_plan(u)[0].code == "trial":
            await reply(update, "Вас пригласил партнёр Kyran — для вас неделя PRO бесплатно уже включена.")
    await reply(update, T.WELCOME)
    return KEYWORDS


async def got_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if re.fullmatch(r"\d{12}", re.sub(r"\s", "", text)):
        return await onboard_by_bin(update, context, re.sub(r"\s", "", text))
    kws = parse_keywords(text)
    if not kws:
        await reply(update, T.BAD_KEYWORDS)
        return KEYWORDS
    context.user_data["keywords"] = kws
    buttons = [[InlineKeyboardButton(label, callback_data=f"amt:{key}")] for key, (label, _, _) in T.AMOUNTS.items()]
    await reply(update, T.ASK_AMOUNT, reply_markup=InlineKeyboardMarkup(buttons))
    return AMOUNT


async def onboard_by_bin(update: Update, context: ContextTypes.DEFAULT_TYPE, bin_: str) -> int:
    from app.services import autoprofile

    await update.effective_chat.send_action(ChatAction.TYPING)
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id, update.effective_user.full_name)
        try:
            r = await autoprofile.preview(s, user, bin_)
        except rivals.CompetitorError as e:
            await reply(update, T.escape(str(e)))
            return KEYWORDS
        if not r["found"]:
            await reply(update, "По этому БИН выигранных закупок не нашлось. Напишите направления через запятую, "
                                "например: <i>ремонт кровли, канцтовары</i>")
            return KEYWORDS
        if r["about"] and not user.about:
            user.about = r["about"]
        if r["name"] and not r["demo"] and not user.company_name:
            user.company_name = r["name"]
        await s.commit()
    demo = (f"<i>Демо-режим: портал госзакупок ещё не подключён, показан пример по данным компании {T.escape(r['demo_company'])}.</i>\n\n"
            if r["demo"] else "")
    await reply(update, f"{demo}Нашёл <b>{r['wins']}</b> выигранных закупок на <b>{T.money(r['total'], short=True)}</b>.\n"
                        f"Направления: <b>{T.escape(', '.join(r['keywords']))}</b>\n"
                        "Изменить их можно в любой момент на сайте: Профиль → Подбор закупок.")
    context.user_data["keywords"] = r["keywords"]
    context.user_data["amount"] = (None, None)
    return await finish(update, context, bin_)


async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    label, lo, hi = T.AMOUNTS.get(q.data.split(":", 1)[1], T.AMOUNTS["any"])
    context.user_data["amount"] = (lo, hi)
    await q.edit_message_text(f"{label}")
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
        await reply(update, "🔎 Ищу открытые закупки под ваш профиль по всем площадкам…")
        if not await core.index_is_full(s):  # база ещё не собрана целиком — дотягиваем по словам
            await core.fetch_for(s, user.keywords or [])
        await core.rematch_user(s, user)
        stats = await core.user_stats(s, user)
        top = await core.feed(s, user, limit=3)
    context.user_data.clear()

    if not has_data():
        await reply(update, "Профиль готов.\n\n" + T.NO_DATA, reply_markup=open_app_markup())
        return ConversationHandler.END

    head = (
        f"Готово. Под вас прямо сейчас открыто <b>{stats['open']}</b> тендеров "
        f"на <b>{T.money(stats['total_amount'], short=True)}</b>"
    )
    if stats["urgent"]:
        head += f", из них <b>{stats['urgent']}</b> с дедлайном в ближайшие {config.URGENT_DAYS} дня"
    await reply(update, head + ".", reply_markup=open_app_markup("Открыть все в Kyran"))
    for lot, m, _ in top:
        await reply(update, T.card(lot, m.score, m.reason), reply_markup=lot_buttons(lot))
    await reply(update, "🎁 У вас включён <b>PRO на 7 дней бесплатно</b>: все подходящие тендеры, уведомление через 5 минут "
                        "после публикации, ИИ-оценка и разборы. Дальше — бесплатный тариф (3 тендера в день) или PRO за 14 900 ₸/мес.\n\n"
                        "Можете писать мне обычным текстом: <i>найди ремонт спортзалов в Актобе</i>.")
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
    user = await load_user(update)
    token = make_link_token(user.id, version=user.token_version or 0)
    await reply(update, "Ссылка для компьютера (личная, не пересылайте, действует 30 дней):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Kyran в браузере", url=f"{config.PUBLIC_URL}/app?t={token}")]]))


async def on_trial_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    async with db.Session() as s:
        user = await core.get_or_create_user(s, q.from_user.id)
        try:
            until = billing.start_trial(user)
        except billing.LimitError as e:
            await q.answer(str(e), show_alert=True)
            return
        await s.commit()
        await core.rematch_user(s, user)
        stats = await core.user_stats(s, user)
    await q.answer("PRO включён на 7 дней")
    await q.edit_message_reply_markup(None)
    await reply(update, f"🎁 <b>PRO включён до {aware(until).astimezone(config.TZ).strftime('%d.%m.%Y')}.</b>\n"
                        f"Под вас открыто {stats['open']} тендеров на {T.money(stats['total_amount'], short=True)} — "
                        "все в ленте, новые буду присылать через 5 минут после публикации. /find — посмотреть лучшие.",
                reply_markup=open_app_markup("Открыть все тендеры"))


async def show_feed(update: Update, only_urgent: bool) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        if not user.onboarded:
            await reply(update, "Сначала расскажите, что вы поставляете: /start")
            return
        plan = billing.current_plan(user)[0]
        rows = await core.feed(s, user, only_urgent=only_urgent, limit=3 if plan.code == "free" else 5)
        stats = await core.user_stats(s, user)
    if not has_data():
        await reply(update, T.NO_DATA)
        return
    if not rows:
        await reply(update, "Сейчас подходящих открытых тендеров нет. Как появятся — пришлю.")
        return
    for lot, m, _ in rows:
        await reply(update, T.card(lot, m.score, m.reason), reply_markup=lot_buttons(lot))
    total = stats["urgent"] if only_urgent else stats["open"]
    if plan.code == "free" and total > len(rows):
        await reply(update, billing.free_note(len(rows), total), reply_markup=pro_markup(user))


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
        f"Направления: {escape(', '.join(u.keywords or []) or '—')}\n"
        f"Суммы: {sums}\n"
        f"БИН: {u.company_bin or 'не указан'}\n"
        f"Уведомления: {'включены' if u.notify else 'на паузе'}\n\n"
        f"Открыто под вас: {stats['open']} на {T.money(stats['total_amount'], short=True)}"
    )
    await reply(update, text, reply_markup=open_app_markup("Изменить в приложении"))


async def toggle_notify(update: Update, on: bool) -> None:
    async with db.Session() as s:
        u = await core.get_or_create_user(s, update.effective_user.id)
        u.notify = on
        await s.commit()
    await reply(update, "Уведомления включены." if on else "Уведомления на паузе. /resume — включить.")


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
    z = next((x for x in SOURCES if x.name == "zakup"), None)
    if z:
        srcs += (f"\nzakup.gov.kz: последний успех {z.last_ok:%d.%m %H:%M} UTC" if z.last_ok else "\nzakup.gov.kz: успешных сборов ещё не было")
        if z.last_error:
            srcs += f", ошибка: {T.escape(z.last_error)}"
    await reply(update, f"{users} пользователей ({onboarded} настроили)\n{lots} лотов ({open_lots} открыто)\n{srcs}\nИИ: {'вкл' if ai.available() else 'выкл'}")


async def zakup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: /zakup <запрос> — диагностика открытого поиска zakup.gov.kz."""
    if update.effective_user.id not in config.ADMIN_IDS:
        return
    z = next((x for x in SOURCES if x.name == "zakup"), None)
    q = " ".join(context.args) or "ремонт"
    await update.effective_chat.send_action(ChatAction.TYPING)
    with_date = await z.diagnose(q, True)
    without = await z.diagnose(q, False)
    await update.effective_message.reply_text(f"Запрос «{q}»\n\nС фильтром по сроку:\n{with_date}\n\nБез фильтра:\n{without}"[:4000],
                                              link_preview_options=NO_PREVIEW)


async def logout_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        user.token_version = (user.token_version or 0) + 1
        await s.commit()
    await reply(update, "Все сессии на сайте завершены. Чтобы войти снова, получите код на странице входа.")


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        used = await billing.usage(s, user)
    plan, active, until = billing.current_plan(user)
    if plan.code == "free":
        trial = billing.trial_available(user)
        await reply(update, "Тариф: <b>Бесплатный</b> — Kyran показывает только часть тендеров:\n"
                            "• 3 лучших тендера в день, утром, а не сразу\n"
                            f"• до {plan.keywords} направлений, {plan.chat_per_day} вопросов ИИ-агенту в день\n"
                            f"• ИИ-разборы в этом месяце: {used['analyses']} из {plan.analyses}\n\n"
                            "<b>PRO — 14 900 ₸/мес:</b>\n"
                            "• все подходящие тендеры, без ограничения\n"
                            "• новый тендер приходит через 5 минут после публикации\n"
                            "• ИИ-оценка каждого, 10 разборов и 20 документов заявки в месяц, слежка за конкурентами"
                            + ("\n\n🎁 Попробуйте PRO 7 дней бесплатно — один раз на аккаунт." if trial else ""),
                    reply_markup=pro_markup(user))
        return
    until_s = aware(until).astimezone(config.TZ).strftime("%d.%m.%Y") if until else "—"
    await reply(update, f"Тариф: <b>{plan.name}</b>, действует до {until_s}\n"
                        f"ИИ-разборы в этом месяце: {used['analyses']} из {plan.analyses}\n"
                        f"Документы заявки от ИИ: {used['generations']} из {plan.generations}\n"
                        f"Документы в библиотеке: {used['documents']} из {plan.documents}\n\n"
                        "Сменить тариф можно на сайте: Профиль → Тариф.", reply_markup=open_app_markup("Открыть тарифы"))


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: /grant <user_id> <start|pro|team> <месяцев>"""
    if update.effective_user.id not in config.ADMIN_IDS:
        return
    try:
        uid, plan, months = int(context.args[0]), context.args[1], int(context.args[2]) if len(context.args) > 2 else 1
    except (IndexError, ValueError):
        await reply(update, "Формат: /grant &lt;user_id&gt; &lt;start|pro|team&gt; &lt;месяцев&gt;")
        return
    async with db.Session() as s:
        user = await s.get(User, uid)
        if not user:
            await reply(update, "Пользователь не найден.")
            return
        try:
            billing.grant(user, plan, months)
        except ValueError as e:
            await reply(update, str(e))
            return
        reqs = (await s.execute(select(UpgradeRequest).where(UpgradeRequest.user_id == uid, UpgradeRequest.status == "new"))).scalars().all()
        for r in reqs:
            r.status = "done"
        await s.commit()
        until = aware(user.plan_until).astimezone(config.TZ).strftime("%d.%m.%Y")
    await reply(update, f"Готово: {uid} → {billing.PLANS[plan].name} до {until}.")
    try:
        await context.bot.send_message(uid, f"Тариф «{billing.PLANS[plan].name}» подключён до {until}. Спасибо, что выбрали Kyran.")
    except Exception:
        pass
    async with db.Session() as s:
        client = await s.get(User, uid)
        earning = await growth.on_payment(s, client, plan, months) if client else None
    if earning:
        await reply(update, f"Партнёру {earning.partner_id} начислено {earning.commission:,} ₸.".replace(",", " "))
        try:
            await context.bot.send_message(
                earning.partner_id, f"💸 Ваш клиент оплатил Kyran. Вам начислено <b>{earning.commission:,} ₸</b>. "
                "Баланс и выплаты — /partner".replace(",", " "), parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ── Партнёрская программа и каналы ─────────────────────────

async def partner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id, update.effective_user.full_name)
        r = await growth.partner_summary(s, user)
    link = f"https://t.me/{runtime.bot_username}?start=ref_{user.id}"
    await reply(update,
        "<b>Партнёрская программа Kyran</b>\n"
        f"Приводите компании — получайте <b>{int(growth.PARTNER_SHARE * 100)}%</b> с каждой их оплаты в течение 12 месяцев. "
        "Подходит бухгалтерам, тендерным консультантам и всем, у кого есть знакомые поставщики.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Пришло по ссылке: {r['invited']}\nОплатили: {r['paying']}\n"
        f"Заработано: {r['earned']:,} ₸, к выплате: {r['to_pay']:,} ₸\n\n".replace(",", " ")
        + "Клиент по вашей ссылке получает неделю PRO бесплатно. Выплата — на Kaspi или на счёт, раз в месяц.")


def _is_admin(update: Update) -> bool:
    return update.effective_user.id in config.ADMIN_IDS


async def channel_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: /channel_add @chat Название | ремонт, строительство | Актюбинская | 1000000"""
    if not _is_admin(update):
        return
    raw = (update.message.text or "").split(maxsplit=1)
    try:
        data = growth.parse_channel_args(raw[1] if len(raw) > 1 else "")
    except ValueError as e:
        await reply(update, f"{T.escape(str(e))}\nФормат: /channel_add @chat Название | ремонт, кровля | Актюбинская | 1000000")
        return
    try:
        chat = await context.bot.get_chat(data["chat"])
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if me.status not in ("administrator", "creator"):
            await reply(update, "Сначала добавьте бота администратором канала с правом публикации.")
            return
    except Exception as e:
        await reply(update, f"Не вижу канал {T.escape(data['chat'])}: {T.escape(str(e))}. Добавьте бота админом.")
        return
    async with db.Session() as s:
        ch = (await s.execute(select(Channel).where(Channel.chat == data["chat"]))).scalar_one_or_none() or Channel(chat=data["chat"])
        ch.title, ch.keywords, ch.region, ch.min_amount, ch.active = data["title"], data["keywords"], data["region"], data["min_amount"], True
        s.add(ch)
        await s.commit()
        lots, total, amount = await growth.pick_lots(s, ch)
    await reply(update, f"Канал #{ch.id} {T.escape(ch.chat)} сохранён: {T.escape(', '.join(ch.keywords))}"
                        f"{' · ' + T.escape(ch.region) if ch.region else ''}. Сейчас подходит {total} тендеров. "
                        f"Опубликовать сразу: /channel_post {ch.id}")


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    async with db.Session() as s:
        rows = await growth.channel_stats(s)
    if not rows:
        await reply(update, "Каналов нет. Добавить: /channel_add @chat Название | ремонт, кровля | Актюбинская")
        return
    await reply(update, "\n".join(
        f"#{c.id} {T.escape(c.chat)} {'✅' if c.active else '⏸'} — {T.escape(', '.join(c.keywords or []))}"
        f"{' · ' + T.escape(c.region) if c.region else ''}\n   пришло в бота: {n}, платят: {p}" for c, n, p in rows))


async def channel_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    if not context.args:
        await reply(update, "Формат: /channel_post <номер канала>")
        return
    async with db.Session() as s:
        ch = await s.get(Channel, int(context.args[0])) if context.args[0].isdigit() else \
            (await s.execute(select(Channel).where(Channel.chat == context.args[0]))).scalar_one_or_none()
        if not ch:
            await reply(update, "Канал не найден: /channels")
            return
        n = await post_channel(context, s, ch)
    await reply(update, f"Опубликовано в {T.escape(ch.chat)}: {n} тендеров." if n else "Новых подходящих тендеров для канала нет.")


async def channel_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update) or not context.args or not context.args[0].isdigit():
        return
    async with db.Session() as s:
        ch = await s.get(Channel, int(context.args[0]))
        if ch:
            ch.active = not ch.active
            await s.commit()
            await reply(update, f"Канал {T.escape(ch.chat)}: {'включён' if ch.active else 'на паузе'}.")


async def partners_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: кому сколько выплатить. /paid <partner_id> — отметить выплату."""
    if not _is_admin(update):
        return
    async with db.Session() as s:
        rows = await growth.partners_debt(s)
    await reply(update, "\n".join(f"{u.id} {T.escape(u.name or '')} @{T.escape(u.username or '')}: {d:,} ₸".replace(",", " ")
                                  for u, d in rows) or "Долгов партнёрам нет.")


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update) or not context.args or not context.args[0].isdigit():
        return
    from sqlalchemy import update as sa_update

    async with db.Session() as s:
        await s.execute(sa_update(PartnerEarning).where(PartnerEarning.partner_id == int(context.args[0]),
                                                        PartnerEarning.paid_out.is_(False)).values(paid_out=True))
        await s.commit()
    await reply(update, "Выплата отмечена.")


async def post_channel(context: ContextTypes.DEFAULT_TYPE, s, ch: Channel) -> int:
    lots, total, amount = await growth.pick_lots(s, ch)
    if not lots:
        return 0
    text = growth.channel_post_text(ch, lots, total, amount, runtime.bot_username)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Получать такие тендеры под свою компанию",
                                                         url=f"https://t.me/{runtime.bot_username}?start=ch_{ch.id}")]])
    await context.bot.send_message(ch.chat, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
    await growth.mark_posted(s, ch, lots)
    return len(lots)


async def channels_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        for ch in (await s.execute(select(Channel).where(Channel.active.is_(True)))).scalars().all():
            try:
                n = await post_channel(context, s, ch)
                log.info("Канал %s: опубликовано %d", ch.chat, n)
            except Exception:
                log.exception("Публикация в канал %s не удалась", ch.chat)


# ── Мониторинг сайтов (только в боте) ──────────────────────

async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "Пришлите адрес страницы с закупками заказчика, например:\n/watch https://example.kz/zakupki\n\n"
                            "Проверяю каждые 3 часа и пишу, когда появляется что-то новое.")
        return
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        try:
            m = await monitors.add_monitor(s, user, context.args[0])
        except Exception as e:
            await reply(update, f"Не получилось добавить: {e}")
            return
    await reply(update, f"Слежу за сайтом «{m.title}». Список: /sites")


async def sites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        items = (await s.execute(select(Monitor).where(Monitor.user_id == update.effective_user.id, Monitor.active.is_(True))
                                 .order_by(Monitor.id))).scalars().all()
    if not items:
        await reply(update, "Вы пока не следите ни за одним сайтом. Добавить: /watch &lt;адрес&gt;")
        return
    lines = []
    for m in items:
        checked = aware(m.last_checked).astimezone(config.TZ).strftime("%d.%m %H:%M") if m.last_checked else "ещё не проверялся"
        err = f" — ошибка: {m.last_error[:60]}" if m.last_error else ""
        lines.append(f"<b>{m.id}</b>. {m.title[:70]}\n{m.url[:90]}\nпроверен: {checked}{err}")
    await reply(update, "Сайты под наблюдением:\n\n" + "\n\n".join(lines) + "\n\nУбрать: /unwatch &lt;номер&gt;")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        mid = int(context.args[0])
    except (IndexError, ValueError):
        await reply(update, "Укажите номер из списка /sites, например: /unwatch 3")
        return
    async with db.Session() as s:
        m = await s.get(Monitor, mid)
        if not m or m.user_id != update.effective_user.id:
            await reply(update, "Такого сайта в вашем списке нет.")
            return
        await s.delete(m)
        await s.commit()
    await reply(update, "Больше не слежу за этим сайтом.")


# ── ТЗ, присланное файлом или фото ─────────────────────────

UPLOAD_WAIT = 2.5  # секунды: собрать все файлы альбома в один разбор


def upload_buttons(lot: Lot) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Техпредложение в Word", callback_data=f"gen:tech:{lot.id}"),
             InlineKeyboardButton("Запрос разъяснений", callback_data=f"gen:clarify:{lot.id}")],
            [InlineKeyboardButton("Опись документов", callback_data=f"gen:inventory:{lot.id}")]]
    if config.PUBLIC_URL:
        rows[1].append(InlineKeyboardButton("Открыть полностью", web_app=WebAppInfo(app_url(f"#lot/{lot.id}"))))
    return InlineKeyboardMarkup(rows)


async def _file_of(update: Update) -> tuple[str, object, str] | None:
    """(имя, telegram-объект для скачивания, mime) или None."""
    m = update.effective_message
    if m.document:
        return m.document.file_name or "document", m.document, m.document.mime_type or ""
    if m.photo:
        return f"photo_{m.message_id}.jpg", m.photo[-1], "image/jpeg"
    return None


async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    f = await _file_of(update)
    if not f:
        return
    name, obj, mime = f
    size = getattr(obj, "file_size", 0) or 0
    if size > uploads.MAX_FILE:
        await reply(update, f"Файл «{T.escape(name)}» больше 20 МБ — Telegram не даёт боту скачать его. Пришлите его частями.")
        return
    group = update.effective_message.media_group_id
    key = f"upload:{update.effective_chat.id}:{group or update.effective_message.message_id}"
    batch = context.chat_data.setdefault(key, {"files": [], "scheduled": False})
    batch["files"].append((name, obj, mime))
    if not batch["scheduled"]:
        batch["scheduled"] = True
        context.job_queue.run_once(process_upload, UPLOAD_WAIT if group else 0.1,
                                   data={"key": key, "chat_id": update.effective_chat.id,
                                         "user": (update.effective_user.id, update.effective_user.full_name, update.effective_user.username or "")},
                                   chat_id=update.effective_chat.id, user_id=update.effective_user.id)


async def process_upload(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    batch = context.chat_data.pop(job.data["key"], None)
    if not batch:
        return
    chat_id = job.data["chat_id"]
    uid, full_name, username = job.data["user"]
    n = len(batch["files"])
    status = await context.bot.send_message(
        chat_id, f"Получил {n} {'файл' if n == 1 else 'файла' if n < 5 else 'файлов'}. Читаю документацию и готовлю разбор — до минуты…")
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        files = []
        for name, obj, mime in batch["files"]:
            tf = await obj.get_file()
            files.append((name, bytes(await tf.download_as_bytearray()), mime))
        async with db.Session() as s:
            user = await core.get_or_create_user(s, uid, full_name, username)
            lot, data = await uploads.process(s, user, files)
    except uploads.UploadError as e:
        await status.edit_text(str(e))
        return
    except Exception:
        log.exception("Разбор присланного ТЗ не удался")
        await status.edit_text("Не получилось разобрать документ. Попробуйте ещё раз через минуту.")
        return
    try:
        await status.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id, T.upload_report(lot, data), parse_mode=ParseMode.HTML,
                                   link_preview_options=NO_PREVIEW, reply_markup=upload_buttons(lot))


async def on_docs_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Документы заявки» под карточкой тендера: выбор документа прямо в Telegram."""
    q = update.callback_query
    lot_id = int(q.data.split(":", 1)[1])
    await q.answer()
    rows = [[InlineKeyboardButton(k.title, callback_data=f"gen:{code}:{lot_id}")] for code, k in docgen.KINDS.items()]
    await context.bot.send_message(
        q.message.chat.id,
        "Какой документ подготовить по этой закупке? Пришлю файл Word сюда же.\n"
        "<i>Совет: перешлите сюда файл ТЗ — тогда техпредложение будет точнее.</i>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def on_generate_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «Техпредложение в Word» и др. под разбором."""
    q = update.callback_query
    _, kind, lot_id = q.data.split(":", 2)
    title = docgen.KINDS[kind].title if kind in docgen.KINDS else "Документ"
    await q.answer(f"Готовлю: {title}")
    wait = await context.bot.send_message(q.message.chat.id, f"Готовлю «{title}»…")
    async with db.Session() as s:
        user = await core.get_or_create_user(s, q.from_user.id)
        lot = await s.get(Lot, int(lot_id))
        if not lot or (lot.source == "upload" and (lot.raw or {}).get("owner") != user.id):
            await wait.edit_text("Закупка не найдена.")
            return
        try:
            g = await docgen.generate(s, user, lot, kind)
        except (billing.LimitError, docgen.GenerationError) as e:
            await wait.edit_text(str(e))
            return
        except Exception:
            log.exception("Документ из бота не сформирован")
            await wait.edit_text("Не получилось сформировать документ. Попробуйте ещё раз через минуту.")
            return
        missing = docgen.missing_requisites(user)
    import io as _io

    from telegram import InputFile

    caption = f"{g.title} — проверьте пункты «[уточнить]» и подпишите."
    if missing:
        caption += f"\nНе заполнены реквизиты: {', '.join(missing)}. Их можно указать на сайте: Профиль → Компания."
    await context.bot.send_document(q.message.chat.id, InputFile(_io.BytesIO(g.content), filename=g.filename), caption=caption[:1000])
    try:
        await wait.delete()
    except Exception:
        pass


# ── Кнопки под карточками ──────────────────────────────────

async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action, lot_id = q.data.split(":", 1)
    async with db.Session() as s:
        user = await core.get_or_create_user(s, q.from_user.id)
        if action == "save":
            await core.set_stage(s, user, int(lot_id), "saved")
            await q.answer("Добавлено в воронку «Интересно»")
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

    user_now = await load_user(update)
    plan_now = billing.current_plan(user_now)[0]
    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    used = context.user_data.get("chat_day") == today and context.user_data.get("chat_used", 0) or 0
    over_limit = used >= plan_now.chat_per_day
    if over_limit and not context.user_data.get("chat_limit_told") == today:
        context.user_data["chat_limit_told"] = today
        await reply(update, f"На бесплатном тарифе — {plan_now.chat_per_day} вопросов ИИ-агенту в день, на сегодня они закончились. "
                            "Дальше ищу простым поиском по словам, завтра лимит обновится.\n"
                            "ИИ-агент без ограничений — в PRO, 14 900 ₸/мес.", reply_markup=pro_markup(user_now))
    if ai.available() and not over_limit:
        context.user_data["chat_day"], context.user_data["chat_used"] = today, used + 1
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
        lots = [l for l in lots if l]
        if plan_now.code == "free":
            found = len(dict.fromkeys(run.shown_lots)) if run else len(lots)
            for lot in lots[:3]:
                await reply(update, T.card(lot), reply_markup=lot_buttons(lot))
            if found > 3:
                await reply(update, f"Бесплатный тариф: показываю 3 карточки из {found}. Все карточки, ИИ-оценку и разборы — "
                                    "активируйте PRO.", reply_markup=pro_markup(user_now))
            return
        for lot in lots:
            await reply(update, T.card(lot), reply_markup=lot_buttons(lot))
        return

    # Без ИИ: ссылка → мониторинг сайта, иначе поиск по словам
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id, update.effective_user.full_name)
        if re.match(r"^(https?://|www\.)\S+$", text):
            try:
                m = await monitors.add_monitor(s, user, text)
                await reply(update, f"Слежу за сайтом «{m.title}». Сообщу, когда там что-то изменится.")
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
    await reply(update, f"Нашёл по запросу «{text[:60]}»:")
    for lot, score, reason in hits:
        await reply(update, T.card(lot, score, reason), reply_markup=lot_buttons(lot))


# ── Фоновые задачи ─────────────────────────────────────────

async def send_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lot: Lot, m: Match, fresh: bool = False) -> bool:
    try:
        text = T.card(lot, m.score, m.reason)
        if fresh:
            text = T.fresh_head(lot) + text
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       link_preview_options=NO_PREVIEW, reply_markup=lot_buttons(lot))
        return True
    except Exception:
        log.exception("Не отправилось пользователю %s", chat_id)
        return False


async def _notify_new(context: ContextTypes.DEFAULT_TYPE, s, new: list[Lot], label: str) -> None:
    """Каждый новый подходящий тендер — сразу в Telegram, пока конкуренты не успели."""
    matches = await core.match_new_lots(s, new)
    log.info("%s: новых лотов %d, подборок для %d пользователей", label, len(new), len(matches))
    for user_id, ms in matches.items():
        user = await s.get(User, user_id)
        if not user or not user.notify or not billing.current_plan(user)[0].instant:
            continue  # бесплатный тариф: эти тендеры придут в утренней подборке
        sent = 0
        for m in sorted(ms, key=lambda x: -x.score):
            if sent >= config.INSTANT_PER_RUN:
                break  # остальные придут в утренней подборке
            lot = await s.get(Lot, m.lot_id)
            deadline = db.aware(lot.deadline)
            if deadline and deadline <= utcnow():
                continue
            if await send_card(context, user_id, lot, m, fresh=True):
                m.notified, sent = True, sent + 1
    await s.commit()


async def fresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в несколько минут: первые страницы по направлениям -> мгновенные уведомления."""
    async with db.Session() as s:
        new = await core.collect_fresh(s, SOURCES)
        if new:
            await _notify_new(context, s, new, "Быстрый сбор")


async def collect_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Полный сбор раз в час: всё, что не попало в быстрый проход."""
    async with db.Session() as s:
        first_full = not await core.index_is_full(s)
        new = await core.collect(s, SOURCES)
        if new and first_full:
            # Первый полный обход принёс десятки тысяч лотов, опубликованных давно:
            # подбираем их молча (лента и утренняя подборка), без «новых» уведомлений.
            matches = await core.match_new_lots(s, new)
            log.info("Первый полный обход: %d лотов, подборки для %d пользователей (без уведомлений)", len(new), len(matches))
        elif new:
            await _notify_new(context, s, new, "Полный сбор")


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утренняя подборка: лучшие ещё не отправленные тендеры."""
    async with db.Session() as s:
        users = (await s.execute(select(User).where(User.onboarded.is_(True), User.notify.is_(True)))).scalars().all()
        for user in users:
            if not billing.current_plan(user)[1]:
                continue
            if not (user.keywords or []):
                try:
                    await context.bot.send_message(
                        user.id,
                        "⚠️ У вас не указаны направления, поэтому Kyran не может присылать тендеры.\n"
                        "Напишите, что поставляет компания, например: <i>мебель, канцтовары, ремонт кровли</i> — "
                        "или откройте кабинет → «Подбор закупок».",
                        parse_mode=ParseMode.HTML, reply_markup=open_app_markup("Указать направления"))
                except Exception:
                    log.exception("Напоминание о направлениях не отправилось %s", user.id)
                continue
            plan = billing.current_plan(user)[0]
            size = min(config.DIGEST_SIZE, plan.digest_size)
            rows = (await s.execute(
                select(Lot, Match).join(Match, and_(Match.lot_id == Lot.id, Match.user_id == user.id))
                .where(Match.notified.is_(False), Match.hidden.is_(False), core.open_condition())
                .order_by(Match.score.desc()).limit(size)
            )).all()
            if not rows:
                continue
            text = T.digest([(l, m.score) for l, m in rows], "<b>Тендеры на сегодня</b>")
            if plan.code == "free":
                waiting = (await s.execute(
                    select(func.count(Match.id)).join(Lot, Lot.id == Match.lot_id)
                    .where(Match.user_id == user.id, Match.notified.is_(False), Match.hidden.is_(False), core.open_condition())
                )).scalar() or 0
                more = max(0, waiting - len(rows))
                if more:
                    text += "\n\n" + billing.free_note(len(rows), len(rows) + more).replace(
                        "показываю", "показываю сегодня") + " Пока конкуренты не успели."
            try:
                await context.bot.send_message(user.id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                               reply_markup=pro_markup(user) if plan.code == "free" else open_app_markup("Смотреть и разобрать"))
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
            text = f"На сайте «{m.title}» новые закупки:\n{lines}"
        else:
            text = f"На сайте «{m.title}» что-то изменилось."
        try:
            await context.bot.send_message(
                m.user_id, text, link_preview_options=NO_PREVIEW,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть сайт", url=m.url)]]),
            )
        except Exception:
            log.exception("Уведомление о сайте не отправилось")


# ── Конкуренты ─────────────────────────────────────────────

async def follow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        async with db.Session() as s:
            user = await core.get_or_create_user(s, update.effective_user.id)
            sug = await rivals.suggest(s, user, limit=5)
        text = ("Пришлите БИН конкурента: /follow 123456789012\n\n"
                "Буду сообщать о каждой его победе: что выиграл, у кого и по какой цене.")
        if sug:
            text += "\n\n<b>Чаще всего в ваших направлениях выигрывают:</b>\n" + "\n".join(
                f"• {T.escape(x['name'][:70])} — {x['wins']} шт., {T.money(x['total'], short=True)}\n  /follow {x['bin']}" for x in sug)
        await reply(update, text)
        return
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        try:
            c = await rivals.follow(s, user, context.args[0], " ".join(context.args[1:]))
        except rivals.CompetitorError as e:
            await reply(update, T.escape(str(e)))
            return
        d = await rivals.dossier(s, user, c.bin)
    name = T.escape(c.name or f"БИН {c.bin}")
    await reply(update, f"Слежу за <b>{name}</b>.\nЗа год: {d['wins']} побед на {T.money(d['total'], short=True)}"
                        f"{', снижение в среднем ' + str(d['median_discount']) + '%' if d['median_discount'] is not None else ''}.\n"
                        "О новых победах напишу сразу. Список: /rivals",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Досье", web_app=WebAppInfo(app_url(f"#rival/{c.bin}")))]])
                if config.PUBLIC_URL else None)


async def rivals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        items = await rivals.list_for(s, user)
    if not items:
        await reply(update, "Вы пока ни за кем не следите. Добавить: /follow &lt;БИН&gt;")
        return
    lines = [f"<b>{T.escape(x['name'][:80])}</b> — {x['wins']} побед за год, {T.money(x['total'], short=True)}\n"
             f"БИН {x['bin']} · убрать: /unfollow {x['bin']}" for x in items]
    await reply(update, "<b>Конкуренты под наблюдением</b>\n\n" + "\n\n".join(lines))


async def unfollow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "Укажите БИН: /unfollow 123456789012")
        return
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        ok = await rivals.unfollow(s, user, context.args[0])
    await reply(update, "Больше не слежу за этой компанией." if ok else "Такого конкурента в вашем списке нет.")


async def rivals_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Новые победы отслеживаемых конкурентов → сообщение владельцу (только активный тариф)."""
    async with db.Session() as s:
        found = await rivals.new_wins(s)
        active = {}
        for uid, _, _ in found:
            if uid not in active:
                u = await s.get(User, uid)
                active[uid] = bool(u and u.notify and billing.current_plan(u)[0].competitors > 0)
    for uid, c, wins in found:
        if not active.get(uid):
            continue
        items = "\n".join(
            f"• {T.escape(a.title[:110])}\n  {T.escape((a.customer or '')[:80])} — {T.money(a.price, short=True)}"
            f"{' (−' + str(round((1 - a.price / a.amount) * 100, 1)) + '%)' if a.amount and a.price else ''}"
            f"{', участников: ' + str(a.participants) if a.participants else ''}" for a in wins[:5])
        more = f"\nи ещё {len(wins) - 5}" if len(wins) > 5 else ""
        text = f"Конкурент <b>{T.escape(c.name or c.bin)}</b> выиграл {len(wins)} {'закупку' if len(wins) == 1 else 'закупки' if len(wins) < 5 else 'закупок'}:\n{items}{more}"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Досье", web_app=WebAppInfo(app_url(f"#rival/{c.bin}")))]]) if config.PUBLIC_URL else None
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
        except Exception:
            log.exception("Уведомление о конкуренте не отправилось %s", uid)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db.Session() as s:
        user = await core.get_or_create_user(s, update.effective_user.id)
        if not user.onboarded:
            await reply(update, "Сначала настройте подбор: /start")
            return
        r = await report.weekly(s, user)
    await reply(update, T.weekly_report(r), reply_markup=open_app_markup("Подробный отчёт") and InlineKeyboardMarkup(
        [[InlineKeyboardButton("Подробный отчёт", web_app=WebAppInfo(app_url("#report")))]]))


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Понедельник утром: что прошло мимо за неделю. Только активные тарифы и включённые уведомления."""
    async with db.Session() as s:
        users = (await s.execute(select(User).where(User.onboarded.is_(True), User.notify.is_(True)))).scalars().all()
        payloads = []
        for user in users:
            if not billing.current_plan(user)[1]:
                continue
            r = await report.weekly(s, user)
            if r["missed"]["count"] or r["soon"]:
                payloads.append((user.id, T.weekly_report(r)))
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Подробный отчёт", web_app=WebAppInfo(app_url("#report")))]]) if config.PUBLIC_URL else None
    for uid, text in payloads:
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
        except Exception:
            log.exception("Отчёт недели не отправился %s", uid)


async def reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Напоминания: сроки подачи по закупкам в воронке и истекающие документы."""
    now = utcnow()
    msgs: list[tuple[int, str, object]] = []
    async with db.Session() as s:
        rows = (await s.execute(
            select(PipelineItem, Lot).join(Lot, Lot.id == PipelineItem.lot_id)
            .where(PipelineItem.stage.in_(("saved", "preparing")), Lot.deadline.is_not(None), Lot.deadline > now)
        )).all()
        for p, lot in rows:
            left = aware(lot.deadline) - now
            if left <= timedelta(days=1) and not p.reminded_1d:
                p.reminded_1d = p.reminded_3d = True
                msgs.append((p.user_id, f"Меньше суток до окончания приёма заявок:\n{T.card(lot)}", lot_buttons(lot)))
            elif left <= timedelta(days=3) and not p.reminded_3d:
                p.reminded_3d = True
                msgs.append((p.user_id, f"До окончания приёма заявок осталось меньше 3 дней:\n{T.card(lot)}", lot_buttons(lot)))
        # Тариф: за 2 дня до окончания и в день окончания
        for user in (await s.execute(select(User).where(User.onboarded.is_(True)))).scalars().all():
            plan, active, until = billing.raw_plan(user, now)
            if until is None or plan.code == "free":
                continue
            stamp = aware(until).astimezone(config.TZ).strftime("%d.%m.%Y")
            what = "пробный период" if plan.code == "trial" else f"тариф «{plan.name}»"
            stats = await core.user_stats(s, user)
            if not active and user.plan_notice != f"expired:{stamp}":
                user.plan_notice = f"expired:{stamp}"
                msgs.append((user.id,
                    f"{what.capitalize()} закончился {stamp}. Kyran продолжит работать на бесплатном тарифе:\n"
                    "• 3 лучших тендера утром вместо всех сразу\n• без ИИ-оценки, 5 вопросов агенту в день\n"
                    "• 1 ИИ-разбор и 1 документ заявки в месяц\n\n"
                    f"Сейчас под вас открыто {stats['open']} тендеров на {T.money(stats['total_amount'], short=True)}. "
                    "Чтобы получать все и сразу — активируйте PRO, 14 900 ₸/мес.", pro_markup(user)))
            elif active and until - now <= timedelta(days=2) and not user.plan_notice.startswith(("soon:" + stamp, "expired:" + stamp)):
                user.plan_notice = f"soon:{stamp}"
                msgs.append((user.id,
                    f"{what.capitalize()} заканчивается {stamp}. За это время Kyran нашёл под вас {stats['open']} открытых тендеров "
                    f"на {T.money(stats['total_amount'], short=True)}. После окончания останется бесплатный тариф: "
                    "3 тендера в день утром, без мгновенных уведомлений и ИИ-оценки. Оставить PRO — 14 900 ₸/мес.",
                    pro_markup(user)))
        docs = (await s.execute(select(Document).where(Document.expires_at.is_not(None)))).scalars().all()
        for d in docs:
            left = aware(d.expires_at) - now
            when = aware(d.expires_at).astimezone(config.TZ).strftime("%d.%m.%Y")
            if left <= timedelta(days=7) and not d.reminded_7d:
                d.reminded_7d = d.reminded_30d = True
                verb = "истёк" if left.total_seconds() < 0 else "истекает"
                msgs.append((d.user_id, f"Документ «{d.title}» {verb} {when}. Обновите его, чтобы не получить отклонение заявки.",
                             open_app_markup("Открыть документы")))
            elif left <= timedelta(days=30) and not d.reminded_30d:
                d.reminded_30d = True
                msgs.append((d.user_id, f"Через месяц, {when}, истекает документ «{d.title}». Закажите новый заранее.",
                             open_app_markup("Открыть документы")))
        await s.commit()
    for chat_id, text, markup in msgs:
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
        except Exception:
            log.exception("Напоминание не отправилось")


# ── Сборка приложения ──────────────────────────────────────

async def post_init(application: Application) -> None:
    runtime.bot = application.bot
    try:
        runtime.bot_username = (await application.bot.get_me()).username or ""
        log.info("Бот: @%s", runtime.bot_username)
    except Exception:
        log.exception("get_me не удался")
    if config.PUBLIC_URL:
        try:
            await application.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Kyran", WebAppInfo(app_url())))
        except Exception:
            log.exception("Не удалось поставить кнопку меню")
    await application.bot.set_my_commands([
        ("app", "Открыть Kyran"), ("find", "Лучшие тендеры сейчас"), ("urgent", "Срочные"),
        ("profile", "Мой профиль"), ("sites", "Сайты под наблюдением"), ("watch", "Следить за сайтом"),
        ("report", "Итоги недели"), ("rivals", "Конкуренты"), ("follow", "Следить за конкурентом"), ("plan", "Мой тариф"), ("partner", "Партнёрам: 25% с оплат"), ("web", "Ссылка для компьютера"), ("start", "Настроить заново"), ("help", "Помощь"),
    ])


def build() -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_keywords)],
            AMOUNT: [CallbackQueryHandler(got_amount, pattern=r"^amt:")],
            BIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_bin), CallbackQueryHandler(skip_bin, pattern=r"^bin:skip$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    # Вход на сайт — раньше всех остальных обработчиков (группа -1), чтобы код не попал в онбординг
    application.add_handler(CommandHandler("start", login_link_handler, filters=filters.Regex(r"^/start\s+login_")), group=-1)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(LOGIN_CODE), login_code_handler), group=-1)
    application.add_handler(onboarding)
    for name, fn in [("app", app_cmd), ("web", web_cmd), ("find", find_cmd), ("urgent", urgent_cmd), ("profile", profile_cmd),
                     ("stop", stop_cmd), ("resume", resume_cmd), ("help", help_cmd), ("stats", stats_cmd),
                     ("logout_all", logout_all_cmd), ("plan", plan_cmd), ("grant", grant_cmd),
                     ("watch", watch_cmd), ("sites", sites_cmd), ("unwatch", unwatch_cmd), ("report", report_cmd),
                     ("follow", follow_cmd), ("rivals", rivals_cmd), ("unfollow", unfollow_cmd),
                     ("zakup", zakup_cmd), ("partner", partner_cmd), ("channel_add", channel_add_cmd),
                     ("channels", channels_cmd), ("channel_post", channel_post_cmd), ("channel_off", channel_off_cmd),
                     ("partners", partners_cmd), ("paid", paid_cmd)]:
        application.add_handler(CommandHandler(name, fn))
    application.add_handler(CallbackQueryHandler(on_card_button, pattern=r"^(save|hide):\d+$"))
    application.add_handler(CallbackQueryHandler(on_trial_button, pattern=r"^trial:start$"))
    application.add_handler(CallbackQueryHandler(on_docs_button, pattern=r"^docs:\d+$"))
    application.add_handler(CallbackQueryHandler(on_generate_button, pattern=r"^gen:(tech|clarify|cover|inventory|experience):\d+$"))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_upload), group=-1)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    jq = application.job_queue
    jq.run_repeating(collect_job, interval=config.COLLECT_EVERY_MIN * 60, first=5)
    jq.run_repeating(fresh_job, interval=config.FAST_EVERY_MIN * 60, first=90)
    jq.run_repeating(monitors_job, interval=config.WEBSITE_EVERY_MIN * 60, first=120)
    jq.run_daily(digest_job, time=dtime(hour=config.DIGEST_HOUR, tzinfo=config.TZ))
    jq.run_repeating(reminders_job, interval=3600, first=60)
    jq.run_repeating(rivals_job, interval=config.COLLECT_EVERY_MIN * 60, first=180)
    jq.run_daily(weekly_report_job, time=dtime(hour=10, tzinfo=config.TZ), days=(1,))  # понедельник
    for hour in (10, 17):  # каналы с тендерами: утром и после обеда
        jq.run_daily(channels_job, time=dtime(hour=hour, minute=5, tzinfo=config.TZ))
    return application
