import asyncio
import json
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
from telegram import Bot

import config
import database as db
import analyzer
import embeddings
import keyboards

logger = logging.getLogger(__name__)

# Shared clients for process_message (queue worker) and deliver_next
# (vote/skip handlers + idle auto-release).
_tg_client: TelegramClient | None = None
_bot: Bot | None = None
_delivery_lock = asyncio.Lock()
_at_cap = False


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


async def process_message(event, media_ids: list[int]):
    """event — primary post message (with caption if any); media_ids — all ids
    to forward (>1 for albums). Analysis runs on the primary caption."""
    global _drift_counter, _at_cap

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

    # Safety valve: the display queue is not infinite. If the user has not
    # voted/skipped for a long time, drop new posts instead of growing forever.
    # Self-drains as the user advances. Edge-triggered log, no spam.
    queued_now = await db.count_queued()
    if queued_now >= config.QUEUE_CAP:
        if not _at_cap:
            _at_cap = True
            logger.warning("Queue cap reached (%d/%d), dropping new messages until it drains",
                            queued_now, config.QUEUE_CAP)
        return
    elif _at_cap:
        _at_cap = False
        logger.info("Queue below cap again (%d/%d), resuming", queued_now, config.QUEUE_CAP)

    # Native Telegram Giveaway (premium) is a special media type, not text:
    # prize/participants/date live on the media object, DeepSeek never sees them.
    # Always subscriber-bait → drop deterministically before analysis.
    if isinstance(event.message.media, (MessageMediaGiveaway, MessageMediaGiveawayResults)):
        logger.info("channel=%s filtered: telegram giveaway", channel_name)
        return

    # Media without caption: nothing to analyze (no concepts/summary/dedup/score).
    # Still enqueue as content, but companion has no concept rows to vote on.
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

    logger.info("Queued: channel=%s local_score=%.1f threshold=%.1f drift=%s media_only=%s items=%d",
                channel_name, local_score, threshold, is_drift, media_only, len(media_ids))

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.enqueue_message(msg_id, media_ids, threshold, is_drift)
    await _maybe_auto_release()


async def _maybe_auto_release():
    """A new post just entered the queue. Deliver it only while the feed is
    idle: no current post on screen (empty/missing anchor, or empty plaque).
    If a post is already shown and waiting for like/skip/dislike — just keep
    accumulating; the next press advances."""
    anchor_kind = await db.get_setting("anchor_kind")
    if anchor_kind in (None, "", "placeholder"):
        await deliver_next()


def _row_media_ids(row: dict) -> list[int]:
    raw = row.get("media_ids")
    return json.loads(raw) if raw else [row["orig_message_id"]]


async def _delete_placeholder(anchor_bot_msg_id_raw: str | None):
    if not anchor_bot_msg_id_raw:
        return
    try:
        await _bot.delete_message(config.NOTIFY_CHAT_ID, int(anchor_bot_msg_id_raw))
    except Exception as e:
        logger.warning("Delete placeholder failed: %s", e)


async def _show_empty_plaque():
    """Send (or keep) the «no more posts» plaque and pin it as the anchor."""
    anchor_kind = await db.get_setting("anchor_kind") or ""
    if anchor_kind == "placeholder":
        return
    sent = await _bot.send_message(config.NOTIFY_CHAT_ID, "📭 Постов больше нет")
    await db.set_setting("anchor_kind", "placeholder")
    await db.set_setting("anchor_bot_msg_id", str(sent.message_id))
    await db.set_setting("anchor_msg_id", "")


