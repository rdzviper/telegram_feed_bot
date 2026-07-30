"""Плагин finance: сырой роутинг финансовых каналов в отдельную БД.

Сообщения (только текст, включая подписи под медиа) из перечисленных каналов
складываются как есть в собственный SQLite-файл. Никакого анализа/скоринга.

Контракт с потребителем (агент finance ходит по SSH на VPS):
    таблица messages(id, source_channel, message_id, text, created_at)
    забор + очистка одной транзакцией, потребитель владеет удалением:
        BEGIN IMMEDIATE;
        SELECT id, source_channel, text, created_at FROM messages ORDER BY id;
        DELETE FROM messages WHERE id <= <max_взятый_id>;
        COMMIT;
Идемпотентность на (source_channel, message_id) — рестарт бота не задваивает.
"""
import logging
import os

import aiosqlite

import config
from plugins.base import FeedPlugin

logger = logging.getLogger(__name__)

FINANCE_DB_PATH = os.environ.get(
    "FINANCE_FEED_DB", os.path.join(config.DATA_DIR, "finance_feed.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_channel, message_id)
);
"""


class FinancePlugin(FeedPlugin):
    name = "finance"
    source_channels = ["stavkivklad", "hranidengi", "blogbankir", "invest_banks"]

    async def init_storage(self):
        async with aiosqlite.connect(FINANCE_DB_PATH) as dbc:
            await dbc.execute("PRAGMA journal_mode=WAL")
            await dbc.executescript(_SCHEMA)
            await dbc.commit()
        logger.info("finance feed db ready: %s", FINANCE_DB_PATH)

    async def handle(self, event):
        text = event.message.text or ""
        if not text.strip():
            return  # только текст: чистое медиа без подписи не берём
        chat = await event.get_chat()
        source = getattr(chat, "username", None) or str(event.chat_id)
        async with aiosqlite.connect(FINANCE_DB_PATH) as dbc:
            await dbc.execute(
                "INSERT OR IGNORE INTO messages (source_channel, message_id, text) "
                "VALUES (?, ?, ?)",
                (source, event.message.id, text),
            )
            await dbc.commit()
        logger.info("finance: stored %s#%s (%d chars)", source, event.message.id, len(text))


PLUGIN = FinancePlugin()
