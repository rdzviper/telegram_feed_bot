import io
import logging

import imagehash
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Почти однотонный кадр (чёрный fade-in intro, статичная заглушка) даёт
# "стабильный", но бессмысленный хэш — риск ложных совпадений между разными
# видео с одинаковым чёрным началом выше, чем польза от такого сигнала.
_MIN_STDDEV = 10.0


def _phash_from_bytes(raw: bytes | None, source: str = "") -> str | None:
    """Чистая часть без Telegram-обёртки: raw-байты превью -> phash | None.
    None — если декодировать не вышло, или превью почти однотонное (см.
    _MIN_STDDEV, чёрный fade-in intro и т.п.) — такой хэш ненадёжен."""
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("L")
    except Exception as e:
        logger.warning("hash_media: decode failed for %s: %s", source, e)
        return None
    if float(np.asarray(img, dtype=np.float64).std()) < _MIN_STDDEV:
        logger.debug("hash_media: near-blank thumbnail for %s, skipping", source)
        return None
    return str(imagehash.phash(img))


async def hash_message(tg_client, msg) -> str | None:
    """Скачивает только превью (не сам файл) и считает perceptual hash."""
    if msg is None or not getattr(msg, "media", None):
        return None
    try:
        raw = await tg_client.download_media(msg, file=bytes, thumb=0)
    except Exception as e:
        logger.warning("hash_message: thumbnail download failed for msg_id=%s: %s", msg.id, e)
        return None
    return _phash_from_bytes(raw, source=f"msg_id={msg.id}")


def hamming(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
