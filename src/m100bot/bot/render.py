"""Тексты бота и разбор ввода мастера — чистые функции, без aiogram.

Тексты короткие, на «вы». Эмодзи в сообщениях — только маркеры статуса: ✅ ⚠️ ❓.
Названия позиций — полные, из таблицы (`Position.short_name`): короткие автоматически получаются
плохо («Хомут для крепления» для двух разных строк).
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Literal

from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.matching.normalize import NUMBER_WORDS
from m100bot.services.report import RemainingLine, TodayLine, fmt_qty
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
VOICE_STUB = "Голосовые не настроены. Пришлите отчёт текстом."
LISTENING = "Распознаю голос…"
TRUNCATED = (
    "⚠️ Сообщение длинное — проверьте, все ли работы попали в отчёт. "
    "Недостающие пришлите отдельным сообщением."
)
VOICE_TOO_LONG = "Голосовое длиннее 5 минут. Разбейте на части или пришлите текстом."
VOICE_EMPTY = "Не расслышал. Повторите голосовое или пришлите текстом."
VOICE_FAILED = "Не удалось распознать голосовое. Попробуйте ещё раз или пришлите текстом."
OTHER_CONTENT = "Пришлите отчёт текстом."
UNKNOWN_COMMAND = "Не знаю такой команды. Список: /help"
CANCELLED = "Отчёт отменён. Ничего не записано."
EMPTY_REPORT = "Отчёт пуст. Ничего не записано."
ADD_PROMPT = "Пришлите ещё позиции текстом, я добавлю их к отчёту."
EDIT_PICK = "Какую позицию исправить?"
BAD_NUMBER = "Не понял число. Пришлите цифрами, например: 12 или 7,5."
FAILED = "Что-то пошло не так. Попробуйте ещё раз."
TABLE_TEXT = "Таблица с объёмами."
STALE_DRAFT = (
    "Есть незаписанный отчёт за {day:%d.%m.%Y} ({n} {word}). "
    "Добавить новое сообщение к нему или начать новый отчёт?"
)
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


def heard(text: str) -> str:
    """Что распознано из голоса — мастер видит, откуда взялись позиции в карточке."""
    return f"Распознал: «{text}»"


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
    """Объём из ответа мастера: «15», «7,5», «15 м», «пятнадцать метров». Иначе — None."""
    m = _QTY_INPUT.match(text)
    if m:
        return float(m.group(1).replace(",", "."))
    return words_to_number(text)


# Единицы после числа («пятнадцать метров») и слова-связки — не мешают
_FILLER_PREFIXES = ("метр", "м", "шт", "штук", "кг", "килограм", "сист", "компл", "ровно")


def words_to_number(text: str) -> float | None:
    """«пятнадцать», «двести сорок пять метров», «семь с половиной», «полтора» → число.

    Только простые числа до 999 и «с половиной»: этого хватает для ответа «сколько?».
    Не разобралось целиком — None (лучше переспросить, чем записать не то).
    """
    if re.search(r"\d", text):
        return None  # цифры разбирает parse_qty; смесь «2 сотни» не угадываем
    tokens = re.findall(r"[а-яё]+", text.lower().replace("ё", "е"))
    while tokens and tokens[-1].startswith(_FILLER_PREFIXES):
        tokens.pop()
    if tokens in (["полтора"], ["полторы"]):
        return 1.5
    if tokens[-2:] == ["с", "половиной"]:
        whole = words_to_number(" ".join(tokens[:-2]))
        return whole + 0.5 if whole is not None else None
    if not tokens:
        return None
    total, last_rank = 0.0, 4
    for token in tokens:
        if token not in NUMBER_WORDS:
            return None
        value, rank = NUMBER_WORDS[token]
        if rank >= last_rank:
            return None
        total += value
        # после «десять…девятнадцать» единицы не идут: «пятнадцать пять» — не число
        last_rank = 1 if 10 <= value < 20 else rank
    return total


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
    ready = sum(1 for i in items if i.is_ready)
    unresolved = sum(1 for i in items if needs_choice(i) or needs_qty(i))
    return ready, unresolved


def _pos(item: MatchedItem, catalog: Catalog) -> Position | None:
    return catalog.by_row(item.row) if item.row is not None else None


def _name(item: MatchedItem, catalog: Catalog) -> str:
    pos = _pos(item, catalog)
    return pos.short_name if pos else f"строка {item.row}"


# ─────────────── Карточка ───────────────


def item_line(n: int, item: MatchedItem, catalog: Catalog) -> str:
    pos = _pos(item, catalog)
    unit = pos.unit if pos else (item.unit or "")
    if item.is_ready:
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
    options: list[tuple[int, str]]  # для choice: (строка таблицы, полное название варианта)


def choice_options(item: MatchedItem, catalog: Catalog) -> list[tuple[int, str]]:
    """Варианты для вопроса — полными названиями (в кнопку они не влезают, поэтому стоят
    списком в тексте, а на кнопках — номера). Одинаковые названия (строки 48/49) различаем
    № и количеством по смете."""
    positions = [p for r in item.candidates if (p := catalog.by_row(r)) is not None]
    names = Counter(p.short_name for p in positions)
    options = []
    for p in sorted(positions, key=lambda p: p.row):
        name = p.short_name
        if names[name] > 1:
            num = p.number if p.number is not None else p.row
            name += f" · № {num}, по смете {fmt_qty(p.planned)} {p.unit}"
        options.append((p.row, name))
    return options


def question_for(items: list[MatchedItem], idx: int, catalog: Catalog) -> Question | None:
    item = items[idx]
    n = idx + 1
    if needs_choice(item):
        options = choice_options(item, catalog)
        if len(options) >= 2:
            listing = "\n".join(f"{k}. {name}" for k, (_, name) in enumerate(options, start=1))
            text = f"Позиция {n}: «{item.phrase}». Какой вариант?\n\n{listing}"
            return Question("choice", idx, text, options)
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

TODAY_STALE = "Этот список — за прошлый день. Откройте /today заново."


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


# ─────────────── «Сколько осталось по …» ───────────────

QUESTION_NOT_FOUND = (
    "Не нашёл позицию. Спросите точнее, например: «сколько осталось по кабелю 3х2,5?»"
)


def remaining_text(lines: list[RemainingLine]) -> str:
    """«Кабель … 3х2,5: по смете 182 м, выполнено 112 м, осталось 70 м.» — по строке на позицию."""
    out = []
    for line in lines:
        u = line.unit
        plan, done = fmt_qty(line.planned), fmt_qty(line.done)
        head = f"{line.name}:\nПо смете {plan} {u}, выполнено {done} {u}"
        if line.left >= 0:
            out.append(f"{head}, осталось {fmt_qty(line.left)} {u}.")
        else:
            out.append(f"{head} — сверх сметы {fmt_qty(-line.left)} {u}.")
    return "\n\n".join(out)


def remaining_question(item: MatchedItem, catalog: Catalog) -> tuple[str, list[tuple[int, str]]]:
    """Вопрос об остатке подходит к нескольким позициям: «По какой позиции?» + список."""
    options = choice_options(item, catalog)
    listing = "\n".join(f"{k}. {name}" for k, (_, name) in enumerate(options, start=1))
    return f"«{item.phrase}» — по какой позиции?\n\n{listing}", options
