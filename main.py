import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions, ChatMember
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

from database import Database

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DATABASE_PATH", "/app/data/bot.db")

db = Database(DB_PATH)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("moderator")

DUR_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def parse_duration(value: str):
    m = DUR_RE.fullmatch(value.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def human_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    parts = []
    for name, size in (("д", 86400), ("ч", 3600), ("м", 60), ("с", 1)):
        if seconds >= size:
            q, seconds = divmod(seconds, size)
            parts.append(f"{q}{name}")
    return " ".join(parts) if parts else "0с"


def user_label(user_id, username=None, first_name=None):
    if username:
        return f"@{username}"
    return first_name or str(user_id)


def is_bot_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_bot_admin(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Доступ только для администраторов бота.")
        return False
    return True


async def target_from_reply(update: Update):
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return None
    return msg.reply_to_message.from_user


async def is_protected_member(bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


async def log_action(application, chat_id, admin_id, action, target_id, reason="", duration=None):
    db.add_log(chat_id, admin_id, action, target_id, reason, duration)
    log_chat_id = db.get_setting(chat_id, "log_chat_id", "")
    if not log_chat_id:
        return
    try:
        await application.bot.send_message(
            int(log_chat_id),
            f"📝 <b>{action}</b>\n"
            f"Чат: <code>{chat_id}</code>\n"
            f"Цель: <code>{target_id}</code>\n"
            f"Админ: <code>{admin_id}</code>\n"
            f"Причина: {reason or '—'}\n"
            f"Срок: {duration or '—'}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("Cannot send log message")


async def schedule_expiry(application, punishment):
    seconds = max(1, punishment["expires_at"] - now_ts())
    application.job_queue.run_once(
        expire_punishment,
        seconds,
        data={"punishment_id": punishment["id"]},
        name=f"punishment:{punishment['id']}",
    )


async def expire_punishment(context: ContextTypes.DEFAULT_TYPE):
    pid = context.job.data["punishment_id"]
    p = db.get_punishment(pid)
    if not p or p["status"] != "active" or p["expires_at"] is None:
        return
    if p["expires_at"] > now_ts():
        await schedule_expiry(context.application, p)
        return
    try:
        await lift_punishment(context.application, p, automatic=True)
    except Exception:
        log.exception("Failed to expire punishment %s", pid)


async def lift_punishment(application, p, automatic=False):
    chat_id, user_id, kind = p["chat_id"], p["user_id"], p["type"]
    if kind == "mute":
        await application.bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
                can_pin_messages=True,
                can_manage_topics=True,
            ),
        )
    elif kind == "ban":
        await application.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    db.close_punishment(p["id"], "expired" if automatic else "removed")
    if not automatic:
        await log_action(application, chat_id, 0, "Снятие наказания", user_id, f"punishment #{p['id']}")
    else:
        await log_action(application, chat_id, 0, "Автоматическое окончание", user_id, f"punishment #{p['id']}")


def panel_keyboard(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Все наказания", callback_data="panel:punishments:0")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="panel:users:0")],
        [InlineKeyboardButton("📝 Журнал действий", callback_data="panel:logs:0")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="panel:settings")],
        [InlineKeyboardButton("👮 Администраторы", callback_data="panel:admins")],
    ])


async def start(update, context):
    if await require_admin(update):
        await update.effective_message.reply_text(
            "🛡 <b>Модератор запущен.</b>\n\n"
            "Открой /panel для управления.",
            parse_mode=ParseMode.HTML,
        )


async def panel(update, context):
    if not await require_admin(update):
        return
    await update.effective_message.reply_text(
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыберите раздел:",
        reply_markup=panel_keyboard(update.effective_chat.id),
        parse_mode=ParseMode.HTML,
    )


