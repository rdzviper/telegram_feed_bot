"""Билдеры инлайн-клавиатур компаньонов, общие для pipeline.py (первичная
отправка) и handlers.py (перерисовка после голоса) — вынесены сюда, чтобы
избежать циклического импорта между этими модулями."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TOPIC_LABEL_MAX = 24
PAGE_CALLBACK_PREFIX = "page:"


def trunc(topic: str) -> str:
    return topic if len(topic) <= TOPIC_LABEL_MAX else topic[: TOPIC_LABEL_MAX - 1] + "…"


def voted_row(topic: str, choice: str) -> list[InlineKeyboardButton]:
    mark = "✅" if choice == "like" else "❌"
    return [InlineKeyboardButton(f"{mark} {trunc(topic)}", callback_data="noop")]


def concept_rows(msg_id: int, concepts: list[str], votes: dict[int, str]) -> list[list[InlineKeyboardButton]]:
    rows = []
    for i, topic in enumerate(concepts):
        if i in votes:
            rows.append(voted_row(topic, votes[i]))
        else:
            rows.append([
                InlineKeyboardButton(trunc(topic), callback_data="noop"),
                InlineKeyboardButton("👍", callback_data=f"tl:{msg_id}:{i}"),
                InlineKeyboardButton("👎", callback_data=f"td:{msg_id}:{i}"),
            ])
    return rows


def pager_row(msg_id: int, label: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(label, callback_data=f"{PAGE_CALLBACK_PREFIX}{msg_id}")]


def companion_keyboard(
    msg_id: int,
    concepts: list[str],
    votes: dict[int, str],
    with_pager: bool,
    pager_label: str = "⏭ Промотать",
) -> InlineKeyboardMarkup | None:
    """Клавиатура текстового поста: строки концептов + опционально строка пейджера."""
    rows = concept_rows(msg_id, concepts, votes)
    if with_pager:
        rows.append(pager_row(msg_id, pager_label))
    return InlineKeyboardMarkup(rows) if rows else None


def media_keyboard(msg_id: int, with_pager: bool) -> InlineKeyboardMarkup | None:
    """Клавиатура медиа-без-подписи компаньона: только строка пейджера (если есть)."""
    return InlineKeyboardMarkup([pager_row(msg_id, "⏭ Следующий")]) if with_pager else None
