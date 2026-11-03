"""Тексты бота и разбор ввода — чистые функции."""

from datetime import date

import pytest

from m100bot.bot import render
from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.services.report import RemainingLine, TodayLine
from m100bot.sheets.catalog import Catalog

D = date(2026, 11, 2)


def ok(row: int, qty: float) -> MatchedItem:
    return MatchedItem(phrase="x", qty=qty, unit="м", status=MatchStatus.OK, row=row)


def amb(candidates: list[int], phrase: str = "хомуты") -> MatchedItem:
    return MatchedItem(
        phrase=phrase, qty=12, unit="шт", status=MatchStatus.AMBIGUOUS, candidates=candidates
    )


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "позиция"), (2, "позиции"), (4, "позиции"), (5, "позиций"), (11, "позиций"),
     (12, "позиций"), (14, "позиций"), (21, "позиция"), (22, "позиции"), (100, "позиций")],
)  # fmt: skip
def test_plural(n: int, word: str) -> None:
    assert render.plural(n, ("позиция", "позиции", "позиций")) == word


@pytest.mark.parametrize(
    ("text", "expected"),
    [("15", 15), ("7,5", 7.5), ("7.5", 7.5), (" 15 м ", 15), ("15м", 15), ("15 метров", 15),
     ("0", 0), ("много", None), ("1 2", None), ("15 и 20", None), ("", None), ("-5", None)],
)  # fmt: skip
def test_parse_qty(text: str, expected: float | None) -> None:
    assert render.parse_qty(text) == expected


def test_looks_like_report() -> None:
    assert not render.looks_like_report("15 м")
    assert not render.looks_like_report("пятнадцать метров")
    assert render.looks_like_report("проложили медь шестёрку")


def test_unit_word() -> None:
    assert render.unit_word("м") == "метров" and render.unit_word("шт") == "штук"
    assert render.unit_word("пог.м") == "в «пог.м»"  # неизвестная единица — без падежей


# ─────────────── Карточка ───────────────


def test_card_lines_for_every_status(catalog: Catalog) -> None:
    over = ok(55, 56)
    over.over_remaining, over.remaining = True, 12
    items = [
        ok(17, 0.5),
        over,
        MatchedItem(phrase="a", qty=None, unit=None, status=MatchStatus.CLARIFY, row=18),
        MatchedItem(phrase="b", qty=6, unit="м", status=MatchStatus.CLARIFY, row=67),
        amb([43, 44]),
        MatchedItem(phrase="покрасили", qty=20, unit="м2", status=MatchStatus.NOT_FOUND),
    ]
    text = render.card_text(items, D, catalog)

    assert text.startswith("Отчёт за 02.11.2026. Распознано 6 позиций, 3 нужно уточнить.")
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 0,5 м" in text  # запятая
    assert "2. ⚠️ Кабель силовой" in text and "\n   превышает остаток (осталось 12 м)" in text
    assert "3. ❓ Фреонопровод медный, Д9,52 TEC TUBE cips — сколько?" in text
    assert "4. ❓ Короб ПВХ размером 25х16мм, длиной 2м — в смете «шт», вы указали 6 м" in text
    assert "5. ❓ «хомуты» — какой вариант?" in text
    assert "6. ❓ «покрасили» — нет в смете, не записываю" in text


def test_card_header_without_questions(catalog: Catalog) -> None:
    assert render.card_text([ok(17, 1)], D, catalog).startswith(
        "Отчёт за 02.11.2026. Распознано 1 позиция.\n\n1. ✅"
    )


def test_remaining_never_shown_negative(catalog: Catalog) -> None:
    over = ok(17, 50)
    over.over_remaining, over.remaining = True, -8
    assert "(осталось 0 м)" in render.card_text([over], D, catalog)


def test_card_counts() -> None:
    items = [
        ok(17, 1),
        amb([43, 44]),
        MatchedItem(phrase="q", qty=None, unit=None, status=MatchStatus.NOT_FOUND),
    ]
    assert render.card_counts(items) == (1, 1)  # «нет в смете» — не вопрос


# ─────────────── Вопросы ───────────────


