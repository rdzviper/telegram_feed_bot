import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
import database as db
import analyzer
import config
import pipeline

logger = logging.getLogger(__name__)

# Telegram: 4096 симв/сообщение и 100 кнопок/клавиатуру. Держим запас,
# иначе при большом числе каналов /stats падает с BadRequest.
_MSG_CHAR_LIMIT = 3800
_KB_ROW_LIMIT = 90
_STATS_HEADER = "📊 <b>Каналы</b> (по рейтингу)\n\n"


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


def _render_stats_pages(channels: list[dict]) -> list[tuple[str, InlineKeyboardMarkup]]:
    """Разбивает каналы на страницы под лимиты Telegram.

    Возвращает список (text, keyboard). Каждая страница влезает и по символам,
    и по числу кнопок — так /stats не падает при любом числе каналов.
    """
    pages: list[tuple[str, InlineKeyboardMarkup]] = []
    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    length = len(_STATS_HEADER)

    def flush():
        if lines:
            pages.append(
                (_STATS_HEADER + "\n\n".join(lines), InlineKeyboardMarkup(buttons))
            )

    for ch in channels:
        line = _channel_line(ch)
        extra = len(line) + 2  # разделитель "\n\n"
        if lines and (length + extra > _MSG_CHAR_LIMIT or len(buttons) >= _KB_ROW_LIMIT):
            flush()
            lines, buttons, length = [], [], len(_STATS_HEADER)
        lines.append(line)
        buttons.append([_channel_button(ch)])
        length += extra
    flush()
    return pages


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    channels = await db.get_channels_stats()
    if not channels:
        await update.message.reply_text("Нет данных — бот ещё не видел сообщений.")
        return

    for text, keyboard in _render_stats_pages(channels):
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _authorized(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    data = query.data

    if data.startswith(("like:", "dislike:")):
        choice, msg_id_str = data.split(":", 1)
        msg_id = int(msg_id_str)

        # защита от повторного голосования
        prev = await db.get_user_choice(msg_id)
        if prev:
            await query.answer("Уже оценено")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        await db.set_user_choice(msg_id, choice)
        await db.increment_channel_vote(msg_id, choice)
        concepts = await db.get_message_concepts(msg_id)
        if concepts:
            delta = 1.0 if choice == "like" else -0.3
            await db.update_interests(concepts, delta=delta)
            logger.info("%s concepts=%s delta=%s", choice, concepts, delta)
            # веса изменились — пересчитываем порог (единственный триггер)
            await pipeline.refresh_threshold()
        label = "✅ Интересно" if choice == "like" else "❌ Не интересно"
        await query.answer(label)

        # медиа-пост несёт caption, текстовый — text; убираем кнопки после голоса
        try:
            if query.message.caption is not None:
                await query.edit_message_caption(
                    caption=f"{query.message.caption_html}\n{label}",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await query.edit_message_text(
                    text=f"{query.message.text_html}\n{label}",
                    parse_mode="HTML",
                    reply_markup=None,
                )
        except Exception as e:
            logger.error("edit after vote failed: %s", e)

    elif data.startswith("toggle:"):
        channel_id = int(data.split(":", 1)[1])
        new_state = await db.toggle_channel(channel_id)
        status = "включён ✅" if new_state else "отключён ❌"
        await query.answer(status, show_alert=True)
        # Стата может быть разбита на несколько сообщений — не ре-рендерим всё
        # (это и роняло /stats). Обновляем только кнопку в ЭТОМ сообщении.
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


def register_handlers(application):
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
