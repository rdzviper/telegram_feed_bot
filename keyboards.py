"""Inline keyboards for companions — shared by pipeline (first send)
and handlers (after vote/skip) to avoid circular imports."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def action_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Like / skip / dislike under the current post."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍", callback_data=f"like:{msg_id}"),
        InlineKeyboardButton("⏭", callback_data=f"skip:{msg_id}"),
        InlineKeyboardButton("👎", callback_data=f"dislike:{msg_id}"),
    ]])


def result_keyboard(choice: str) -> InlineKeyboardMarkup:
    """Final mark after like/dislike/skip — no more actions."""
    mark = {"like": "✅", "dislike": "❌", "skip": "⏭"}.get(choice, "•")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(mark, callback_data="noop"),
    ]])
