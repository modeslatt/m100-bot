"""Тексты бота и разбор ввода мастера — чистые функции, без aiogram.

Тексты короткие, на «вы». Эмодзи в сообщениях — только маркеры статуса: ✅ ⚠️ ❓.
Названия позиций — полные, из таблицы (`compact_name`): короткие автоматически получаются
плохо («Хомут для крепления» для двух разных строк).
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Literal

from m100bot.matching.llm import compact_name
from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.services.report import TodayLine, fmt_qty, is_ready
from m100bot.sheets.catalog import Catalog, Position

HELP = (
    "Пришлите отчёт за день текстом, например:\n"
    "«Проложили медь 6-ку 15 м и 9-ку 15 м, изоляцию столько же, кабель 3х2,5 — 56 м».\n\n"
    "Я покажу, что распознал, и запишу в таблицу только после подтверждения.\n"
    "/today — что записано сегодня и исправление\n"
    "/table — ссылка на таблицу"
)
THINKING = "Разбираю отчёт…"
NOTHING_FOUND = (
    "Не нашёл в сообщении работ. Напишите, что и сколько сделали, например: «медь 6-ку 15 м»."
)
NOTHING_ADDED = "Не нашёл новых работ. Напишите ещё раз или нажмите «Назад»."
VOICE_STUB = "Голос пока не поддерживается. Пришлите отчёт текстом."
OTHER_CONTENT = "Пришлите отчёт текстом."
UNKNOWN_COMMAND = "Не знаю такой команды. Список: /help"
CANCELLED = "Отчёт отменён. Ничего не записано."
EMPTY_REPORT = "Отчёт пуст. Ничего не записано."
ADD_PROMPT = "Пришлите ещё позиции текстом, я добавлю их к отчёту."
EDIT_PICK = "Какую позицию исправить?"
BAD_NUMBER = "Не понял число. Пришлите цифрами, например: 12 или 7,5."
FAILED = "Что-то пошло не так. Попробуйте ещё раз."
TABLE_TEXT = "Таблица с объёмами."
MAX_TEXT = 3800  # лимит Telegram — 4096 знаков; оставляем запас

_UNIT_WORDS = {
    "м": "метров",
    "шт": "штук",
    "кг": "килограммов",
    "компл": "комплектов",
    "сист": "систем",
}
_QTY_INPUT = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*[а-яёa-z.]*\s*$", re.IGNORECASE)


# ─────────────── Мелочи ───────────────


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(1, ("позиция","позиции","позиций")) → «позиция»."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def unit_word(unit: str) -> str:
    """«м» → «метров» (для вопроса «Сколько метров?»)."""
    return _UNIT_WORDS.get(unit, f"в «{unit}»")


def parse_qty(text: str) -> float | None:
    """Объём из ответа мастера: «15», «7,5», «15 м». Что-то другое — None."""
    m = _QTY_INPUT.match(text)
    return float(m.group(1).replace(",", ".")) if m else None


def looks_like_report(text: str) -> bool:
    """Длинный текст вместо числа — скорее всего, новый отчёт, а не ответ на вопрос."""
    return len(text.split()) >= 3


def needs_choice(item: MatchedItem) -> bool:
    """Позиция неоднозначна: нужно выбрать один из вариантов."""
    return (
        item.row is None
        and len(item.candidates) >= 2
        and item.status in (MatchStatus.AMBIGUOUS, MatchStatus.CLARIFY)
    )


def needs_qty(item: MatchedItem) -> bool:
    """Позиция ясна, но объём не назван или в других единицах."""
    return item.row is not None and item.status == MatchStatus.CLARIFY


def card_counts(items: list[MatchedItem]) -> tuple[int, int]:
    """(готовых к записи, требующих уточнения)."""
    ready = sum(1 for i in items if is_ready(i))
    unresolved = sum(1 for i in items if needs_choice(i) or needs_qty(i))
    return ready, unresolved


def _pos(item: MatchedItem, catalog: Catalog) -> Position | None:
    return catalog.by_row(item.row) if item.row is not None else None


def _name(item: MatchedItem, catalog: Catalog) -> str:
    pos = _pos(item, catalog)
    return compact_name(pos.name) if pos else f"строка {item.row}"


# ─────────────── Карточка ───────────────


def item_line(n: int, item: MatchedItem, catalog: Catalog) -> str:
    pos = _pos(item, catalog)
    unit = pos.unit if pos else (item.unit or "")
    if is_ready(item):
        assert item.qty is not None
        marker = "⚠️" if item.over_remaining else "✅"
        line = f"{n}. {marker} {_name(item, catalog)} — {fmt_qty(item.qty)} {unit}".rstrip()
        if item.over_remaining:
            left = max(item.remaining or 0.0, 0.0)
            line += f"\n   превышает остаток (осталось {fmt_qty(left)} {unit})".rstrip()
        return line
    if needs_qty(item):
        if item.qty is None or item.qty <= 0:
            return f"{n}. ❓ {_name(item, catalog)} — сколько?"
        given = f"{fmt_qty(item.qty)} {item.unit or ''}".rstrip()
        return f"{n}. ❓ {_name(item, catalog)} — в смете «{unit}», вы указали {given}"
    if needs_choice(item):
        return f"{n}. ❓ «{item.phrase}» — какой вариант?"
    return f"{n}. ❓ «{item.phrase}» — нет в смете, не записываю"


def card_text(items: list[MatchedItem], work_date: date, catalog: Catalog) -> str:
    """Нумерованный список позиций со статусами. Слишком длинный — обрезается с пометкой."""
    _, unresolved = card_counts(items)
    n = len(items)
    word = plural(n, ("позиция", "позиции", "позиций"))
    header = f"Отчёт за {work_date:%d.%m.%Y}. Распознано {n} {word}"
    header += f", {unresolved} нужно уточнить." if unresolved else "."
    lines = [item_line(i, item, catalog) for i, item in enumerate(items, start=1)]
    text = header + "\n\n" + "\n".join(lines)
    if len(text) <= MAX_TEXT:
        return text
    kept: list[str] = []
    size = len(header) + 2
    for line in lines:
        if size + len(line) + 1 > MAX_TEXT - 80:
            break
        kept.append(line)
        size += len(line) + 1
    rest = n - len(kept)
    return (
        header + "\n\n" + "\n".join(kept) + f"\n…и ещё {rest} (будут записаны вместе с остальными)"
    )


# ─────────────── Вопросы ───────────────


@dataclass(frozen=True)
class Question:
    kind: Literal["choice", "qty"]
    idx: int
    text: str
    options: list[tuple[int, str]]  # для choice: (строка таблицы, подпись кнопки)


def choice_options(item: MatchedItem, catalog: Catalog) -> list[tuple[int, str]]:
    """Подписи кнопок. Одинаковые названия (строки 48/49) различаем № и количеством по смете."""
    positions = [p for r in item.candidates if (p := catalog.by_row(r)) is not None]
    names = Counter(compact_name(p.name) for p in positions)
    options = []
    for p in sorted(positions, key=lambda p: p.row):
        name = compact_name(p.name)
        if names[name] > 1:
            num = p.number if p.number is not None else p.row
            suffix = f" · № {num}, по смете {fmt_qty(p.planned)} {p.unit}"
            label = name[: max(60 - len(suffix), 10)] + suffix
        else:
            label = name[:60]
        options.append((p.row, label))
    return options


def question_for(items: list[MatchedItem], idx: int, catalog: Catalog) -> Question | None:
    item = items[idx]
    n = idx + 1
    if needs_choice(item):
        options = choice_options(item, catalog)
        if len(options) >= 2:
            return Question("choice", idx, f"Позиция {n}: «{item.phrase}». Какой вариант?", options)
        return None
    if needs_qty(item):
        pos = _pos(item, catalog)
        if pos is None:
            return None
        word = unit_word(pos.unit)
        if item.qty is None or item.qty <= 0:
            head = f"Позиция {n}: {_name(item, catalog)}. Сколько {word}?"
        else:
            head = (
                f"Позиция {n}: {_name(item, catalog)}. В смете он в «{pos.unit}», "
                f"вы указали «{item.unit or '—'}». Сколько {word}?"
            )
        return Question("qty", idx, head + "\nОтветьте числом, например: 15", [])
    return None


def next_question(items: list[MatchedItem], catalog: Catalog) -> Question | None:
    """Первая по порядку позиция, которой нужен ответ мастера."""
    for idx in range(len(items)):
        q = question_for(items, idx, catalog)
        if q is not None:
            return q
    return None


# ─────────────── Исправление ───────────────


def item_edit_text(n: int, item: MatchedItem, catalog: Catalog) -> str:
    if item.row is None:
        return f"Позиция {n}: «{item.phrase}» — нет в смете.\nЕё можно только удалить."
    pos = _pos(item, catalog)
    unit = pos.unit if pos else ""
    now = f" — {fmt_qty(item.qty)} {unit}" if item.qty else ""
    return (
        f"Позиция {n}: {_name(item, catalog)}{now}.\n"
        f"Пришлите новый объём числом ({unit}) или удалите позицию."
    )


# ─────────────── /today ───────────────


def today_text(work_date: date, lines: list[TodayLine]) -> str:
    if not lines:
        return f"Сегодня, {work_date:%d.%m.%Y}, ничего не записано."
    body = "\n".join(
        f"{n}. {line.name} — {fmt_qty(line.qty)} {line.unit}".rstrip()
        for n, line in enumerate(lines, start=1)
    )
    return f"Сегодня, {work_date:%d.%m.%Y}, записано:\n\n{body}"


def today_item_text(line: TodayLine) -> str:
    return (
        f"{line.name} — {fmt_qty(line.qty)} {line.unit}.\n"
        f"Пришлите новый объём числом ({line.unit}) или очистите."
    ).replace(" .", ".")
