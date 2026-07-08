import json
import logging
from openai import AsyncOpenAI
import config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.DEEPSEEK_API_KEY,
    base_url=config.DEEPSEEK_BASE_URL,
)

_ANALYZE_PROMPT = """\
Проанализируй Telegram-пост и верни JSON.

АНАЛИЗИРУЕМЫЙ ПОСТ:
{text}

Верни строго JSON без пояснений:
{{"concepts": ["концепт1", ...], "is_ad": false}}

Правила:
- concepts: 3-7 ключевых концептов. Форма: именительный падеж, ед. число. Убирать описательные прилагательные, если они не меняют смысл ("бездомный кот" → "кот", "уличное животное" → "животное"). Сохранять прилагательные, если они образуют устойчивый термин ("кредитная карта", "ядерное оружие", "социальная сеть"). Общие слова ("новость", "рынок", "компания", "мероприятие") не включать. Шутки, иронию, метафоры игнорировать ("Кошачья служба" — не концепт).
- is_ad: реклама, промо, партнёрский материал, призыв купить/подписаться\
"""

_CALL_KWARGS = dict(
    model=config.DEEPSEEK_MODEL,
    temperature=0,
    response_format={"type": "json_object"},
    extra_body={"thinking": {"type": "disabled"}},
)



async def analyze(text: str) -> dict:
    prompt = _ANALYZE_PROMPT.format(text=text[:1500])
    try:
        logger.debug("Calling DeepSeek model=%s prompt_len=%d", config.DEEPSEEK_MODEL, len(prompt))
        response = await _client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            **_CALL_KWARGS,
        )
        content = response.choices[0].message.content or ""
        logger.debug("DeepSeek raw: %r", content)
        if not content.strip():
            logger.warning("DeepSeek returned empty content, using defaults")
            return {"concepts": [], "is_ad": False}
        raw = json.loads(content)
        return {
            "concepts": [str(c) for c in raw.get("concepts", [])][:7],
            "is_ad": bool(raw.get("is_ad", False)),
        }
    except Exception as e:
        logger.error("DeepSeek error: %s", e)
        return {"concepts": [], "is_ad": False}
