import logging
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus, ParseMode, ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

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


async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    """Check the user's REAL role in the current Telegram chat."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except (BadRequest, Forbidden):
        return False


async def require_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id=None) -> bool:
    """Authorization by chat role, not by ADMIN_IDS."""
    user = update.effective_user
    bot = context.bot
    target_chat = chat_id if chat_id is not None else update.effective_chat.id

    if not user or not await is_chat_admin(bot, target_chat, user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ Только для администраторов этого чата.", show_alert=True)
        elif update.effective_message:
            # Do not expose authorization errors in the group.
            try:
                await update.effective_message.delete()
            except Exception:
                pass
        return False
    return True


async def delete_command(message):
    """Delete moderator commands so normal members do not see them."""
    try:
        await message.delete()
    except Exception:
        pass


async def send_private_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup=None):
    """Send the moderator result/panel privately. User must have started the bot."""
    try:
        return await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    except Forbidden:
        return None


async def private_or_group_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """For admin-only UX: delete group command and send the result to admin DM."""
    user = update.effective_user
    await delete_command(update.effective_message)
    if user:
        sent = await send_private_admin(context, user.id, text)
        if sent is None:
            # The only group-visible message is a short instruction, and it is deleted immediately.
            msg = await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Сначала откройте личный чат с ботом и отправьте /start. После этого ответы модерации будут приходить только вам.",
            )
            try:
                await msg.delete()
            except Exception:
                pass


async def target_from_reply(update: Update):
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return None
    return msg.reply_to_message.from_user


async def is_protected_member(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


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
            chat_id,
            user_id,
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
    await log_action(
        application,
        chat_id,
        0,
        "Автоматическое окончание" if automatic else "Снятие наказания",
        user_id,
        f"punishment #{p['id']}",
    )


def panel_keyboard(chat_id):
    # chat_id is embedded so a private panel still controls the original group.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Все наказания", callback_data=f"panel:punishments:{chat_id}:0")],
        [InlineKeyboardButton("👥 Пользователи", callback_data=f"panel:users:{chat_id}:0")],
        [InlineKeyboardButton("📝 Журнал действий", callback_data=f"panel:logs:{chat_id}:0")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data=f"panel:settings:{chat_id}")],
        [InlineKeyboardButton("👮 Администраторы", callback_data=f"panel:admins:{chat_id}")],
    ])


async def start(update, context):
    # /start in private chat is intentionally private. In a group, delete it.
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "🛡 <b>Модератор готов.</b>\n\n"
            "Теперь ответы команд /mute, /warn, /ban и /panel будут приходить сюда, только вам.\n"
            "В группе команды модераторов автоматически удаляются.",
            parse_mode=ParseMode.HTML,
        )
        return
    if await require_chat_admin(update, context):
        await delete_command(update.effective_message)
        await send_private_admin(
            context,
            update.effective_user.id,
            "🛡 <b>Бот готов.</b>\nОткройте личный чат с ботом и нажмите /start.",
        )


async def panel(update, context):
    if update.effective_chat.type == ChatType.PRIVATE:
        # A private /panel must specify the group somehow; use the most recent chat stored for the admin.
        chat_id = context.user_data.get("panel_chat_id")
        if not chat_id:
            await update.effective_message.reply_text("Откройте /panel из нужной группы. Команда будет удалена, а панель придёт сюда.")
            return
        if not await is_chat_admin(context.bot, chat_id, update.effective_user.id):
            await update.effective_message.reply_text("⛔ Вы больше не администратор этого чата.")
            return
        await update.effective_message.reply_text(
            "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыберите раздел:",
            reply_markup=panel_keyboard(chat_id),
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    context.user_data["panel_chat_id"] = chat_id
    await delete_command(update.effective_message)
    sent = await send_private_admin(
        context,
        update.effective_user.id,
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыберите раздел:",
        panel_keyboard(chat_id),
    )
    if sent is None:
        msg = await context.bot.send_message(
            chat_id,
            "⚠️ Откройте личный чат с ботом и отправьте /start, затем снова /panel в группе.",
        )
        try:
            await msg.delete()
        except Exception:
            pass


async def id_cmd(update, context):
    # ID is harmless; in group delete the command and DM the answer.
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(f"Твой ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)
    else:
        await delete_command(update.effective_message)
        await send_private_admin(context, update.effective_user.id, f"Твой ID: <code>{update.effective_user.id}</code>")


async def mute_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ <b>/mute</b> нужно использовать ответом на сообщение пользователя.\nПример: <code>/mute 30m флуд</code>")
        return
    if await is_protected_member(context.bot, chat_id, target.id):
        await private_or_group_notice(update, context, "❌ Нельзя мутить владельца или администратора.")
        return
    duration = context.args[0] if context.args else db.get_setting(chat_id, "default_mute", "10m")
    seconds = parse_duration(duration)
    if seconds is None:
        await private_or_group_notice(update, context, "❌ Формат срока: <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>3d</code>, <code>1w</code>.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Без причины"
    expires = now_ts() + seconds
    await context.bot.restrict_chat_member(
        chat_id,
        target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=datetime.fromtimestamp(expires, tz=timezone.utc),
    )
    p = db.add_punishment(chat_id, target.id, target.username, target.first_name, "mute", reason, expires, update.effective_user.id)
    await schedule_expiry(context.application, p)
    await log_action(context.application, chat_id, update.effective_user.id, "Мут", target.id, reason, human_seconds(seconds))
    await delete_command(update.effective_message)
    await send_private_admin(
        context,
        update.effective_user.id,
        f"🔇 <b>{target.mention_html()}</b> получил мут на <b>{human_seconds(seconds)}</b>.\nПричина: {reason}",
    )


async def unmute_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение пользователя: <code>/unmute</code>")
        return
    await context.bot.restrict_chat_member(
        chat_id,
        target.id,
        permissions=ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True, can_pin_messages=True,
            can_manage_topics=True,
        ),
    )
    for p in db.active_for_user(chat_id, target.id, "mute"):
        db.close_punishment(p["id"], "removed")
    await log_action(context.application, chat_id, update.effective_user.id, "Размут", target.id)
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, f"🔊 Мут с {target.mention_html()} снят.")


async def warn_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/warn 7d причина</code>")
        return
    if await is_protected_member(context.bot, chat_id, target.id):
        await private_or_group_notice(update, context, "❌ Нельзя выдать предупреждение администратору.")
        return
    duration = context.args[0] if context.args else db.get_setting(chat_id, "default_warn", "7d")
    seconds = parse_duration(duration)
    if seconds is None:
        await private_or_group_notice(update, context, "❌ Формат срока: <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>3d</code>, <code>1w</code>.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Без причины"
    p = db.add_punishment(chat_id, target.id, target.username, target.first_name, "warn", reason, now_ts() + seconds, update.effective_user.id)
    await schedule_expiry(context.application, p)
    count = len(db.active_for_user(chat_id, target.id, "warn"))
    await log_action(context.application, chat_id, update.effective_user.id, "Предупреждение", target.id, reason, human_seconds(seconds))
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, f"⚠️ {target.mention_html()} получил предупреждение.\nПричина: <b>{reason}</b>\nАктивных: <b>{count}</b>")

    limit = int(db.get_setting(chat_id, "max_warnings", "3"))
    if count >= limit:
        mute_duration = db.get_setting(chat_id, "default_mute", "10m")
        mute_seconds = parse_duration(mute_duration)
        if mute_seconds:
            expires = now_ts() + mute_seconds
            await context.bot.restrict_chat_member(chat_id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.fromtimestamp(expires, tz=timezone.utc))
            mp = db.add_punishment(chat_id, target.id, target.username, target.first_name, "mute", f"Автоматически: {limit} предупреждений", expires, update.effective_user.id)
            await schedule_expiry(context.application, mp)
            await send_private_admin(context, update.effective_user.id, f"🔇 Лимит {limit} предупреждений достигнут. Автомут: {human_seconds(mute_seconds)}.")


async def unwarn_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/unwarn</code>")
        return
    rows = db.active_for_user(chat_id, target.id, "warn")
    if not rows:
        await private_or_group_notice(update, context, "✅ Активных предупреждений нет.")
        return
    db.close_punishment(rows[-1]["id"], "removed")
    await log_action(context.application, chat_id, update.effective_user.id, "Снятие предупреждения", target.id, f"punishment #{rows[-1]['id']}")
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, "✅ Последнее предупреждение снято.")


async def warnings_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/warnings</code>")
        return
    rows = db.active_for_user(chat_id, target.id, "warn")
    if not rows:
        await private_or_group_notice(update, context, "✅ Активных предупреждений нет.")
        return
    lines = [f"⚠️ <b>Предупреждения {target.mention_html()}</b>"]
    for p in rows:
        lines.append(f"#{p['id']} — {p['reason']} — осталось {human_seconds(p['expires_at'] - now_ts())}")
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, "\n".join(lines))


async def ban_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/ban [срок] причина</code>. Без срока — навсегда.")
        return
    if await is_protected_member(context.bot, chat_id, target.id):
        await private_or_group_notice(update, context, "❌ Нельзя забанить владельца или администратора.")
        return
    args = context.args
    seconds = parse_duration(args[0]) if args else None
    reason = " ".join(args[1:]) if seconds else " ".join(args)
    expires = now_ts() + seconds if seconds else None
    await context.bot.ban_chat_member(chat_id, target.id, until_date=datetime.fromtimestamp(expires, tz=timezone.utc) if expires else None)
    p = db.add_punishment(chat_id, target.id, target.username, target.first_name, "ban", reason or "Без причины", expires, update.effective_user.id)
    if expires:
        await schedule_expiry(context.application, p)
    await log_action(context.application, chat_id, update.effective_user.id, "Бан", target.id, reason or "Без причины", human_seconds(seconds) if seconds else "навсегда")
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, f"🚫 {target.mention_html()} заблокирован.\nСрок: {human_seconds(seconds) if seconds else 'навсегда'}\nПричина: {reason or 'Без причины'}")


async def unban_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/unban</code>")
        return
    await context.bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
    for p in db.active_for_user(chat_id, target.id, "ban"):
        db.close_punishment(p["id"], "removed")
    await log_action(context.application, chat_id, update.effective_user.id, "Разбан", target.id)
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, "🔓 Пользователь разблокирован.")


async def kick_cmd(update, context):
    chat_id = update.effective_chat.id
    if not await require_chat_admin(update, context, chat_id):
        return
    target = await target_from_reply(update)
    if not target:
        await private_or_group_notice(update, context, "❌ Ответьте на сообщение: <code>/kick причина</code>")
        return
    if await is_protected_member(context.bot, chat_id, target.id):
        await private_or_group_notice(update, context, "❌ Нельзя кикнуть администратора.")
        return
    await context.bot.ban_chat_member(chat_id, target.id)
    await context.bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
    reason = " ".join(context.args) or "Без причины"
    await log_action(context.application, chat_id, update.effective_user.id, "Кик", target.id, reason)
    await delete_command(update.effective_message)
    await send_private_admin(context, update.effective_user.id, f"👢 {target.mention_html()} исключён.\nПричина: {reason}")


def page_buttons(prefix, chat_id, page, total, per_page=5):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{chat_id}:{page-1}"))
    if (page + 1) * per_page < total:
        buttons.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{chat_id}:{page+1}"))
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
        buttons.append([InlineKeyboardButton(f"Снять #{p['id']}", callback_data=f"lift:{p['id']}:{chat_id}:{page}")])
    buttons += page_buttons("panel:punishments", chat_id, page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data=f"panel:home:{chat_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_logs(query, chat_id, page=0):
    per = 8
    rows, total = db.logs_page(chat_id, page, per)
    text = "📝 <b>ЖУРНАЛ ДЕЙСТВИЙ</b>\n\n"
    for r in rows:
        text += f"#{r['id']} — {r['action']} — target <code>{r['target_id']}</code>\n{r['reason'] or '—'}\n"
    if not rows:
        text += "Журнал пуст."
    buttons = page_buttons("panel:logs", chat_id, page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data=f"panel:home:{chat_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_users(query, chat_id, page=0):
    per = 8
    rows, total = db.users_page(chat_id, page, per)
    text = "👥 <b>ПОЛЬЗОВАТЕЛИ С НАКАЗАНИЯМИ</b>\n\n"
    for r in rows:
        text += f"• {user_label(r['user_id'], r['username'], r['first_name'])}: {r['cnt']}\n"
    if not rows:
        text += "Нет данных."
    buttons = page_buttons("panel:users", chat_id, page, total, per)
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data=f"panel:home:{chat_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_settings(query, chat_id):
    s = db.settings(chat_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔇 Мут: {s['default_mute']}", callback_data=f"set:mute:{chat_id}")],
        [InlineKeyboardButton(f"⚠️ Варн: {s['default_warn']}", callback_data=f"set:warn:{chat_id}")],
        [InlineKeyboardButton(f"🔢 Лимит: {s['max_warnings']}", callback_data=f"set:limit:{chat_id}")],
        [InlineKeyboardButton(f"📢 Логи: {s['log_chat_id'] or 'не задан'}", callback_data=f"set:log:{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"panel:home:{chat_id}")],
    ])
    await query.edit_message_text("⚙️ <b>НАСТРОЙКИ ЧАТА</b>\n\nНажми настройку, затем отправь значение в этом личном чате.", reply_markup=kb, parse_mode=ParseMode.HTML)


async def callback(update, context):
    q = update.callback_query
    data = q.data.split(":")
    await q.answer()

    try:
        if data[0] == "lift":
            _, pid, chat_id, page = data
            chat_id = int(chat_id)
        elif data[0] == "panel":
            # panel:<section>:<chat_id>[:page] or panel:home:<chat_id>
            chat_id = int(data[2])
        elif data[0] == "set":
            _, setting, chat_id = data
            chat_id = int(chat_id)
        else:
            await q.answer("Неизвестная команда", show_alert=True)
            return
    except (ValueError, IndexError):
        await q.answer("Некорректная кнопка", show_alert=True)
        return

    if not await is_chat_admin(context.bot, chat_id, q.from_user.id):
        await q.answer("⛔ Вы больше не администратор этого чата.", show_alert=True)
        return

    if data[0] == "panel":
        section = data[1]
        if section == "home":
            await q.edit_message_text("🛠 <b>АДМИН-ПАНЕЛЬ</b>", reply_markup=panel_keyboard(chat_id), parse_mode=ParseMode.HTML)
        elif section == "punishments":
            await show_punishments(q, chat_id, int(data[3]))
        elif section == "logs":
            await show_logs(q, chat_id, int(data[3]))
        elif section == "users":
            await show_users(q, chat_id, int(data[3]))
        elif section == "settings":
            await show_settings(q, chat_id)
        elif section == "admins":
            try:
                admins = await context.bot.get_chat_administrators(chat_id)
                lines = []
                for m in admins:
                    name = m.user.mention_html()
                    role = "👑 владелец" if m.status == ChatMemberStatus.OWNER else "🛡 администратор"
                    lines.append(f"{role} — {name}")
                text = "👮 <b>АДМИНИСТРАТОРЫ ЧАТА</b>\n\n" + "\n".join(lines)
            except Exception:
                text = "❌ Не удалось получить список администраторов."
            await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"panel:home:{chat_id}")]]), parse_mode=ParseMode.HTML)
        return

    if data[0] == "set":
        _, setting, chat_id_s = data
        context.user_data["setting"] = setting
        context.user_data["setting_chat_id"] = chat_id
        await q.edit_message_text("✏️ Отправь новое значение отдельным сообщением в этом личном чате.\n\nДля логов: ID чата или пусто.")
        return

    if data[0] == "lift":
        _, pid, chat_id_s, page = data
        p = db.get_punishment(int(pid))
        if not p or p["status"] != "active":
            await q.answer("Наказание уже снято.", show_alert=True)
            await show_punishments(q, chat_id, int(page))
            return
        await lift_punishment(context.application, p, automatic=False)
        await q.answer("Наказание снято.")
        await show_punishments(q, chat_id, int(page))


async def setting_message(update, context):
    # Settings are entered only in the private admin chat.
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    setting = context.user_data.get("setting")
    chat_id = context.user_data.get("setting_chat_id")
    if not setting or not chat_id:
        return
    if not await is_chat_admin(context.bot, chat_id, update.effective_user.id):
        context.user_data.pop("setting", None)
        context.user_data.pop("setting_chat_id", None)
        return

    value = update.effective_message.text.strip()
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
            try:
                int(value)
            except ValueError:
                await update.effective_message.reply_text("❌ Нужен числовой chat_id.")
                return
        db.set_setting(chat_id, "log_chat_id", value)

    context.user_data.pop("setting", None)
    context.user_data.pop("setting_chat_id", None)
    await update.effective_message.reply_text("✅ Настройка сохранена.")


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
