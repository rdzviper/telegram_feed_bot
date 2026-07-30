import asyncio
import html
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

# --- клиенты, общие для process_message (вызывается из очереди bot.py)
# и deliver_batch (вызывается также из handlers.py по кнопке «Промотать») ---
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


async def process_message(event, media_ids: list[int]):
    """event — праймари-сообщение поста (с подписью, если есть); media_ids — все
    id для форварда (>1 для альбома). Анализ идёт по подписи праймари-сообщения."""
    global _drift_counter, _at_cap

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

    # Предохранитель: очередь показа не резиновая. Если пользователь давно не
    # жал «Промотать», не копим до бесконечности — дропаем новые посты.
    # Разгружается само по мере пролистывания. Лог edge-triggered, не спамим.
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

    logger.info("Queued: channel=%s local_score=%.1f threshold=%.1f drift=%s media_only=%s items=%d",
                channel_name, local_score, threshold, is_drift, media_only, len(media_ids))

    _forwarded_times.append(datetime.now(timezone.utc).timestamp())
    await db.enqueue_message(msg_id, media_ids, threshold, is_drift)
    await _maybe_auto_release()


async def _maybe_auto_release():
    """Новый пост встал в очередь. Если лента сейчас простаивает (якорь —
    заглушка «Больше постов нет» или его вообще ещё не было), это единственный
    момент, когда доставка идёт без нажатия кнопки — иначе бы кнопку неоткуда
    было взять. Если якорь уже висит на посте (юзер ещё не долистал предыдущую
    пачку), просто копим — новый показ ждёт нажатия «Промотать»."""
    anchor_kind = await db.get_setting("anchor_kind")
    if anchor_kind in (None, "", "placeholder"):
        await deliver_batch()


def _row_media_ids(row: dict) -> list[int]:
    raw = row.get("media_ids")
    return json.loads(raw) if raw else [row["orig_message_id"]]


async def _strip_pager(anchor_kind: str, anchor_msg_id: int, anchor_bot_msg_id: int):
    """Снимает строку пейджера с текущего якоря, оставляя остальное содержимое
    (голоса по концептам для текстового поста) как есть."""
    try:
        if anchor_kind == "text":
            concepts = await db.get_message_concepts(anchor_msg_id)
            votes = await db.get_topic_votes(anchor_msg_id)
            markup = keyboards.companion_keyboard(anchor_msg_id, concepts, votes, with_pager=False)
        elif anchor_kind == "media":
            markup = None
        else:
            return
        await _bot.edit_message_reply_markup(
            chat_id=config.NOTIFY_CHAT_ID, message_id=anchor_bot_msg_id, reply_markup=markup
        )
    except Exception as e:
        logger.warning("Strip pager failed: %s", e)


async def _deliver_one(row: dict, with_pager: bool) -> dict | None:
    """Форвардит один пост из очереди + шлёт компаньон (кроме медиа-без-подписи
    не в хвосте пачки — там компаньон не нужен). Возвращает {"kind",
    "bot_message_id", "msg_id"} — вызывающий использует результат последнего
    элемента пачки, чтобы установить новый якорь; None, если ставить нечего
    (форвард не удался, либо компаньон не отправлялся)."""
    msg_id = row["id"]
    text = row["text"] or ""
    media_only = not text.strip()
    concepts = json.loads(row["concepts"]) if row["concepts"] else []
    is_drift = bool(row["is_drift"])
    local_score = row["local_score"] or 0.0
    threshold = row["threshold_at_score"] or 0.0

    try:
        await _tg_client.forward_messages(config.NOTIFY_CHAT_ID, _row_media_ids(row), row["channel_id"])
        logger.info("Forwarded OK: channel=%s msg_id=%s", row["channel_name"], msg_id)
    except Exception as e:
        logger.error("Forward failed for queued msg_id=%s: %s", msg_id, e)
        await db.mark_delivery_failed(msg_id)
        return None

    bot_message_id = None
    kind = "media" if media_only else "text"

    # медиа без подписи вне хвоста пачки не оценить и незачем пейджить —
    # компаньон вообще не нужен (как и раньше, тихий форвард); только последний
    # элемент пачки получает компаньон-с-кнопкой, чтобы было на чём висеть якорю
    if media_only and not with_pager:
        await db.mark_delivered(msg_id, None)
        await db.increment_forwarded(row["channel_id"])
        return None

    try:
        if media_only:
            sent = await _bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text="📎",
                reply_markup=keyboards.media_keyboard(msg_id, with_pager),
            )
        else:
            votes = await db.get_topic_votes(msg_id)
            prefix = "🔍 " if is_drift else ""
            vote_text = f"{prefix}Σ <b>{local_score:.2f}</b>/{threshold:.2f}"
            sent = await _bot.send_message(
                chat_id=config.NOTIFY_CHAT_ID,
                text=vote_text,
                parse_mode="HTML",
                reply_markup=keyboards.companion_keyboard(msg_id, concepts, votes, with_pager),
            )
        bot_message_id = sent.message_id
    except Exception as e:
        logger.error("Send companion failed for msg_id=%s: %s", msg_id, e)

    await db.mark_delivered(msg_id, bot_message_id)
    await db.increment_forwarded(row["channel_id"])
    return {"kind": kind, "bot_message_id": bot_message_id, "msg_id": msg_id}


