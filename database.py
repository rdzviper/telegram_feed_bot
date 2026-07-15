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
"""

_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN concepts TEXT",
    "ALTER TABLE messages ADD COLUMN local_score REAL",
    "ALTER TABLE messages ADD COLUMN summary TEXT",
    "ALTER TABLE messages ADD COLUMN embedding BLOB",
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


async def mark_forwarded(msg_id: int, bot_message_id: int | None, is_drift: bool):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET forwarded = 1, bot_message_id = ?, is_drift = ? WHERE id = ?",
            (bot_message_id, int(is_drift), msg_id),
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
