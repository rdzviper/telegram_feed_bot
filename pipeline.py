import logging
import re
from collections import deque
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaGiveaway,
    MessageMediaGiveawayResults,
)
from telegram import Bot

import config
import database as db
import analyzer
import embeddings
import media_hash

logger = logging.getLogger(__name__)

# Shared clients for process_message (queue worker) and _deliver (HOP1/HOP2 forward).
_tg_client: TelegramClient | None = None
_bot: Bot | None = None


def set_clients(tg_client: TelegramClient, bot: Bot):
    global _tg_client, _bot
    _tg_client, _bot = tg_client, bot

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+")
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _norm_tokens(t: str) -> set[str]:
    t = _URL_RE.sub(" ", t.lower().replace("ё", "е"))
    return {w for w in _NONWORD_RE.sub(" ", t).split() if len(w) > 2}


def _is_text_duplicate(text: str, recent_texts: list[str], threshold: float = 0.9) -> bool:
    """Cheap layer 0: near-verbatim reposts via token overlap."""
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

# Threshold is cached: refresh on start and after each vote, not per message —
# otherwise unrated posts slowly push the threshold up by themselves.
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


async def _hash_album_media(event, media_ids: list[int], channel_id: int) -> list[str]:
    """Хэш превью для каждого media-item поста (альбом = несколько). Основной
    item уже есть в event.message; остальные (только id) подтягиваем."""
    primary_id = event.message.id
    messages = [event.message]
    other_ids = [m for m in media_ids if m != primary_id]
    if other_ids:
        try:
            fetched = await _tg_client.get_messages(channel_id, ids=other_ids)
        except Exception as e:
            logger.warning("Fetch album items failed for channel_id=%s: %s", channel_id, e)
            fetched = []
        if not isinstance(fetched, list):
            fetched = [fetched]
        messages += [m for m in fetched if m is not None]

    hashes = []
    for msg in messages:
        h = await media_hash.hash_message(_tg_client, msg)
        if h:
            hashes.append(h)
    return hashes


async def process_message(event, media_ids: list[int]):
    """event — primary post message (with caption if any); media_ids — all ids
    to forward (>1 for albums). Analysis runs on the primary caption."""
    global _drift_counter

    text = event.message.text or ""
    has_media = bool(event.message.media) or len(media_ids) > 1
    # Completely empty post (no text, no media) — drop.
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

    # Native Telegram Giveaway (premium) is a special media type, not text:
    # prize/participants/date live on the media object, DeepSeek never sees them.
    # Always subscriber-bait → drop deterministically before analysis.
    if isinstance(event.message.media, (MessageMediaGiveaway, MessageMediaGiveawayResults)):
        logger.info("channel=%s filtered: telegram giveaway", channel_name)
        return

    # Media without caption: nothing to analyze (no concepts/summary/dedup/score).
    # Still delivered as content; a reaction on it just has nothing to update.
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

    # Local scoring against the interest weight matrix.
    weights = await db.get_concept_weights(concepts)
    threshold = _threshold  # cache; only changes on voting
    # Known concepts take their weight; unknowns split the threshold between them
    # (+eps) so a fully-new-topic post still slightly beats the threshold and
    # does not sink over time.
    unknown_count = sum(1 for c in concepts if c not in weights)
    default_w = (
        round(threshold / unknown_count, 2) + config.UNKNOWN_CONCEPT_EPS
        if unknown_count else 0.0
    )
    local_score = sum(weights.get(c, default_w) for c in concepts)

    # Dedup: layer 0 (text, verbatim) + layer 1 (cosine summary, meaning).
    # Media-only has neither text nor summary — skip.
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

    # Layer 2: perceptual hash медиа-превью — покрывает то, что текст/cosine
    # слои не видят: медиа без подписи или с сильно разными подписями (media_only
    # или разные каналы переписали текст). Независим от media_only.
    media_hashes: list[str] = []
    if has_media:
        media_hashes = await _hash_album_media(event, media_ids, channel_id)
        if media_hashes and not same_story:
            recent_hashes = await db.get_recent_media_hashes(hours=config.DUPLICATE_HOURS)
            for h in media_hashes:
                for rh in recent_hashes:
                    dist = media_hash.hamming(h, rh)
                    if dist <= config.MEDIA_HASH_THRESHOLD:
                        same_story = True
                        logger.info("Duplicate (media hash, distance=%d ≤ %d): channel=%s",
                                    dist, config.MEDIA_HASH_THRESHOLD, channel_name)
                        break
                if same_story:
                    break

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
        await db.save_message_media_hashes(msg_id, media_hashes)
    except Exception as e:
        logger.error("DB save failed: %s", e, exc_info=True)
        return

    if is_ad:
        logger.info("channel=%s filtered: ad", channel_name)
        return
    if same_story:
        logger.info("channel=%s filtered: duplicate story", channel_name)
        return
    # Drop text noise; media posts still pass (image = content).
    if not informative and not has_media:
        logger.info("channel=%s filtered: non-informative text", channel_name)
        return

    # Media without caption cannot be scored — forward unconditionally.
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

    logger.info("Delivering: channel=%s local_score=%.1f threshold=%.1f drift=%s media_only=%s items=%d",
                channel_name, local_score, threshold, is_drift, media_only, len(media_ids))

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.mark_forwarded(msg_id, media_ids, is_drift, threshold)
    await _deliver(msg_id, media_ids, channel_id, channel_name)


async def _deliver(msg_id: int, media_ids: list[int], channel_id: int, channel_name: str):
    """Infinite feed, no anchor/pacing: HOP1 userbot forward (source → relay
    NOTIFY_CHAT_ID, the only account subscribed to sources) then HOP2 bot
    forward (relay → DM_CHAT_ID, a real incoming message for Alex — no
    self-authored scroll-to-bottom, no MarkDialogUnread focus-loss). Voting
    happens as a native reaction directly on the DM copies; message_deliveries
    maps every dm_message_id (1 for a single post, N for an album) back to
    msg_id uniformly."""
    try:
        fwd = await _tg_client.forward_messages(config.NOTIFY_CHAT_ID, media_ids, channel_id)
    except Exception as e:
        logger.error("HOP1 forward failed for msg_id=%s: %s", msg_id, e)
        return
    relay_ids = [m.id for m in fwd] if isinstance(fwd, list) else [fwd.id]

    # Batch forward (Bot API forwardMessages, plural) in one call — same as
    # Telethon's HOP1 batch — so an album stays grouped in the DM, instead of
    # arriving as N separate messages (singular forward_message per item).
    try:
        sent = await _bot.forward_messages(
            chat_id=config.DM_CHAT_ID, from_chat_id=config.NOTIFY_CHAT_ID, message_ids=relay_ids
        )
        dm_message_ids = [m.message_id for m in sent]
    except Exception as e:
        logger.error("HOP2 forward failed for msg_id=%s: %s", msg_id, e)
        dm_message_ids = []

    if not dm_message_ids:
        logger.error("HOP2 fully failed for msg_id=%s, no DM copies delivered", msg_id)
        return

    await db.save_message_deliveries(dm_message_ids, msg_id)
    await db.increment_forwarded(channel_id)
    logger.info("Delivered: channel=%s msg_id=%s dm_messages=%d",
                channel_name, msg_id, len(dm_message_ids))
