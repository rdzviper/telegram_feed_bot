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
from handlers import register_handlers

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

    queue: asyncio.Queue = asyncio.Queue()

    async def pipeline_worker():
        while True:
            event = await queue.get()
            try:
                await process_message(event, tg_client, bot)
            except Exception as e:
                logger.error("Pipeline error: %s", e, exc_info=True)
            finally:
                queue.task_done()

    asyncio.ensure_future(pipeline_worker())

    @tg_client.on(events.NewMessage())
    async def on_new_message(event):
        if config.DEBUG_ALL_MESSAGES:
            logger.info(
                "[DEBUG] chat_id=%s post=%s is_channel=%s megagroup=%s text=%.60r",
                event.chat_id,
                event.message.post,
                event.is_channel,
                getattr(event.chat, "megagroup", None),
                event.message.text or "",
            )
        if not event.message.post:
            return
        if event.chat_id == config.NOTIFY_CHAT_ID:
            return
        await queue.put(event)

    application = Application.builder().token(config.BOT_TOKEN).build()
    register_handlers(application)

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot polling started")

    await tg_client.start()
    logger.info("Userbot connected")

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

    try:
        await tg_client.run_until_disconnected()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