def test_next_question_goes_in_order(catalog: Catalog) -> None:
    items = [
        ok(17, 1),
        MatchedItem(phrase="a", qty=None, unit=None, status=MatchStatus.CLARIFY, row=18),
        amb([43, 44]),
    ]
    q = render.next_question(items, catalog)
    assert q is not None and (q.kind, q.idx) == ("qty", 1)
    assert render.next_question([ok(17, 1)], catalog) is None


def test_choice_labels_are_plain_when_names_differ(catalog: Catalog) -> None:
    q = render.question_for([amb([43, 44])], 0, catalog)
    assert q is not None and q.kind == "choice"
    assert [label for _, label in q.options] == [
        "Хомут для крепления дренажа М8 25мм",
        "Хомут для крепления фреонопровода М8 32 мм",
    ]


def test_choice_labels_separate_identical_names(catalog: Catalog) -> None:
    """Строки 48 и 49 — «Сверление отверстий Д80»: различаем № и количеством по смете."""
    q = render.question_for([amb([49, 48])], 0, catalog)
    assert q is not None
    assert [row for row, _ in q.options] == [48, 49]  # по порядку строк
    first, second = (label for _, label in q.options)
    assert first.endswith("· № 44, по смете 18 шт") and second.endswith("· № 45, по смете 26 шт")


def test_choice_question_lists_full_names(catalog: Catalog) -> None:
    """Длинные названия кнопка обрезает — поэтому полный список стоит в тексте вопроса."""
    q = render.question_for([amb([43, 44])], 0, catalog)
    assert q is not None
    assert q.text.endswith(
        "Какой вариант?\n\n"
        "1. Хомут для крепления дренажа М8 25мм\n"
        "2. Хомут для крепления фреонопровода М8 32 мм"
    )


def test_no_question_for_resolved_items(catalog: Catalog) -> None:
    assert render.question_for([ok(17, 1)], 0, catalog) is None


# ─────────────── /today и правка ───────────────


def test_today_text() -> None:
    lines = [TodayLine(17, "Фреонопровод", 20, "м"), TodayLine(28, "Хладагент", 0.5, "кг")]
    assert render.today_text(D, lines) == (
        "Сегодня, 02.11.2026, записано:\n\n1. Фреонопровод — 20 м\n2. Хладагент — 0,5 кг"
    )
    assert render.today_text(D, []) == "Сегодня, 02.11.2026, ничего не записано."


def test_item_edit_text(catalog: Catalog) -> None:
    text = render.item_edit_text(2, ok(17, 15), catalog)
    assert text == (
        "Позиция 2: Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м.\n"
        "Пришлите новый объём числом (м) или удалите позицию."
    )
    gone = MatchedItem(phrase="стена", qty=1, unit="м", status=MatchStatus.NOT_FOUND)
    assert render.item_edit_text(1, gone, catalog).endswith("Её можно только удалить.")


@pytest.mark.parametrize(
    ("text", "qty"),
    [
        ("15", 15),
        ("7,5 м", 7.5),
        ("Пятнадцать.", 15),
        ("пятнадцать метров", 15),
        ("двести сорок пять", 245),
        ("сто", 100),
        ("семь с половиной метров", 7.5),
        ("полтора", 1.5),
        ("ноль", 0),
        ("три штуки", 3),
        # не число или число не целиком — лучше переспросить
        ("пятнадцать пять", None),
        ("пять двадцать", None),
        ("медь шестёрка", None),
        ("много", None),
        ("", None),
    ],
)
def test_parse_qty_digits_and_words(text: str, qty: float | None) -> None:
    assert render.parse_qty(text) == qty


def test_remaining_text() -> None:
    lines = [
        RemainingLine(55, "Кабель 3х2,5", "м", 182, 112),
        RemainingLine(48, "Сверление Д80", "шт", 18, 20),
    ]
    assert render.remaining_text(lines) == (
        "Кабель 3х2,5:\nПо смете 182 м, выполнено 112 м, осталось 70 м.\n\n"
        "Сверление Д80:\nПо смете 18 шт, выполнено 20 шт — сверх сметы 2 шт."
    )
