from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def review_keyboard(n_items: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Записать", callback_data="confirm")
    kb.button(text="✏️ Исправить", callback_data="edit")
    kb.button(text="➕ Добавить", callback_data="add")
    kb.button(text="✖️ Отмена", callback_data="cancel")
    kb.adjust(1, 3)
    return kb.as_markup()


def choice_keyboard(item_idx: int, options: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Варианты для неоднозначной позиции: (строка, короткое название)."""
    kb = InlineKeyboardBuilder()
    for row, title in options:
        kb.button(text=title[:60], callback_data=f"pick:{item_idx}:{row}")
    kb.button(text="Нет нужного", callback_data=f"pick:{item_idx}:none")
    kb.adjust(1)
    return kb.as_markup()


def sheet_link_keyboard(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Открыть таблицу", url=url)
    return kb.as_markup()
