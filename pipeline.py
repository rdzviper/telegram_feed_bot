import html
import io
import logging
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
    local_score = sum(weights.get(c, 0.0) for c in concepts)
    threshold = await db.get_adaptive_threshold()

    logger.info(
        "Analyzed: channel=%s concepts=%s local_score=%.1f threshold=%.1f is_ad=%s same_story=%s",
        channel_name, concepts, local_score, threshold, is_ad, same_story,
    )

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

    if is_ad:
        logger.debug("channel=%s filtered: ad", channel_name)
        return
    if same_story:
        logger.debug("channel=%s filtered: duplicate story", channel_name)
        return

    should_forward = local_score >= threshold
    is_drift = False

    if not should_forward:
        _drift_counter += 1
        if _drift_counter >= config.DRIFT_EVERY_N:
            _drift_counter = 0
            should_forward = True
            is_drift = True
            logger.debug("Drift forward triggered (every %d)", config.DRIFT_EVERY_N)

    if not should_forward:
        logger.debug("channel=%s local_score=%.1f < threshold=%.1f, skipping",
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

    body = _message_html(event.message, 900)  # caption limit 1024, оставляем место для header
    media_caption = f"{header}\n\n{body}" if body else header

    bot_message_id = None
    try:
        if event.message.media:
            buf = io.BytesIO(await tg_client.download_media(event.message, bytes))
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
        else:
            body = _message_html(event.message, 3500)
            sent = await bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text=f"{header}\n\n{body}",
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        bot_message_id = sent.message_id
    except Exception as e:
        logger.error("Send failed: %s", e)

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.mark_forwarded(msg_id, bot_message_id, is_drift)
    await db.increment_forwarded(channel_id)
