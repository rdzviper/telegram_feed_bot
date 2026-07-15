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
{{"concepts": ["концепт1", ...], "is_ad": false, "informative": true, "summary": "суть события одной фразой"}}

Правила:
- concepts: ровно {n} ключевых концептов (можно меньше, только если значимых в тексте реально меньше). Форма: именительный падеж, ед. число.
- КАНОНИЗАЦИЯ (критично для сравнения постов): один и тот же объект ВСЕГДА должен давать одну и ту же строку, независимо от того, как он назван в тексте. Приводи к самой узнаваемой общепринятой форме:
  • Аббревиатура и полное название → одна форма: "Counter-Strike 2" и "CS2" → "CS2"; "искусственный интеллект" и "ИИ" → "ИИ"; "Playstation 5" и "PS5" → "PS5".
  • Убирай уточнения, порядковые номера, эпитеты — до базового существительного: "собаки-заложники" → "собака"; "5-й сезон Premier" → "сезон Premier"; "бездомный кот" → "кот".
  • Сохраняй прилагательное только в устойчивом термине, где без него меняется смысл: "кредитная карта", "ядерное оружие", "социальная сеть".
  • Описательные действия/ситуации разбивай на предметные концепты, а не оставляй составной фразой: "заправка вне очереди" → "АЗС", "очередь"; "обслуживание вне очереди" → "очередь". Один и тот же поступок, названный разными словами в разных постах, обязан давать одни и те же предметные концепты.
  • Конкретную географию события (город/страна), общую для сюжета, включай отдельным концептом в каноничной форме: "Девушка из Пензы" → "Пенза". Это помогает разным постам об одном событии совпасть при дедупе.
- ОБЯЗАТЕЛЬНО первым концептом — главная сущность текста (произведение, персона, компания, продукт, игра, событие), даже если упомянута кратко, в каноничной форме: "третья часть Дюны" → "Дюна". Это нужно чтобы разные посты об одном сюжете совпадали.
- Общие слова ("новость", "рынок", "компания", "мероприятие", "обновление") не включать. Шутки, иронию, метафоры игнорировать ("Кошачья служба" — не концепт).
- is_ad: реклама, промо, партнёрский материал, призыв купить/подписаться. Также считать рекламой: зазыв или приглашение в другой канал/чат (инвайт-ссылка вида t.me/+…, «наконец-то в Telegram», «подписывайся»), контент-приманку ради набора подписчиков (обещание голых фото, «слитых» материалов, шок-контента с переходом по ссылке)
- informative: несёт ли ТЕКСТ самостоятельный смысл — новость, факт, законченную мысль, полноценную шутку. false для пустых реакций, филлера, контекстно-зависимых однострочников, которые без картинки/треда ничего не значат ("Не прошло и года", "Запости жизу заебал", "ваша остановка"). Оценивай только текст; если сомневаешься — true
- summary: одна короткая нейтральная фраза сути события (кто/что/где), канонично, без филлера/эмодзи/оценок/призывов. Для РАЗНЫХ постов об ОДНОМ событии summary обязаны получиться максимально похожими — это используется для дедупа по смыслу. Пример: "Россия: тестируют госмессенджер «Точка контакта» для заключённых в колониях"\
"""

_CALL_KWARGS = dict(
    model=config.DEEPSEEK_MODEL,
    temperature=0,
    response_format={"type": "json_object"},
    extra_body={"thinking": {"type": "disabled"}},
)

_WORDS_PER_CONCEPT = 20
_MAX_CONCEPTS = 10

# Короткий пост (≤5 слов) в DeepSeek не шлём: каждое слово поста — топик,
# берём только слова длиннее 3 символов.
_SHORT_POST_MAX_WORDS = 5
_MIN_TOPIC_LEN = 3
_PUNCT = "«»\"'“”„.,!?;:()[]{}—–-…*_`"


def target_concept_count(text: str) -> int:
    """Сколько концептов просить: 1 на 20 слов, зажато в [1, 10]."""
    words = len(text.split())
    return max(1, min(_MAX_CONCEPTS, round(words / _WORDS_PER_CONCEPT)))


def _short_post_concepts(text: str) -> list[str]:
    """Топики короткого поста: каждое слово длиннее 3 символов (без дублей, порядок сохранён)."""
    concepts = []
    for w in text.split():
        cw = w.strip(_PUNCT)
        if len(cw) > _MIN_TOPIC_LEN:
            concepts.append(cw)
    return list(dict.fromkeys(concepts))


def _fallback(text: str) -> dict:
    # summary = сырой текст (обрезан) — чтобы семантический слой дедупа работал даже без DeepSeek
    return {"concepts": [], "is_ad": False, "informative": True, "summary": text.strip()[:500]}


async def analyze(text: str) -> dict:
    words = text.split()
    if len(words) <= _SHORT_POST_MAX_WORDS:
        concepts = _short_post_concepts(text)
        logger.debug("Short post (%d words), skip DeepSeek, concepts=%s", len(words), concepts)
        # короткий пост в DeepSeek не идёт → summary = сам текст
        return {"concepts": concepts, "is_ad": False, "informative": True, "summary": text.strip()}

    n = target_concept_count(text)
    prompt = _ANALYZE_PROMPT.format(text=text[:1500], n=n)
    try:
        logger.debug("Calling DeepSeek model=%s n=%d prompt_len=%d", config.DEEPSEEK_MODEL, n, len(prompt))
        response = await _client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            **_CALL_KWARGS,
        )
        content = response.choices[0].message.content or ""
        logger.debug("DeepSeek raw: %r", content)
        if not content.strip():
            logger.warning("DeepSeek returned empty content, using defaults")
            return _fallback(text)
        raw = json.loads(content)
        return {
            "concepts": [str(c) for c in raw.get("concepts", [])][:n],
            "is_ad": bool(raw.get("is_ad", False)),
            "informative": bool(raw.get("informative", True)),
            "summary": (str(raw.get("summary", "")).strip() or text.strip()[:500]),
        }
    except Exception as e:
        logger.error("DeepSeek error: %s", e)
        return _fallback(text)