async def id_cmd(update, context):
    await update.effective_message.reply_text(
        f"Твой ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def mute_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение пользователя: /mute 10m причина")
        return
    if await is_protected_member(update.get_bot(), update.effective_chat.id, target.id):
        await update.effective_message.reply_text("❌ Нельзя мутить владельца или администратора.")
        return
    duration = context.args[0] if context.args else db.get_setting(update.effective_chat.id, "default_mute", "10m")
    seconds = parse_duration(duration)
    if seconds is None:
        await update.effective_message.reply_text("Формат: 30s, 10m, 2h, 3d, 1w")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Без причины"
    expires = now_ts() + seconds
    await update.get_bot().restrict_chat_member(
        update.effective_chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=datetime.fromtimestamp(expires, tz=timezone.utc),
    )
    p = db.add_punishment(update.effective_chat.id, target.id, target.username, target.first_name,
                          "mute", reason, expires, update.effective_user.id)
    await schedule_expiry(update.get_bot().application, p)
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id,
                     "Мут", target.id, reason, human_seconds(seconds))
    await update.effective_message.reply_text(
        f"🔇 {target.mention_html()} получил мут на <b>{human_seconds(seconds)}</b>.",
        parse_mode=ParseMode.HTML
    )


async def unmute_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /unmute")
        return
    await update.get_bot().restrict_chat_member(
        update.effective_chat.id, target.id,
        permissions=ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True,
            can_manage_topics=True,
        ),
    )
    for p in db.active_for_user(update.effective_chat.id, target.id, "mute"):
        db.close_punishment(p["id"], "removed")
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id,
                     "Размут", target.id)
    await update.effective_message.reply_text(f"🔊 Мут с {target.mention_html()} снят.", parse_mode=ParseMode.HTML)


async def warn_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /warn 7d причина")
        return
    if await is_protected_member(update.get_bot(), update.effective_chat.id, target.id):
        await update.effective_message.reply_text("❌ Нельзя выдать предупреждение администратору.")
        return
    duration = context.args[0] if context.args else db.get_setting(update.effective_chat.id, "default_warn", "7d")
    seconds = parse_duration(duration)
    if seconds is None:
        await update.effective_message.reply_text("Формат: 30s, 10m, 2h, 3d, 1w")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Без причины"
    p = db.add_punishment(update.effective_chat.id, target.id, target.username, target.first_name,
                          "warn", reason, now_ts() + seconds, update.effective_user.id)
    await schedule_expiry(update.get_bot().application, p)
    count = len(db.active_for_user(update.effective_chat.id, target.id, "warn"))
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id,
                     "Предупреждение", target.id, reason, human_seconds(seconds))
    await update.effective_message.reply_text(
        f"⚠️ {target.mention_html()} получил предупреждение.\n"
        f"Причина: <b>{reason}</b>\n"
        f"Активных: <b>{count}</b>",
        parse_mode=ParseMode.HTML
    )
    limit = int(db.get_setting(update.effective_chat.id, "max_warnings", "3"))
    if count >= limit:
        mute_duration = db.get_setting(update.effective_chat.id, "default_mute", "10m")
        fake = type("X", (), {})()
        # Автомут через ту же логику, без команды.
        mute_seconds = parse_duration(mute_duration)
        if mute_seconds:
            expires = now_ts() + mute_seconds
            await update.get_bot().restrict_chat_member(
                update.effective_chat.id, target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.fromtimestamp(expires, tz=timezone.utc),
            )
            mp = db.add_punishment(update.effective_chat.id, target.id, target.username, target.first_name,
                                   "mute", f"Автоматически: {limit} предупреждений", expires, update.effective_user.id)
            await schedule_expiry(update.get_bot().application, mp)
            await update.effective_message.reply_text(
                f"🔇 Лимит {limit} предупреждений достигнут. Автомут: {human_seconds(mute_seconds)}."
            )


async def unwarn_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /unwarn")
        return
    rows = db.active_for_user(update.effective_chat.id, target.id, "warn")
    if not rows:
        await update.effective_message.reply_text("Активных предупреждений нет.")
        return
    db.close_punishment(rows[-1]["id"], "removed")
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id,
                     "Снятие предупреждения", target.id, f"punishment #{rows[-1]['id']}")
    await update.effective_message.reply_text("✅ Последнее предупреждение снято.")


