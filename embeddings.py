"""Семантический слой (A1): эмбеддинги саммари для дедупа по смыслу.

Вектор считается через внешний OpenAI-совместимый API (у DeepSeek эмбеддингов нет).
Локальной модели нет — footprint ящика не трогаем. Любая ошибка API → None,
пайплайн не падает (дедуп деградирует до текстового предфильтра).
"""
import logging

import numpy as np
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

# клиент поднимаем только если задан ключ — без ключа embed() всегда None
_client = (
    AsyncOpenAI(api_key=config.EMBEDDING_API_KEY, base_url=config.EMBEDDING_BASE_URL)
    if config.EMBEDDING_API_KEY
    else None
)


async def embed(text: str) -> np.ndarray | None:
    """L2-нормированный float32 вектор саммари (cosine = dot).
    None — если нет ключа/пустой текст/ошибка API (пайплайн продолжает работать)."""
    if _client is None or not text.strip():
        return None
    try:
        resp = await _client.embeddings.create(
            model=config.EMBEDDING_MODEL,
            input=text,
            dimensions=config.EMBEDDING_DIM,
        )
        vec = np.asarray(resp.data[0].embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return None
        return vec / norm
    except Exception as e:
        logger.error("Embedding API error: %s", e)
        return None


def to_blob(vec: np.ndarray) -> bytes:
    """Вектор → BLOB для хранения в SQLite."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """BLOB из SQLite → вектор."""
    return np.frombuffer(blob, dtype=np.float32)
