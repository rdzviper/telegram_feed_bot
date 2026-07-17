import html
import logging
import re
from collections import deque
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.functions.messages import MarkDialogUnreadRequest
from telethon.tl.types import (
    InputDialogPeer,
    MessageMediaGiveaway,
    MessageMediaGiveawayResults,
)
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import analyzer
import embeddings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+")
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _norm_tokens(t: str) -> set[str]:
    t = _URL_RE.sub(" ", t.lower().replace("ё", "е"))
    return {w for w in _NONWORD_RE.sub(" ", t).split() if len(w) > 2}


def _is_text_duplicate(text: str, recent_texts: list[str], threshold: float = 0.9) -> bool:
    """Дешёвый слой 0: ловит побайтовые/почти-побайтовые репосты по overlap токенов."""
    cur = _norm_tokens(text)
    if len(cur) < 3:
        return False
    for prev in recent_texts:
        p = _norm_tokens(prev)
        if len(p) >= 3 and len(cur & p) / min(len(cur), len(p)) >= threshold:
            return True
    return False

_forwarded_times: deque = deque()
_drift_counter = 0

# порог кэшируется: пересчитывается только при старте и после голоса,
# а не на каждом сообщении — иначе неоценённые посты самоусиливают порог
_threshold = 0.0


async def refresh_threshold():
    global _threshold
    _threshold = await db.get_adaptive_threshold()
    logger.info("Threshold refreshed: %.3f", _threshold)



def _rate_limit_ok() -> bool:
    now = datetime.now(timezone.utc).timestamp()
    while _forwarded_times and now - _forwarded_times[0] > 3600:
        _forwarded_times.popleft()
    return len(_forwarded_times) < config.RATE_LIMIT_PER_HOUR


async def process_message(event, media_ids: list[int], tg_client: TelegramClient, bot: Bot):
    """event — праймари-сообщение поста (с подписью, если есть); media_ids — все
    id для форварда (>1 для альбома). Анализ идёт по подписи праймари-сообщения."""
    global _drift_counter

    text = event.message.text or ""
    has_media = bool(event.message.media) or len(media_ids) > 1
    # Пост совсем без текста И без медиа — мусор, режем.
    if not text.strip() and not has_media:
        logger.debug("Empty message (no text, no media), skipping")
        return

    channel_id = event.chat_id
    channel_name = (
        event.chat.title if event.chat and hasattr(event.chat, "title") else str(channel_id)
    )

    logger.info("Incoming: channel=%s media=%s items=%d text=%.80r",
                channel_name, has_media, len(media_ids), text)

    await db.upsert_channel(channel_id, channel_name)
    await db.increment_seen(channel_id)

    if not await db.is_channel_active(channel_id):
        logger.debug("channel=%s is disabled, skipping", channel_name)
        return

    # Нативный Telegram Giveaway (премиум-фича) — спец. тип медиа, а не текст:
    # «приз/участники/дата» лежат в объекте media, DeepSeek их не видит. Это всегда
    # накрутка подписчиков → режем детерминированно, до анализа.
    if isinstance(event.message.media, (MessageMediaGiveaway, MessageMediaGiveawayResults)):
        logger.info("channel=%s filtered: telegram giveaway", channel_name)
        return

    # Медиа без подписи: анализировать нечего (нет текста → нет концептов, summary,
    # дедупа, скоринга). Форвардим как контент, минуя DeepSeek и порог; при этом
    # companion-сообщение (голосовалку) НЕ шлём — оценивать нечего.
    media_only = not text.strip()
    if media_only:
        logger.debug("Media-only post (no caption): channel=%s, skipping analysis", channel_name)
        result = {"concepts": [], "is_ad": False, "informative": True, "summary": ""}
    else:
        logger.debug("Sending to DeepSeek: channel=%s", channel_name)
        result = await analyzer.analyze(text)
    concepts = result["concepts"]
    is_ad = result["is_ad"]
    informative = result["informative"]

    # локальный scoring по матрице весов
    weights = await db.get_concept_weights(concepts)
    threshold = _threshold  # кэш; меняется только при голосовании
    # известные концепты берут свой вес; неизвестные делят порог между собой
    # (+эпсилон), чтобы пост целиком из новых тем всегда чуть перебивал порог
    # и не тонул со временем
    unknown_count = sum(1 for c in concepts if c not in weights)
    default_w = (
        round(threshold / unknown_count, 2) + config.UNKNOWN_CONCEPT_EPS
        if unknown_count else 0.0
    )
    local_score = sum(weights.get(c, default_w) for c in concepts)

    # --- Дедуп: слой 0 (текст, verbatim) + слой 1 (cosine саммари, смысл) ---
    # Для медиа без подписи дедуп невозможен (нет ни текста, ни summary) — пропускаем.
    summary = result.get("summary", "")
    same_story = False
    embedding = None

    if not media_only:
        recent_texts = await db.get_recent_forwarded_texts(hours=config.DUPLICATE_HOURS)
        if _is_text_duplicate(text, recent_texts):
            same_story = True
            logger.info("Duplicate (text pre-filter): channel=%s", channel_name)

        embedding = await embeddings.embed(summary)
        if not same_story and embedding is not None:
            best = 0.0
            for blob in await db.get_recent_forwarded_embeddings(hours=config.DUPLICATE_HOURS):
                cos = float(embedding @ embeddings.from_blob(blob))
                if cos > best:
                    best = cos
            if best >= config.DUPLICATE_COSINE_THRESHOLD:
                same_story = True
                logger.info("Duplicate (cosine=%.3f ≥ %.2f): channel=%s summary=%.80r",
                            best, config.DUPLICATE_COSINE_THRESHOLD, channel_name, summary)

    logger.info(
        "Analyzed: channel=%s concepts=%s local_score=%.1f threshold=%.1f is_ad=%s same_story=%s informative=%s media=%s",
        channel_name, concepts, local_score, threshold, is_ad, same_story, informative, has_media,
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
        await db.save_message_semantic(
            msg_id, summary, embeddings.to_blob(embedding) if embedding is not None else None
        )
    except Exception as e:
        logger.error("DB save failed: %s", e, exc_info=True)
        return

    if is_ad:
        logger.info("channel=%s filtered: ad", channel_name)
        return
    if same_story:
        logger.info("channel=%s filtered: duplicate story", channel_name)
        return
    # текстовый шум режем; пост с медиа пропускаем (картинка = контент)
    if not informative and not has_media:
        logger.info("channel=%s filtered: non-informative text", channel_name)
        return

    # медиа без подписи скорить нечем — форвардим безусловно (медиа = контент)
    should_forward = media_only or local_score >= threshold
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

    logger.info("Forwarding: channel=%s local_score=%.1f threshold=%.1f drift=%s media_only=%s items=%d",
                channel_name, local_score, threshold, is_drift, media_only, len(media_ids))

    bot_message_id = None
    try:
        # forward_messages со списком id отдаёт альбом одним блоком
        await tg_client.forward_messages(
            config.NOTIFY_CHAT_ID,
            media_ids,
            event.chat_id,
        )
        logger.info("Forwarded OK: channel=%s (%d msg)", channel_name, len(media_ids))
    except Exception as e:
        logger.error("Forward failed: %s", e)
        return

    # companion-голосовалку шлём только если есть что оценивать (есть подпись).
    # Для медиа без подписи ничего не шлём — сообщение не нужно.
    if not media_only:
        prefix = "🔍 " if is_drift else ""
        vote_text = f"{prefix}Σ <b>{local_score:.2f}</b>/{threshold:.2f}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗳 Оценить", callback_data=f"rate:{msg_id}")]]
        )
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
