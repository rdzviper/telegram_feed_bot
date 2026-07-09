import html
import logging
from collections import deque
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.functions.messages import MarkDialogUnreadRequest
from telethon.tl.types import InputDialogPeer
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import analyzer

logger = logging.getLogger(__name__)

_forwarded_times: deque = deque()
_drift_counter = 0



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

    logger.debug("Sending to DeepSeek: channel=%s", channel_name)
    result = await analyzer.analyze(text)
    concepts = result["concepts"]
    is_ad = result["is_ad"]

    # локальный scoring по матрице весов
    weights = await db.get_concept_weights(concepts)
    threshold = await db.get_adaptive_threshold()
    # известные концепты берут свой вес; неизвестные делят порог между собой
    # (+эпсилон), чтобы пост целиком из новых тем всегда чуть перебивал порог
    # и не тонул со временем
    unknown_count = sum(1 for c in concepts if c not in weights)
    default_w = (
        round(threshold / unknown_count, 2) + config.UNKNOWN_CONCEPT_EPS
        if unknown_count else 0.0
    )
    local_score = sum(weights.get(c, default_w) for c in concepts)

    # дедупликация по Jaccard
    recent_concepts = await db.get_recent_forwarded_concepts(hours=config.DUPLICATE_HOURS)
    current_set = set(concepts)
    same_story = False
    if current_set:
        for prev in recent_concepts:
            prev_set = set(prev)
            if not prev_set:
                continue
            jaccard = len(current_set & prev_set) / len(current_set | prev_set)
            if jaccard >= config.DUPLICATE_JACCARD_THRESHOLD:
                same_story = True
                logger.info("Duplicate detected: jaccard=%.2f concepts=%s", jaccard, concepts)
                break

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

    concept_parts = []
    for c in concepts:
        w = weights.get(c, default_w)
        concept_parts.append(f"{html.escape(c)} {w:+.2f}")
    score_line = "  ·  ".join(concept_parts)
    vote_text = f"{score_line} Σ <b>{local_score:.2f}</b>/{threshold:.2f}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍", callback_data=f"like:{msg_id}"),
            InlineKeyboardButton("👎", callback_data=f"dislike:{msg_id}"),
        ]
    ])

    bot_message_id = None
    try:
        await tg_client.forward_messages(
            config.NOTIFY_CHAT_ID,
            event.message.id,
            event.chat_id,
        )
        logger.info("Forwarded OK: channel=%s", channel_name)
    except Exception as e:
        logger.error("Forward failed: %s", e)
        return

    try:
        sent = await bot.send_message(
            chat_id=config.NOTIFY_CHAT_ID,
            text=vote_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        bot_message_id = sent.message_id
        logger.info("Голосовалка sent: msg_id=%s", bot_message_id)
    except Exception as e:
        logger.error("Send keyboard failed: %s", e)

    # держим notify-чат непрочитанным (свой форвард гасит бейдж) —
    # флаг серверный, синхронизируется на все клиенты
    try:
        peer = await tg_client.get_input_entity(config.NOTIFY_CHAT_ID)
        await tg_client(MarkDialogUnreadRequest(peer=InputDialogPeer(peer=peer), unread=True))
    except Exception as e:
        logger.warning("Mark unread failed: %s", e)

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.mark_forwarded(msg_id, bot_message_id, is_drift)
    await db.increment_forwarded(channel_id)