async def warnings_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /warnings")
        return
    rows = db.active_for_user(update.effective_chat.id, target.id, "warn")
    if not rows:
        await update.effective_message.reply_text("✅ Активных предупреждений нет.")
        return
    lines = [f"⚠️ <b>Предупреждения {target.mention_html()}</b>\n"]
    for p in rows:
        lines.append(f"#{p['id']} — {p['reason']} — осталось {human_seconds(p['expires_at']-now_ts())}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def ban_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /ban [срок] причина\nБез срока — навсегда.")
        return
    if await is_protected_member(update.get_bot(), update.effective_chat.id, target.id):
        await update.effective_message.reply_text("❌ Нельзя забанить владельца или администратора.")
        return
    args = context.args
    seconds = parse_duration(args[0]) if args else None
    reason = " ".join(args[1:]) if seconds else " ".join(args)
    expires = now_ts() + seconds if seconds else None
    await update.get_bot().ban_chat_member(
        update.effective_chat.id, target.id,
        until_date=datetime.fromtimestamp(expires, tz=timezone.utc) if expires else None,
    )
    p = db.add_punishment(update.effective_chat.id, target.id, target.username, target.first_name,
                          "ban", reason or "Без причины", expires, update.effective_user.id)
    if expires:
        await schedule_expiry(update.get_bot().application, p)
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id,
                     "Бан", target.id, reason or "Без причины", human_seconds(seconds) if seconds else "навсегда")
    await update.effective_message.reply_text(
        f"🚫 {target.mention_html()} заблокирован.", parse_mode=ParseMode.HTML
    )


async def unban_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /unban")
        return
    await update.get_bot().unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
    for p in db.active_for_user(update.effective_chat.id, target.id, "ban"):
        db.close_punishment(p["id"], "removed")
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id, "Разбан", target.id)
    await update.effective_message.reply_text("🔓 Пользователь разблокирован.")


async def kick_cmd(update, context):
    if not await require_admin(update): return
    target = await target_from_reply(update)
    if not target:
        await update.effective_message.reply_text("Ответь на сообщение: /kick причина")
        return
    if await is_protected_member(update.get_bot(), update.effective_chat.id, target.id):
        await update.effective_message.reply_text("❌ Нельзя кикнуть администратора.")
        return
    await update.get_bot().ban_chat_member(update.effective_chat.id, target.id)
    await update.get_bot().unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
    reason = " ".join(context.args) or "Без причины"
    await log_action(update.get_bot().application, update.effective_chat.id, update.effective_user.id, "Кик", target.id, reason)
    await update.effective_message.reply_text(f"👢 {target.mention_html()} исключён.", parse_mode=ParseMode.HTML)


def page_buttons(prefix, page, total, per_page=5):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{page-1}"))
    if (page + 1) * per_page < total:
        buttons.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{page+1}"))
    return [buttons] if buttons else []


