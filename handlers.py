import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageReactionHandler,
)
import database as db
import config
import pipeline

logger = logging.getLogger(__name__)

# Subset of telegram.constants.ReactionEmoji (the full default Bot API catalog,
# verified 2026-08-05 against python-telegram-bot 21.6 — 💩 IS in the default
# set, not custom_emoji-only as assumed during design). Reactions outside these
# two buckets (incl. genuine custom_emoji, Premium accounts) are ignored.
_LIKE_EMOJI = {"👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "👌",
               "😍", "❤‍🔥", "💯", "🤣", "🏆", "🆒", "💘", "😘", "😎"}
_DISLIKE_EMOJI = {"👎", "🤬", "😢", "🤮", "💩", "💔", "🖕", "😭", "😡", "🤡"}

# Telegram: 4096 chars/message and 100 buttons/keyboard. Keep headroom,
# otherwise large channel lists fail with BadRequest.
_MSG_CHAR_LIMIT = 3800
_KB_ROW_LIMIT = 90
_STATS_HEADER = "📊 <b>Статистика каналов</b> (А→Я)\n\n"
_CHANNELS_HEADER = "🎛 <b>Управление каналами</b> (А→Я)\nНажми, чтобы включить/отключить.\n\n"


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.ALLOWED_USER_ID


def _channel_line(ch: dict) -> str:
    total = ch["likes"] + ch["dislikes"]
    ratio = ch["likes"] / total if total > 0 else 0
    filled = round(ratio * 5)
    bar = "█" * filled + "░" * (5 - filled)
    status = " ❌" if not ch["active"] else ""
    return (
        f"{bar} <b>{ch['channel_name']}</b>{status}\n"
        f"  👍{ch['likes']} 👎{ch['dislikes']} · "
        f"видел {ch['seen']} · слал {ch['forwarded']}"
    )


def _channel_button(ch: dict) -> InlineKeyboardButton:
    label = f"{'✅' if ch['active'] else '❌'} {ch['channel_name']}"
    return InlineKeyboardButton(label, callback_data=f"toggle:{ch['channel_id']}")


def _render_stats_pages(channels: list[dict]) -> list[str]:
    """Text-only stats pages split under the 4096-char limit."""
    pages: list[str] = []
    lines: list[str] = []
    length = len(_STATS_HEADER)
    for ch in channels:
        line = _channel_line(ch)
        extra = len(line) + 2  # "\n\n" separator
        if lines and length + extra > _MSG_CHAR_LIMIT:
            pages.append(_STATS_HEADER + "\n\n".join(lines))
            lines, length = [], len(_STATS_HEADER)
        lines.append(line)
        length += extra
    if lines:
        pages.append(_STATS_HEADER + "\n\n".join(lines))
    return pages


def _render_channel_pages(channels: list[dict]) -> list[tuple[str, InlineKeyboardMarkup]]:
    """Channel toggle pages split under the 100-button limit."""
    pages: list[tuple[str, InlineKeyboardMarkup]] = []
    buttons: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        if len(buttons) >= _KB_ROW_LIMIT:
            pages.append((_CHANNELS_HEADER, InlineKeyboardMarkup(buttons)))
            buttons = []
        buttons.append([_channel_button(ch)])
    if buttons:
        pages.append((_CHANNELS_HEADER, InlineKeyboardMarkup(buttons)))
    return pages


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    channels = await db.get_channels_stats()
    if not channels:
        await update.message.reply_text("Нет данных — бот ещё не видел сообщений.")
        return

    channels.sort(key=lambda c: c["channel_name"].casefold())
    for text in _render_stats_pages(channels):
        await update.message.reply_text(text, parse_mode="HTML")


async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    channels = await db.get_channels_stats()
    if not channels:
        await update.message.reply_text("Нет каналов — бот ещё не синхронизировал диалоги.")
        return

    channels.sort(key=lambda c: c["channel_name"].casefold())
    for text, keyboard in _render_channel_pages(channels):
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard
        )


async def reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Native reaction on a DM copy = vote. No companion, no anchor: first
    reaction from a known bucket sticks (db.get_user_choice guard); later
    changes to the reaction are not un-applied (weights model is not
    reversible). See SPEC.md 'Отказ от отдельного notify-чата' revision."""
    mr = update.message_reaction
    if mr is None or mr.chat.type != "private" or mr.chat.id != config.DM_CHAT_ID:
        return
    if mr.user is None or mr.user.id != config.ALLOWED_USER_ID:
        return
    if not mr.new_reaction:
        return  # reaction removed, not added — no vote to apply

    emoji = None
    for r in mr.new_reaction:
        if r.type == "emoji":
            emoji = r.emoji
            break
    if emoji is None:
        return  # custom_emoji reaction (Premium) — not handled yet

    if emoji in _LIKE_EMOJI:
        choice = "like"
    elif emoji in _DISLIKE_EMOJI:
        choice = "dislike"
    else:
        return  # not a known vote emoji — ignore

    msg_id = await db.resolve_msg_id(mr.message_id)
    if msg_id is None:
        return
    if await db.get_user_choice(msg_id):
        return  # already voted

    concepts = await db.get_message_concepts(msg_id)
    delta = 1.0 if choice == "like" else -0.3
    if concepts:
        await db.update_interests(concepts, delta=delta)
    await db.set_user_choice(msg_id, choice)
    await db.increment_channel_vote(msg_id, choice)
    await pipeline.refresh_threshold()
    logger.info("post %s concepts=%d delta=%s", choice, len(concepts), delta)

    if concepts:
        mark = "👍" if choice == "like" else "👎"
        await context.bot.send_message(
            chat_id=config.DM_CHAT_ID, text=f"{mark} {choice}: {', '.join(concepts)}"
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _authorized(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    data = query.data or ""

    if data.startswith("toggle:"):
        channel_id = int(data.split(":", 1)[1])
        new_state = await db.toggle_channel(channel_id)
        status = "включён ✅" if new_state else "отключён ❌"
        await query.answer(status, show_alert=True)
        # /channels may span several messages — re-render only this one.
        old_rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
        new_rows = []
        for row in old_rows:
            new_row = []
            for btn in row:
                if btn.callback_data == data:
                    name = btn.text.split(" ", 1)[1] if " " in btn.text else btn.text
                    new_label = f"{'✅' if new_state else '❌'} {name}"
                    new_row.append(InlineKeyboardButton(new_label, callback_data=data))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(new_rows)
            )
        except Exception:
            pass

    else:
        await query.answer()


async def error_handler(update, context):
    err = context.error
    from telegram.error import TimedOut, NetworkError
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Telegram network error: %s", err)
    else:
        logger.error("Unhandled telegram error", exc_info=err)


BOT_COMMANDS = [
    BotCommand("stats", "Статистика каналов"),
    BotCommand("channels", "Управление каналами (вкл/выкл)"),
]


async def set_bot_commands(application):
    """Register bot command menu (the «/» button in Telegram clients)."""
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application):
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("channels", channels_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageReactionHandler(reaction_callback))
    application.add_error_handler(error_handler)
