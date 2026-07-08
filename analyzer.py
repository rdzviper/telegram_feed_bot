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

НЕДАВНО ПЕРЕСЛАННЫЕ ПОСТЫ (для проверки дублей):
{recent}

АНАЛИЗИРУЕМЫЙ ПОСТ:
{text}

Верни строго JSON без пояснений:
{{"concepts": ["концепт1", ...], "is_ad": false, "same_story": false}}

Правила:
- concepts: 3-7 ключевых концептов, нормализованных (именительный падеж, ед. число, одна каноническая форма). "Sony" и "сони" → "Sony". "игры" и "игровой" → "игра". Общие слова ("новость", "рынок", "компания") не включать.
- is_ad: реклама, промо, партнёрский материал, призыв купить/подписаться
- same_story: тот же инфоповод что уже есть среди недавних постов (другие слова, тот же факт)\
"""

_CALL_KWARGS = dict(
    model=config.DEEPSEEK_MODEL,
    temperature=0,
    response_format={"type": "json_object"},
    extra_body={"thinking": {"type": "disabled"}},
)


def _fmt_list(items: list[str], empty: str = "нет") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


async def analyze(text: str, recent_headlines: list[str]) -> dict:
    prompt = _ANALYZE_PROMPT.format(
        recent=_fmt_list(recent_headlines),
        text=text[:1500],
    )
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
            return {"concepts": [], "is_ad": False, "same_story": False}
        raw = json.loads(content)
        return {
            "concepts": [str(c) for c in raw.get("concepts", [])][:7],
            "is_ad": bool(raw.get("is_ad", False)),
            "same_story": bool(raw.get("same_story", False)),
        }
    except Exception as e:
        logger.error("DeepSeek error: %s", e)
        return {"concepts": [], "is_ad": False, "same_story": False}