async def show_punishments(query, chat_id, page=0):
    per = 5
    rows, total = db.punishments_page(chat_id, page, per)
    text = "📋 <b>АКТИВНЫЕ НАКАЗАНИЯ</b>\n\n"
    buttons = []
    if not rows:
        text += "Нет активных наказаний."
    for p in rows:
        left = "навсегда" if p["expires_at"] is None else human_seconds(p["expires_at"] - now_ts())
        icon = {"mute": "🔇", "warn": "⚠️", "ban": "🚫"}[p["type"]]
        name = user_label(p["user_id"], p["username"], p["first_name"])
        text += f"{icon} <b>#{p['id']}</b> {name}\n{p['reason']} | {left}\n\n"
        buttons.append([InlineKeyboardButton(
            f"Снять #{p['id']}", callback_data=f"lift:{p['id']}:{page}"
        )])
    buttons += page_buttons("panel:punishments", page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="panel:home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_logs(query, chat_id, page=0):
    per = 8
    rows, total = db.logs_page(chat_id, page, per)
    text = "📝 <b>ЖУРНАЛ ДЕЙСТВИЙ</b>\n\n"
    for r in rows:
        text += f"#{r['id']} — {r['action']} — target <code>{r['target_id']}</code>\n"
        text += f"{r['reason'] or '—'}\n"
    if not rows: text += "Журнал пуст."
    buttons = page_buttons("panel:logs", page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="panel:home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_users(query, chat_id, page=0):
    per = 8
    rows, total = db.users_page(chat_id, page, per)
    text = "👥 <b>ПОЛЬЗОВАТЕЛИ С НАКАЗАНИЯМИ</b>\n\n"
    for r in rows:
        text += f"• {user_label(r['user_id'], r['username'], r['first_name'])}: {r['cnt']}\n"
    if not rows: text += "Нет данных."
    buttons = page_buttons("panel:users", page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="panel:home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def callback(update, context):
    q = update.callback_query
    await q.answer()
    if not is_bot_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    chat_id = q.message.chat_id
    data = q.data

    if data == "panel:home":
        await q.edit_message_text("🛠 <b>АДМИН-ПАНЕЛЬ</b>", reply_markup=panel_keyboard(chat_id), parse_mode=ParseMode.HTML)
    elif data.startswith("panel:punishments:"):
        await show_punishments(q, chat_id, int(data.rsplit(":",1)[1]))
    elif data.startswith("panel:logs:"):
        await show_logs(q, chat_id, int(data.rsplit(":",1)[1]))
    elif data.startswith("panel:users:"):
        await show_users(q, chat_id, int(data.rsplit(":",1)[1]))
    elif data == "panel:settings":
        s = db.settings(chat_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔇 Мут: {s['default_mute']}", callback_data="set:mute")],
            [InlineKeyboardButton(f"⚠️ Варн: {s['default_warn']}", callback_data="set:warn")],
            [InlineKeyboardButton(f"🔢 Лимит: {s['max_warnings']}", callback_data="set:limit")],
            [InlineKeyboardButton(f"📢 Логи: {s['log_chat_id'] or 'не задан'}", callback_data="set:log")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="panel:home")],
        ])
        await q.edit_message_text("⚙️ <b>НАСТРОЙКИ ЧАТА</b>\n\nНажми настройку, затем отправь значение.", reply_markup=kb, parse_mode=ParseMode.HTML)
    elif data == "panel:admins":
        admins = ", ".join(map(str, sorted(ADMIN_IDS))) or "нет"
        await q.edit_message_text(f"👮 <b>Администраторы бота</b>\n\n<code>{admins}</code>\n\nИзменять ADMIN_IDS можно через переменные окружения Bothost.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="panel:home")]]),
                                  parse_mode=ParseMode.HTML)
    elif data.startswith("set:"):
        context.user_data["setting"] = data.split(":",1)[1]
        await q.edit_message_text("✏️ Отправь новое значение отдельным сообщением.\n\nДля лога: ID чата или пусто.")
    elif data.startswith("lift:"):
        _, pid, page = data.split(":")
        p = db.get_punishment(int(pid))
        if not p or p["status"] != "active":
            await q.answer("Наказание уже снято.", show_alert=True)
            await show_punishments(q, chat_id, int(page))
            return
        await lift_punishment(context.application, p, automatic=False)
        await q.answer("Наказание снято.")
        await show_punishments(q, chat_id, int(page))


async def setting_message(update, context):
    if not is_bot_admin(update.effective_user.id):
        return
    setting = context.user_data.get("setting")
    if not setting or update.effective_chat.type not in ("group", "supergroup"):
        return
    value = update.effective_message.text.strip()
    chat_id = update.effective_chat.id
    if setting in ("mute", "warn"):
        if parse_duration(value) is None:
            await update.effective_message.reply_text("❌ Неверный формат. Например: 10m, 2h, 7d.")
            return
        db.set_setting(chat_id, f"default_{setting}", value)
    elif setting == "limit":
        if not value.isdigit() or not 1 <= int(value) <= 100:
            await update.effective_message.reply_text("❌ Укажи число от 1 до 100.")
            return
        db.set_setting(chat_id, "max_warnings", value)
    elif setting == "log":
        if value:
            try: int(value)
            except ValueError:
                await update.effective_message.reply_text("❌ Нужен числовой chat_id.")
                return
        db.set_setting(chat_id, "log_chat_id", value)
    context.user_data.pop("setting", None)
    await update.effective_message.reply_text("✅ Настройка сохранена. Открой /panel → ⚙️ Настройки.")


async def post_init(application):
    for p in db.active_expiring():
        await schedule_expiry(application, p)
    log.info("Bot started")


def main():
    db.init()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("unwarn", unwarn_cmd))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setting_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
