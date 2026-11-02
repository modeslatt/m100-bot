"""Разбор фразы мастера кодом: объёмы, деление слитых упоминаний, голые числа."""

import pytest

from m100bot.matching.phrase import (
    Part,
    bare_numbers,
    find_quantities,
    split_by_features,
    synonym_key,
)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("пнр мультисплита 2 системы", [(2.0, "сист")]),
        ("кабель 3 на 2,5 — 56 м", [(56.0, "м")]),  # 2,5 — сечение, не объём
        ("кифлекс 9 мм 10 метров", [(10.0, "м")]),  # «9 мм» — размер
        ("4х1.5 24м", [(24.0, "м")]),
        ("дозаправили фреоном 0,5 кг", [(0.5, "кг")]),
        ("покрасили стену 20 м2", [(20.0, "м2")]),
        ("тройники 3 штуки", [(3.0, "шт")]),
        ("медь 6-ку 15 м и 9-ку 15 м", [(15.0, "м"), (15.0, "м")]),
        ("повесили внутряк К3.1", []),
    ],
)
def test_find_quantities(phrase: str, expected: list) -> None:
    assert [(q.value, q.unit) for q in find_quantities(phrase)] == expected


@pytest.mark.parametrize(
    ("phrase", "parts"),
    [
        (
            "автоматы 20А и 32А по одному",
            [Part("автоматы 20а", 1.0), Part("автоматы 32а по одному", 1.0)],
        ),
        (
            "трасса 1/4 и 3/8 по 14 метров",
            [Part("трасса 1/4", 14.0), Part("трасса 3/8 по 14 метров", 14.0)],
        ),
        (
            "кабель 4 на полтора 24 и 3 на полтора 16",
            [Part("кабель 4х1.5 24", 24.0), Part("кабель 3х1.5 16", 16.0)],
        ),
        (
            "медь 6-ку 15 м и 9-ку 15 м",
            [Part("медь 6-ку 15 м", 15.0), Part("медь 9-ку 15 м", 15.0)],
        ),
    ],
)
def test_split_by_features(phrase: str, parts: list[Part]) -> None:
    assert split_by_features(phrase) == parts


@pytest.mark.parametrize(
    "phrase", ["кабель 3 на 2,5 — 56 м", "отводов 90 на 25-й 4 шт", "медь 6-ку 15 м"]
)
def test_nothing_to_split(phrase: str) -> None:
    assert split_by_features(phrase) is None


def test_bare_numbers() -> None:
    assert bare_numbers("медь на 1/2 дюйма 10 м", {10.0}) == []  # дробь и объём не считаем
    assert bare_numbers("переходной тройник 25-20-25 две штуки", {2.0}) == [25.0, 20.0]
    assert bare_numbers("покрасили стену 20 м2", {20.0}) == []


@pytest.mark.parametrize(
    ("phrase", "qty", "key"),
    [
        ("12 хомутов", 12, "хомутов"),
        ("хомутов 12 штук", 12, "хомутов"),
        ("поставили 12 хомутов", 12, "хомутов"),  # глагол не входит в ключ
        ("хомуты — 12", 12, "хомуты"),
        ("тройник 25 — 3 шт", 3, "тройник 25"),  # 25 — размер, он остаётся
        ("медь 6-ку 15 м", 15, "медь 6-ку"),
        ("проложили кабель-канал 25 на 16", None, "кабель-канал 25х16"),
        ("смонтировали материал 3 шт", 3, "материал"),  # «-ал» — не глагол
        ("", None, ""),
    ],
)
def test_synonym_key(phrase: str, qty: float | None, key: str) -> None:
    assert synonym_key(phrase, qty) == key
