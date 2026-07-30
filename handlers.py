import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
import database as db
import analyzer
import config
import pipeline
import keyboards

logger = logging.getLogger(__name__)

# Telegram: 4096 симв/сообщение и 100 кнопок/клавиатуру. Держим запас,
# иначе при большом числе каналов команды падают с BadRequest.
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


# --- голосование по топикам поста ---
# Концепты теперь всегда развёрнуты (компаньон отправляется сразу с рядами
# голосов, см. pipeline.deliver_batch); клавиатуру для перерисовки после
# голоса строит общий keyboards.companion_keyboard.


def _render_stats_pages(channels: list[dict]) -> list[str]:
    """Текстовые страницы статистики (без кнопок), разбитые под лимит 4096 симв."""
    pages: list[str] = []
    lines: list[str] = []
    length = len(_STATS_HEADER)
    for ch in channels:
        line = _channel_line(ch)
        extra = len(line) + 2  # разделитель "\n\n"
        if lines and length + extra > _MSG_CHAR_LIMIT:
            pages.append(_STATS_HEADER + "\n\n".join(lines))
            lines, length = [], len(_STATS_HEADER)
        lines.append(line)
        length += extra
    if lines:
        pages.append(_STATS_HEADER + "\n\n".join(lines))
    return pages


def _render_channel_pages(channels: list[dict]) -> list[tuple[str, InlineKeyboardMarkup]]:
    """Страницы управления: заголовок + кнопки-тумблеры, разбитые под лимит 100 кнопок."""
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _authorized(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    data = query.data

    if data.startswith("page:"):
        # «Промотать»/«Следующий» — всегда отдаёт следующие до PAGE_SIZE постов
        # из очереди, ничего не пропуская. Единственная кнопка внизу ленты —
        # если жмут не на актуальном якоре (уже прибит новым постом), молчим.
        msg_id_str = data.split(":", 1)[1]
        anchor_msg_id = await db.get_setting("anchor_msg_id")
        if anchor_msg_id != msg_id_str:
            await query.answer("Устарело")
            return
        await query.answer()
        await pipeline.deliver_batch()

    elif data.startswith(("tl:", "td:")):
        # голос по одному топику: вес правим точечно и только на первый голос
        kind, msg_id_str, idx_str = data.split(":")
        msg_id, idx = int(msg_id_str), int(idx_str)
        choice = "like" if kind == "tl" else "dislike"

        concepts = await db.get_message_concepts(msg_id)
        if idx >= len(concepts):
            await query.answer("Устарело", show_alert=True)
            return
        topic = concepts[idx]

        # идемпотентность на уровне БД: вес применяем только если голос новый
        if await db.add_topic_vote(msg_id, idx, choice):
            delta = 1.0 if choice == "like" else -0.3
            await db.update_interests([topic], delta=delta)
            await db.increment_channel_vote(msg_id, choice)
            await pipeline.refresh_threshold()
            logger.info("topic %s %r delta=%s", choice, topic, delta)
            await query.answer("👍 +1.0" if choice == "like" else "👎 −0.3")
        else:
            await query.answer("Уже оценено")

        votes = await db.get_topic_votes(msg_id)
        anchor_msg_id = await db.get_setting("anchor_msg_id")
        with_pager = anchor_msg_id == str(msg_id)
        try:
            await query.edit_message_reply_markup(
                reply_markup=keyboards.companion_keyboard(msg_id, concepts, votes, with_pager)
            )
        except Exception as e:
            logger.error("topic vote edit failed: %s", e)

    elif data.startswith("toggle:"):
        channel_id = int(data.split(":", 1)[1])
        new_state = await db.toggle_channel(channel_id)
        status = "включён ✅" if new_state else "отключён ❌"
        await query.answer(status, show_alert=True)
        # /channels может быть разбит на несколько сообщений — не ре-рендерим всё.
        # Обновляем только кнопку в ЭТОМ сообщении.
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
    # таймауты и сетевые ошибки — обычное дело, не нужен трейсбэк
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
    """Регистрирует меню команд бота (кнопка «/» в клиенте Telegram)."""
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application):
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("channels", channels_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
