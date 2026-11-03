"""Клавиатуры бота.

В кнопках карточки зашит отпечаток черновика (`t`): после любого изменения черновика
кнопки старых карточек перестают работать (см. `Draft.token`).
Эмодзи на кнопках — как были в заготовке: ✅ ✏️ ➕ ✖️ 📊.
"""

from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from m100bot.services.report import TodayLine

CardAction = Literal["ok", "edit", "add", "cancel", "back", "item", "del", "qdel", "pick"]
TodayAction = Literal["edit", "row", "clear", "back"]


class Act(CallbackData, prefix="r"):
    """Действие над карточкой отчёта."""

    a: CardAction
    t: str = ""  # отпечаток черновика
    i: int = -1  # номер позиции в черновике (с 0)
    r: int = 0  # строка таблицы (для pick; 0 — «нет нужного»)


class Today(CallbackData, prefix="d"):
    """Действие в /today."""

    a: TodayAction
    r: int = 0  # строка таблицы


def review_keyboard(token: str, n_ready: int, n_unresolved: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    if n_ready:
        label = f"✅ Записать готовое ({n_ready})" if n_unresolved else "✅ Записать"
        kb.button(text=label, callback_data=Act(a="ok", t=token))
        rows.append(1)
    kb.button(text="✏️ Исправить", callback_data=Act(a="edit", t=token))
    kb.button(text="➕ Добавить", callback_data=Act(a="add", t=token))
    kb.button(text="✖️ Отмена", callback_data=Act(a="cancel", t=token))
    rows.append(3)
    kb.adjust(*rows)
    return kb.as_markup()


def choice_keyboard(token: str, idx: int, options: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Варианты для неоднозначной позиции: (строка таблицы, подпись)."""
    kb = InlineKeyboardBuilder()
    for row, label in options:
        kb.button(text=label, callback_data=Act(a="pick", t=token, i=idx, r=row))
    kb.button(text="Нет нужного", callback_data=Act(a="pick", t=token, i=idx, r=0))
    kb.adjust(1)
    return kb.as_markup()


def qty_question_keyboard(token: str, idx: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Удалить позицию", callback_data=Act(a="qdel", t=token, i=idx))
    return kb.as_markup()


def edit_pick_keyboard(token: str, n_items: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i in range(n_items):
        kb.button(text=str(i + 1), callback_data=Act(a="item", t=token, i=i))
    kb.button(text="Назад", callback_data=Act(a="back", t=token))
    kb.adjust(*([5] * (n_items // 5)), *([n_items % 5] if n_items % 5 else []), 1)
    return kb.as_markup()


def item_edit_keyboard(token: str, idx: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Удалить", callback_data=Act(a="del", t=token, i=idx))
    kb.button(text="Назад", callback_data=Act(a="back", t=token))
    kb.adjust(2)
    return kb.as_markup()


def back_keyboard(token: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data=Act(a="back", t=token))
    return kb.as_markup()


def sheet_link_keyboard(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Открыть таблицу", url=url)
    return kb.as_markup()


def today_keyboard(url: str, has_lines: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_lines:
        kb.button(text="✏️ Исправить", callback_data=Today(a="edit"))
    kb.button(text="📊 Открыть таблицу", url=url)
    kb.adjust(2 if has_lines else 1)
    return kb.as_markup()


def today_pick_keyboard(lines: list[TodayLine]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n, line in enumerate(lines, start=1):
        kb.button(text=str(n), callback_data=Today(a="row", r=line.row))
    kb.button(text="Назад", callback_data=Today(a="back"))
    n = len(lines)
    kb.adjust(*([5] * (n // 5)), *([n % 5] if n % 5 else []), 1)
    return kb.as_markup()


def today_item_keyboard(row: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Очистить", callback_data=Today(a="clear", r=row))
    kb.button(text="Назад", callback_data=Today(a="back"))
    kb.adjust(2)
    return kb.as_markup()
