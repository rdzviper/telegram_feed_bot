import json
import aiosqlite
import logging
from datetime import datetime, timezone, timedelta
import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    orig_message_id INTEGER NOT NULL,
    bot_message_id INTEGER,
    text TEXT NOT NULL,
    date TEXT NOT NULL,
    score INTEGER,
    is_ad INTEGER DEFAULT 0,
    same_story INTEGER DEFAULT 0,
    forwarded INTEGER DEFAULT 0,
    is_drift INTEGER DEFAULT 0,
    user_choice TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY,
    channel_name TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    seen INTEGER DEFAULT 0,
    forwarded INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    dislikes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interests (
    topic TEXT PRIMARY KEY,
    weight REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS topic_votes (
    msg_id INTEGER NOT NULL,
    topic_idx INTEGER NOT NULL,
    choice TEXT NOT NULL,
    PRIMARY KEY (msg_id, topic_idx)
);

-- Infinite-feed delivery (2026-08-05): a post can occupy several DM messages
-- (album = one Telegram message per media item). Maps each back to its post,
-- uniformly for single messages (one row) and albums (one row per item).
CREATE TABLE IF NOT EXISTS message_deliveries (
    dm_message_id INTEGER PRIMARY KEY,
    msg_id INTEGER NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN concepts TEXT",
    "ALTER TABLE messages ADD COLUMN local_score REAL",
    "ALTER TABLE messages ADD COLUMN summary TEXT",
    "ALTER TABLE messages ADD COLUMN embedding BLOB",
    # Очередь доставки (pull-модель, см. tests/dedup_cases.md-соседние доки):
    # 'queued' — проанализирован и принят, ждёт показа; 'delivered' — показан
    # (компаньон отправлен); старые строки (до миграции) остаются 'delivered'
    # по дефолту — они уже были доставлены в старой модели.
    "ALTER TABLE messages ADD COLUMN delivery_state TEXT DEFAULT 'delivered'",
    "ALTER TABLE messages ADD COLUMN media_ids TEXT",
    "ALTER TABLE messages ADD COLUMN threshold_at_score REAL",
]


async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(_SCHEMA)
        async with db.execute("PRAGMA table_info(messages)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for sql in _MIGRATIONS:
            col = sql.split("ADD COLUMN ")[1].split()[0]
            if col not in existing:
                await db.execute(sql)
        await db.commit()


async def upsert_channel(channel_id: int, channel_name: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)",
            (channel_id, channel_name),
        )
        await db.execute(
            "UPDATE channels SET channel_name = ? WHERE channel_id = ?",
            (channel_name, channel_id),
        )
        await db.commit()


async def prune_channels(active_ids: set[int]) -> list[str]:
    """Удаляет из channels все каналы, которых нет в active_ids (отписки).
    Возвращает имена удалённых."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT channel_id, channel_name FROM channels") as cur:
            rows = await cur.fetchall()
        stale = [(cid, name) for cid, name in rows if cid not in active_ids]
        for cid, _ in stale:
            await db.execute("DELETE FROM channels WHERE channel_id = ?", (cid,))
        await db.commit()
        return [name for _, name in stale]


async def is_channel_active(channel_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT active FROM channels WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            return row is None or bool(row[0])


async def increment_seen(channel_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE channels SET seen = seen + 1 WHERE channel_id = ?", (channel_id,)
        )
        await db.commit()


async def increment_forwarded(channel_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE channels SET forwarded = forwarded + 1 WHERE channel_id = ?", (channel_id,)
        )
        await db.commit()


async def save_message(
    channel_id: int,
    channel_name: str,
    orig_message_id: int,
    text: str,
    date: str,
    score: int,
    is_ad: bool,
    same_story: bool,
) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO messages
               (channel_id, channel_name, orig_message_id, text, date, score, is_ad, same_story)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, channel_name, orig_message_id, text, date, score, int(is_ad), int(same_story)),
        )
        await db.commit()
        return cur.lastrowid


async def save_message_concepts(msg_id: int, concepts: list[str], local_score: float):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET concepts = ?, local_score = ? WHERE id = ?",
            (json.dumps(concepts, ensure_ascii=False), local_score, msg_id),
        )
        await db.commit()


async def get_message_text(msg_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT text FROM messages WHERE id = ?", (msg_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_message_concepts(msg_id: int) -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT concepts FROM messages WHERE id = ?", (msg_id,)) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return []


async def mark_forwarded(msg_id: int, media_ids: list[int], is_drift: bool, threshold: float):
    """Пост принят к показу и уходит в доставку немедленно (бесконечная лента,
    без очереди/anchor): forwarded=1 сразу, чтобы участвовал в дедупе следующих
    входящих."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """UPDATE messages
               SET forwarded = 1, is_drift = ?, media_ids = ?, threshold_at_score = ?
               WHERE id = ?""",
            (int(is_drift), json.dumps(media_ids), threshold, msg_id),
        )
        await db.commit()


async def save_message_deliveries(dm_message_ids: list[int], msg_id: int):
    """Мапит все DM-сообщения поста (1 для одиночного, N для альбома) на его
    внутренний msg_id — резолв реакции на любое из них даёт один и тот же пост."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO message_deliveries (dm_message_id, msg_id) VALUES (?, ?)",
            [(dm_id, msg_id) for dm_id in dm_message_ids],
        )
        await db.commit()


