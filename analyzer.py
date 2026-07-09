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
{{"concepts": ["концепт1", ...], "is_ad": false, "informative": true}}

Правила:
- concepts: 3-7 ключевых концептов. Форма: именительный падеж, ед. число.
- КАНОНИЗАЦИЯ (критично для сравнения постов): один и тот же объект ВСЕГДА должен давать одну и ту же строку, независимо от того, как он назван в тексте. Приводи к самой узнаваемой общепринятой форме:
  • Аббревиатура и полное название → одна форма: "Counter-Strike 2" и "CS2" → "CS2"; "искусственный интеллект" и "ИИ" → "ИИ"; "Playstation 5" и "PS5" → "PS5".
  • Убирай уточнения, порядковые номера, эпитеты — до базового существительного: "собаки-заложники" → "собака"; "5-й сезон Premier" → "сезон Premier"; "бездомный кот" → "кот".
  • Сохраняй прилагательное только в устойчивом термине, где без него меняется смысл: "кредитная карта", "ядерное оружие", "социальная сеть".
- ОБЯЗАТЕЛЬНО первым концептом — главная сущность текста (произведение, персона, компания, продукт, игра, событие), даже если упомянута кратко, в каноничной форме: "третья часть Дюны" → "Дюна". Это нужно чтобы разные посты об одном сюжете совпадали.
- Общие слова ("новость", "рынок", "компания", "мероприятие", "обновление") не включать. Шутки, иронию, метафоры игнорировать ("Кошачья служба" — не концепт).
- is_ad: реклама, промо, партнёрский материал, призыв купить/подписаться
- informative: несёт ли ТЕКСТ самостоятельный смысл — новость, факт, законченную мысль, полноценную шутку. false для пустых реакций, филлера, контекстно-зависимых однострочников, которые без картинки/треда ничего не значат ("Не прошло и года", "Запости жизу заебал", "ваша остановка"). Оценивай только текст; если сомневаешься — true\
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
            return {"concepts": [], "is_ad": False, "informative": True}
        raw = json.loads(content)
        return {
            "concepts": [str(c) for c in raw.get("concepts", [])][:7],
            "is_ad": bool(raw.get("is_ad", False)),
            "informative": bool(raw.get("informative", True)),
        }
    except Exception as e:
        logger.error("DeepSeek error: %s", e)
        return {"concepts": [], "is_ad": False, "informative": True}