async def deliver_batch():
    """Отдаёт следующие до PAGE_SIZE постов из очереди. Вызывается либо из
    _maybe_auto_release (лента простаивала), либо из handlers.py по нажатию
    кнопки пейджера. Сериализовано локом — обе точки входа асинхронны и могут
    пересечься по времени."""
    async with _delivery_lock:
        batch = await db.get_queued_batch(config.PAGE_SIZE)
        anchor_kind = await db.get_setting("anchor_kind") or ""
        anchor_bot_msg_id_raw = await db.get_setting("anchor_bot_msg_id")
        anchor_msg_id_raw = await db.get_setting("anchor_msg_id")

        if not batch:
            if anchor_kind in ("text", "media") and anchor_bot_msg_id_raw and anchor_msg_id_raw:
                await _strip_pager(anchor_kind, int(anchor_msg_id_raw), int(anchor_bot_msg_id_raw))
            if anchor_kind != "placeholder":
                sent = await _bot.send_message(config.NOTIFY_CHAT_ID, "📭 Больше постов нет")
                await db.set_setting("anchor_kind", "placeholder")
                await db.set_setting("anchor_bot_msg_id", str(sent.message_id))
                await db.set_setting("anchor_msg_id", "")
            return

        if anchor_kind == "placeholder" and anchor_bot_msg_id_raw:
            try:
                await _bot.delete_message(config.NOTIFY_CHAT_ID, int(anchor_bot_msg_id_raw))
            except Exception as e:
                logger.warning("Delete placeholder failed: %s", e)
        elif anchor_kind in ("text", "media") and anchor_bot_msg_id_raw and anchor_msg_id_raw:
            await _strip_pager(anchor_kind, int(anchor_msg_id_raw), int(anchor_bot_msg_id_raw))

        last = None
        for i, row in enumerate(batch):
            result = await _deliver_one(row, with_pager=(i == len(batch) - 1))
            if result:
                last = result

        if last and last["bot_message_id"] is not None:
            await db.set_setting("anchor_kind", last["kind"])
            await db.set_setting("anchor_bot_msg_id", str(last["bot_message_id"]))
            await db.set_setting("anchor_msg_id", str(last["msg_id"]))
        elif last:
            # компаньон последнего поста пачки не отправился (сбой Telegram API) —
            # вешать пейджер не на что; откатываемся в «пусто», следующий новый
            # пост подхватит через _maybe_auto_release, ручной кнопки до тех пор нет
            logger.warning("Last batch item msg_id=%s has no companion, clearing anchor", last["msg_id"])
            await db.set_setting("anchor_kind", "")
            await db.set_setting("anchor_bot_msg_id", "")
            await db.set_setting("anchor_msg_id", "")

        # держим notify-чат непрочитанным (свой форвард гасит бейдж) —
        # флаг серверный, синхронизируется на все клиенты
        try:
            peer = await _tg_client.get_input_entity(config.NOTIFY_CHAT_ID)
            await _tg_client(MarkDialogUnreadRequest(peer=InputDialogPeer(peer=peer), unread=True))
        except Exception as e:
            logger.warning("Mark unread failed: %s", e)