async def resolve_msg_id(dm_message_id: int) -> int | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT msg_id FROM message_deliveries WHERE dm_message_id = ?", (dm_message_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def save_message_semantic(msg_id: int, summary: str, embedding: bytes | None):
    """Сохраняет саммари и вектор поста (для семантического дедупа A1)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET summary = ?, embedding = ? WHERE id = ?",
            (summary, embedding, msg_id),
        )
        await db.commit()


async def get_recent_forwarded_texts(hours: int = 24) -> list[str]:
    """Полные тексты пересланных постов за окно (для текстового предфильтра дедупа)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            """SELECT text FROM messages
               WHERE forwarded = 1 AND created_at > ?
               ORDER BY created_at DESC""",
            (cutoff,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_recent_forwarded_embeddings(hours: int = 24) -> list[bytes]:
    """BLOB'ы векторов пересланных постов за окно (для cosine-дедупа)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            """SELECT embedding FROM messages
               WHERE forwarded = 1 AND embedding IS NOT NULL AND created_at > ?
               ORDER BY created_at DESC""",
            (cutoff,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def cleanup_old_data(message_retention_days: int, embedding_retention_hours: int) -> tuple[int, int]:
    """Ретеншн: старые messages целиком удаляем (текст/мета дальше не нужны),
    embedding чистим раньше и агрессивнее — за пределами окна дедупа он мёртвый груз
    (2КБ/строка, самая тяжёлая колонка в базе). Возвращает (удалено сообщений, очищено embedding)."""
    msg_cutoff = (datetime.now(timezone.utc) - timedelta(days=message_retention_days)).isoformat()
    emb_cutoff = (datetime.now(timezone.utc) - timedelta(hours=embedding_retention_hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("DELETE FROM messages WHERE created_at < ?", (msg_cutoff,))
        deleted = cur.rowcount
        # topic_votes/message_deliveries без FK/CASCADE — чистим осиротевшие вручную
        await db.execute(
            "DELETE FROM topic_votes WHERE msg_id NOT IN (SELECT id FROM messages)"
        )
        await db.execute(
            "DELETE FROM message_deliveries WHERE msg_id NOT IN (SELECT id FROM messages)"
        )
        cur2 = await db.execute(
            "UPDATE messages SET embedding = NULL WHERE embedding IS NOT NULL AND created_at < ?",
            (emb_cutoff,),
        )
        cleared = cur2.rowcount
        await db.commit()
    return deleted, cleared


async def get_recent_forwarded_concepts(hours: int = 24) -> list[list[str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            """SELECT concepts FROM messages
               WHERE forwarded = 1 AND concepts IS NOT NULL AND created_at > ?
               ORDER BY created_at DESC""",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
            return [json.loads(r[0]) for r in rows if r[0]]


async def get_recent_headlines(hours: int = 24, limit: int = 30) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            """SELECT text FROM messages
               WHERE forwarded = 1 AND created_at > ?
               ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [row[0][:200] for row in rows]


async def get_concept_weights(concepts: list[str]) -> dict[str, float]:
    if not concepts:
        return {}
    async with aiosqlite.connect(config.DB_PATH) as db:
        placeholders = ",".join("?" * len(concepts))
        async with db.execute(
            f"SELECT topic, weight FROM interests WHERE topic IN ({placeholders})",
            concepts,
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}


async def get_adaptive_threshold(limit: int = 200) -> float:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT AVG(local_score) FROM "
            "(SELECT local_score FROM messages WHERE local_score IS NOT NULL ORDER BY created_at DESC LIMIT ?)",
            (limit,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] or 0.0


async def update_interests(topics: list[str], delta: float = 1.0):
    async with aiosqlite.connect(config.DB_PATH) as db:
        for topic in topics:
            await db.execute(
                "INSERT OR IGNORE INTO interests (topic, weight) VALUES (?, 0.0)", (topic,)
            )
            await db.execute(
                "UPDATE interests SET weight = weight + ? WHERE topic = ?", (delta, topic)
            )
        await db.commit()


async def get_topic_votes(msg_id: int) -> dict[int, str]:
    """Голоса по топикам поста: {topic_idx: 'like'|'dislike'}."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT topic_idx, choice FROM topic_votes WHERE msg_id = ?", (msg_id,)
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}


async def add_topic_vote(msg_id: int, topic_idx: int, choice: str) -> bool:
    """Записывает голос по топику. True — голос новый (применить вес),
    False — по этому топику уже голосовали (идемпотентность, вес не трогаем)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO topic_votes (msg_id, topic_idx, choice) VALUES (?, ?, ?)",
            (msg_id, topic_idx, choice),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_interests(limit: int = 30) -> list[tuple[str, float]]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT topic, weight FROM interests ORDER BY weight DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


async def get_total_likes() -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT SUM(likes) FROM channels") as cur:
            row = await cur.fetchone()
            return row[0] or 0


async def get_user_choice(msg_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT user_choice FROM messages WHERE id = ?", (msg_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_user_choice(msg_id: int, choice: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET user_choice = ? WHERE id = ?", (choice, msg_id)
        )
        await db.commit()


async def increment_channel_vote(msg_id: int, choice: str):
    col = "likes" if choice == "like" else "dislikes"
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            f"UPDATE channels SET {col} = {col} + 1 "
            f"WHERE channel_id = (SELECT channel_id FROM messages WHERE id = ?)",
            (msg_id,),
        )
        await db.commit()


async def get_channels_stats() -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            """SELECT channel_id, channel_name, active, seen, forwarded, likes, dislikes
               FROM channels
               ORDER BY
                 CASE WHEN (likes + dislikes) = 0 THEN 0
                      ELSE likes * 1.0 / (likes + dislikes)
                 END DESC"""
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "channel_id": r[0],
                    "channel_name": r[1],
                    "active": bool(r[2]),
                    "seen": r[3],
                    "forwarded": r[4],
                    "likes": r[5],
                    "dislikes": r[6],
                }
                for r in rows
            ]


async def toggle_channel(channel_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT active FROM channels WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return True
            new_state = not bool(row[0])
        await db.execute(
            "UPDATE channels SET active = ? WHERE channel_id = ?",
            (int(new_state), channel_id),
        )
        await db.commit()
        return new_state
