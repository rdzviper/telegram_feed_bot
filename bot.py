import asyncio
import logging
import os

from telethon import TelegramClient, events
from telegram import Bot
from telegram.ext import Application
from telegram.request import HTTPXRequest

import config
import database as db
import pipeline
from pipeline import process_message
from handlers import register_handlers, set_bot_commands
from plugins import load_plugins

logging.basicConfig(
    format="%(asctime)s %(name)-20s %(levelname)s %(message)s",
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
logger = logging.getLogger(__name__)


async def main():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    await db.init_db()
    logger.info("DB ready")
    await pipeline.refresh_threshold()

    tg_client = TelegramClient(config.SESSION_PATH, config.TG_API_ID, config.TG_API_HASH)

    _request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60,
        write_timeout=60,   # загрузка медиа может быть долгой
        connect_timeout=15,
        pool_timeout=30,
    )
    bot = Bot(token=config.BOT_TOKEN, request=_request)
    pipeline.set_clients(tg_client, bot)

    queue: asyncio.Queue = asyncio.Queue()
    # буфер альбомов: grouped_id -> {"event": праймари, "ids": [msg_id...], "timer": handle}
    albums: dict[int, dict] = {}

    # Плагины сырого роутинга. channel_id -> [plugin...] заполняется после
    # старта userbot-клиента (setup резолвит usernames в id).
    plugins = load_plugins()
    plugin_channel_map: dict[int, list] = {}

    async def pipeline_worker():
        while True:
            event, media_ids = await queue.get()
            try:
                await process_message(event, media_ids)
            except Exception as e:
                logger.error("Pipeline error: %s", e, exc_info=True)
            finally:
                queue.task_done()

    asyncio.ensure_future(pipeline_worker())

    def flush_album(gid: int):
        # дебаунс истёк — альбом собран, отправляем в пайплайн одним постом
        slot = albums.pop(gid, None)
        if slot:
            queue.put_nowait((slot["event"], sorted(slot["ids"])))

    @tg_client.on(events.NewMessage())
    async def on_new_message(event):
        if config.DEBUG_ALL_MESSAGES:
            logger.info(
                "[DEBUG] chat_id=%s post=%s is_channel=%s megagroup=%s grouped=%s text=%.60r",
                event.chat_id,
                event.message.post,
                event.is_channel,
                getattr(event.chat, "megagroup", None),
                event.message.grouped_id,
                event.message.text or "",
            )
        # Плагины сырого роутинга — ДО фильтра по типу: источник может быть
        # чатом/мегагруппой (post=False), а не broadcast-каналом. Матч по
        # chat_id, обход основного пайплайна.
        ph = plugin_channel_map.get(event.chat_id)
        if ph:
            for p in ph:
                try:
                    await p.handle(event)
                except Exception as e:
                    logger.error("Plugin %s error: %s", p.name, e, exc_info=True)
            return

        if not event.message.post:
            return
        if event.chat_id == config.NOTIFY_CHAT_ID:
            return

        gid = event.message.grouped_id
        if gid is None:
            await queue.put((event, [event.message.id]))
            return

        # альбом: копим сообщения группы, форвардим одним блоком после дебаунса
        slot = albums.get(gid)
        if slot is None:
            slot = albums[gid] = {"event": event, "ids": [], "timer": None}
        slot["ids"].append(event.message.id)
        # праймари = сообщение с подписью (для анализа/скоринга); если ни у кого
        # подписи нет — останется первое (медиа-only ветка в пайплайне)
        if (event.message.text or "").strip():
            slot["event"] = event
        if slot["timer"] is not None:
            slot["timer"].cancel()
        slot["timer"] = asyncio.get_event_loop().call_later(
            config.ALBUM_DEBOUNCE_SEC, flush_album, gid
        )

    application = Application.builder().token(config.BOT_TOKEN).build()
    register_handlers(application)

    await application.initialize()
    await set_bot_commands(application)
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot polling started")

    await tg_client.start()
    logger.info("Userbot connected")

    # Резолвим каналы плагинов и строим карту диспатча (до входящего трафика).
    for p in plugins:
        await p.setup(tg_client)
        for cid in p.channel_ids:
            plugin_channel_map.setdefault(cid, []).append(p)
    if plugins:
        logger.info("Plugins active: %s",
                    {p.name: sorted(p.channel_ids) for p in plugins})

    logger.info("Syncing dialogs...")
    dialogs = await tg_client.get_dialogs()
    logger.info("Dialogs synced")

    # чистим статистику каналов, от которых пользователь отписался
    active_ids = {d.id for d in dialogs}
    pruned = await db.prune_channels(active_ids)
    if pruned:
        logger.info("Pruned %d unsubscribed channels: %s", len(pruned), pruned)
    else:
        logger.info("No stale channels to prune")

    await bot.send_message(config.NOTIFY_CHAT_ID, "✅ Бот запущен, лайк +1, дизлайк -0.3")

    # On start: if a post is already queued — show it; otherwise put up the
    # empty plaque. Later arrivals auto-release while the feed is idle.
    await pipeline.deliver_next()

    try:
        await tg_client.run_until_disconnected()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
