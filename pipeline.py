import html
import io
import logging
import os
from collections import deque
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.extensions import html as tl_html
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import analyzer

logger = logging.getLogger(__name__)

_forwarded_times: deque = deque()
_drift_counter = 0


def _message_html(message, limit: int) -> str:
    """Конвертирует форматирование поста (bold, ссылки и т.д.) в HTML для Bot API."""
    raw = message.raw_text or ""
    entities = message.entities or []
    if len(raw) > limit:
        raw = raw[:limit]
        entities = [e for e in entities if e.offset + e.length <= limit]
    return tl_html.unparse(raw, entities)


def _rate_limit_ok() -> bool:
    now = datetime.now(timezone.utc).timestamp()
    while _forwarded_times and now - _forwarded_times[0] > 3600:
        _forwarded_times.popleft()
    return len(_forwarded_times) < config.RATE_LIMIT_PER_HOUR


async def process_message(event, tg_client: TelegramClient, bot: Bot):
    global _drift_counter

    text = event.message.text or ""
    if not text.strip():
        logger.debug("Empty message, skipping")
        return

    channel_id = event.chat_id
    channel_name = (
        event.chat.title if event.chat and hasattr(event.chat, "title") else str(channel_id)
    )

    logger.info("Incoming: channel=%s text=%.80r", channel_name, text)

    await db.upsert_channel(channel_id, channel_name)
    await db.increment_seen(channel_id)

    if not await db.is_channel_active(channel_id):
        logger.debug("channel=%s is disabled, skipping", channel_name)
        return

    recent = await db.get_recent_headlines(hours=24, limit=30)

    logger.debug("Sending to DeepSeek: channel=%s recent=%d", channel_name, len(recent))
    result = await analyzer.analyze(text, recent)
    concepts = result["concepts"]
    is_ad = result["is_ad"]
    same_story = result["same_story"]

    # локальный scoring по матрице весов
    weights = await db.get_concept_weights(concepts)
    local_score = sum(weights.get(c, 0.15) for c in concepts)
    threshold = await db.get_adaptive_threshold()

    logger.info(
        "Analyzed: channel=%s concepts=%s local_score=%.1f threshold=%.1f is_ad=%s same_story=%s",
        channel_name, concepts, local_score, threshold, is_ad, same_story,
    )

    try:
        msg_id = await db.save_message(
            channel_id=channel_id,
            channel_name=channel_name,
            orig_message_id=event.message.id,
            text=text,
            date=event.message.date.isoformat(),
            score=round(local_score),
            is_ad=is_ad,
            same_story=same_story,
        )
        await db.save_message_concepts(msg_id, concepts, local_score)
    except Exception as e:
        logger.error("DB save failed: %s", e, exc_info=True)
        return

    if is_ad:
        logger.info("channel=%s filtered: ad", channel_name)
        return
    if same_story:
        logger.info("channel=%s filtered: duplicate story", channel_name)
        return

    should_forward = local_score >= threshold
    is_drift = False

    if not should_forward:
        _drift_counter += 1
        if _drift_counter >= config.DRIFT_EVERY_N:
            _drift_counter = 0
            should_forward = True
            is_drift = True
            logger.info("Drift forward triggered (every %d)", config.DRIFT_EVERY_N)

    if not should_forward:
        logger.info("channel=%s local_score=%.1f < threshold=%.1f, skip",
                    channel_name, local_score, threshold)
        return

    if not _rate_limit_ok():
        logger.warning("Rate limit reached, skipping %s", channel_name)
        return

    logger.info("Forwarding: channel=%s local_score=%.1f threshold=%.1f drift=%s",
                channel_name, local_score, threshold, is_drift)

    username = getattr(event.chat, "username", None)
    if username:
        post_link = f"https://t.me/{username}/{event.message.id}"
    else:
        bare_id = str(abs(event.chat_id))[3:]
        post_link = f"https://t.me/c/{bare_id}/{event.message.id}"

    prefix = "🔍 " if is_drift else ""
    header = f'{prefix}<a href="{post_link}">{html.escape(channel_name)}</a>'
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍", callback_data=f"like:{msg_id}"),
            InlineKeyboardButton("👎", callback_data=f"dislike:{msg_id}"),
        ]
    ])

    body_short = _message_html(event.message, 900)
    body_full = _message_html(event.message, 3500)
    media_caption = f"{header}\n\n{body_short}" if body_short else header

    # файлы крупнее лимита отправляем как текст со ссылкой
    _MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_MB", "10")) * 1024 * 1024
    file_size = getattr(getattr(event.message, "file", None), "size", 0) or 0
    send_as_text = not event.message.media or file_size > _MAX_MEDIA_BYTES
    if file_size > _MAX_MEDIA_BYTES:
        logger.info("Media too large (%d MB), sending text only", file_size // 1024 // 1024)

    bot_message_id = None
    if send_as_text:
        try:
            sent = await bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text=f"{header}\n\n{body_full}",
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            bot_message_id = sent.message_id
        except Exception as e:
            logger.error("Send message failed: %s", e)
    else:
        try:
            buf = io.BytesIO(await tg_client.download_media(event.message, bytes))
        except Exception as e:
            logger.error("Download failed: %s", e)
            buf = None
        if buf is not None:
            try:
                if event.message.photo:
                    buf.name = "photo.jpg"
                    sent = await bot.send_photo(config.NOTIFY_CHAT_ID, photo=buf, caption=media_caption, parse_mode="HTML", reply_markup=keyboard)
                elif event.message.video:
                    buf.name = "video.mp4"
                    sent = await bot.send_video(config.NOTIFY_CHAT_ID, video=buf, caption=media_caption, parse_mode="HTML", reply_markup=keyboard)
                elif event.message.gif:
                    buf.name = "animation.mp4"
                    sent = await bot.send_animation(config.NOTIFY_CHAT_ID, animation=buf, caption=media_caption, parse_mode="HTML", reply_markup=keyboard)
                else:
                    buf.name = "file"
                    sent = await bot.send_document(config.NOTIFY_CHAT_ID, document=buf, caption=media_caption, parse_mode="HTML", reply_markup=keyboard)
                bot_message_id = sent.message_id
            except Exception as e:
                logger.error("Upload failed: %s", e)

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.mark_forwarded(msg_id, bot_message_id, is_drift)
    await db.increment_forwarded(channel_id)