async def _deliver_one(row: dict) -> dict | None:
    """Forward one queued post + send companion with 👍/⏭/👎.
    Returns {"kind", "bot_message_id", "msg_id"} for the new anchor, or None
    if the forward failed."""
    msg_id = row["id"]
    text = row["text"] or ""
    media_only = not text.strip()
    is_drift = bool(row["is_drift"])
    local_score = row["local_score"] or 0.0
    threshold = row["threshold_at_score"] or 0.0

    try:
        await _tg_client.forward_messages(
            config.NOTIFY_CHAT_ID, _row_media_ids(row), row["channel_id"]
        )
        logger.info("Forwarded OK: channel=%s msg_id=%s", row["channel_name"], msg_id)
    except Exception as e:
        logger.error("Forward failed for queued msg_id=%s: %s", msg_id, e)
        await db.mark_delivery_failed(msg_id)
        return None

    kind = "media" if media_only else "text"
    bot_message_id = None

    try:
        if media_only:
            sent = await _bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text="📎",
                reply_markup=keyboards.action_keyboard(msg_id),
            )
        else:
            prefix = "🔍 " if is_drift else ""
            vote_text = f"{prefix}Σ <b>{local_score:.2f}</b>/{threshold:.2f}"
            sent = await _bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text=vote_text,
                parse_mode="HTML",
                reply_markup=keyboards.action_keyboard(msg_id),
            )
        bot_message_id = sent.message_id
    except Exception as e:
        logger.error("Send companion failed for msg_id=%s: %s", msg_id, e)

    await db.mark_delivered(msg_id, bot_message_id)
    await db.increment_forwarded(row["channel_id"])

    if bot_message_id is None:
        # Companion failed — nothing to hang buttons on. Leave anchor empty so
        # the next auto-release / next button still has a path forward.
        return None
    return {"kind": kind, "bot_message_id": bot_message_id, "msg_id": msg_id}


async def deliver_next():
    """Show the next single post from the queue, or the empty plaque.
    Called from:
      - bot startup (empty feed → plaque)
      - _maybe_auto_release (idle feed + new queued post)
      - handlers after like / skip / dislike
    Serialized by lock — both entry points are async and can race."""
    async with _delivery_lock:
        batch = await db.get_queued_batch(1)
        anchor_kind = await db.get_setting("anchor_kind") or ""
        anchor_bot_msg_id_raw = await db.get_setting("anchor_bot_msg_id")

        if not batch:
            if anchor_kind in ("text", "media") and anchor_bot_msg_id_raw:
                # Previous post already finalized its keyboard in the handler;
                # just move the anchor to the empty plaque.
                pass
            await _show_empty_plaque()
            return

        if anchor_kind == "placeholder":
            await _delete_placeholder(anchor_bot_msg_id_raw)
        # Previous post keeps its final mark (✅/❌/⏭) from the handler —
        # do not strip it when advancing.

        result = await _deliver_one(batch[0])
        if result:
            await db.set_setting("anchor_kind", result["kind"])
            await db.set_setting("anchor_bot_msg_id", str(result["bot_message_id"]))
            await db.set_setting("anchor_msg_id", str(result["msg_id"]))
        else:
            # Forward/companion failed. If more posts remain, try the next one
            # immediately; otherwise fall back to empty plaque.
            more = await db.count_queued()
            if more > 0:
                logger.warning("Delivery failed, retrying next queued post (%d left)", more)
                # Drop lock briefly is not possible here; recurse under same lock
                # via a local loop instead of nested deliver_next().
                while True:
                    nxt = await db.get_queued_batch(1)
                    if not nxt:
                        await _show_empty_plaque()
                        break
                    result = await _deliver_one(nxt[0])
                    if result:
                        await db.set_setting("anchor_kind", result["kind"])
                        await db.set_setting("anchor_bot_msg_id", str(result["bot_message_id"]))
                        await db.set_setting("anchor_msg_id", str(result["msg_id"]))
                        break
            else:
                await _show_empty_plaque()

        # Disabled: MarkDialogUnread on Desktop Telegram kicks the user out of
        # the open chat ("Select a chat to start messaging") after each vote/skip.
        # try:
        #     peer = await _tg_client.get_input_entity(config.NOTIFY_CHAT_ID)
        #     await _tg_client(MarkDialogUnreadRequest(peer=InputDialogPeer(peer=peer), unread=True))
        # except Exception as e:
        #     logger.warning("Mark unread failed: %s", e)


# Back-compat alias if anything still imports the old name.
deliver_batch = deliver_next
