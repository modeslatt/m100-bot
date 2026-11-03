"""Адреса, формулы и запросы к таблице — чистые функции, без сети."""

import math
from datetime import date
from typing import Any

import pytest

from m100bot.sheets.addressing import (
    CellNotNumericError,
    build_row_link,
    cell,
    col_index,
    col_letter,
    column_range,
    column_values,
    compute_changes,
    date_to_serial,
    extend_sum_formula,
    extend_sum_formulas,
    extend_sum_requests,
    find_shifted_rows,
    insert_date_column_requests,
    parse_synonyms,
    plan_date_column,
    sheet_value,
    two_column_values,
)
from m100bot.sheets.catalog import Catalog, parse_date_columns

# ─────────────── Буквы столбцов ───────────────


@pytest.mark.parametrize(
    ("col", "letters"),
    [(1, "A"), (9, "I"), (10, "J"), (26, "Z"), (27, "AA"), (49, "AW"), (50, "AX"), (53, "BA"),
     (85, "CG"), (104, "CZ"), (105, "DA"), (702, "ZZ"), (703, "AAA")],
)  # fmt: skip
def test_col_letter_and_back(col: int, letters: str) -> None:
    assert col_letter(col) == letters
    assert col_index(letters) == col
    assert col_index(letters.lower()) == col


@pytest.mark.parametrize("bad", [0, -1])
def test_col_letter_rejects_non_positive(bad: int) -> None:
    with pytest.raises(ValueError):
        col_letter(bad)


@pytest.mark.parametrize("bad", ["", "A1", "1", "А"])  # последняя «А» — кириллица
def test_col_index_rejects_non_letters(bad: str) -> None:
    with pytest.raises(ValueError):
        col_index(bad)


def test_cell_and_range() -> None:
    assert cell(17, 50) == "AX17"
    assert column_range(50, 5, 71) == "AX5:AX71"


# ─────────────── Даты ───────────────


@pytest.mark.parametrize(
    "d", [date(2026, 8, 19), date(2026, 9, 30), date(2026, 11, 2), date(2027, 1, 1)]
)
def test_serial_roundtrips_through_catalog_parser(d: date) -> None:
    """Дата, записанная нами числом, читается парсером справочника как та же дата."""
    header = [[], [], [None] * 9 + [date_to_serial(d)]]
    columns, last = parse_date_columns(header)
    assert columns == {d: [10]} and last == 10


def test_known_serial() -> None:
    assert date_to_serial(date(1899, 12, 30)) == 0
    assert date_to_serial(date(2026, 8, 19)) == 46253


# ─────────────── Куда писать дату ───────────────


def test_plan_existing_date() -> None:
    plan = plan_date_column({date(2026, 9, 1): [12]}, 49, date(2026, 9, 1))
    assert (plan.col, plan.exists, plan.duplicates, plan.extend_sum_to) == (12, True, (), None)


def test_plan_duplicate_date_takes_first() -> None:
    """Ловушка образца: 30.09 стоит и в AV3, и в AW3 — пишем в первый."""
    plan = plan_date_column({date(2026, 9, 30): [48, 49]}, 49, date(2026, 9, 30))
    assert (plan.col, plan.exists, plan.duplicates) == (48, True, (48, 49))


def test_plan_new_date_goes_after_last() -> None:
    plan = plan_date_column({date(2026, 9, 30): [49]}, 49, date(2026, 11, 2))
    assert (plan.col, plan.exists, plan.extend_sum_to) == (50, False, None)


def test_plan_first_date_ever_starts_at_j() -> None:
    plan = plan_date_column({}, 9, date(2026, 11, 2))
    assert plan.col == 10 and not plan.exists


@pytest.mark.parametrize(
    ("last_col", "new_col", "extend_to"),
    [(102, 103, None), (103, 104, None), (104, 105, 105), (110, 111, 111)],
)
def test_plan_sum_boundary_cz(last_col: int, new_col: int, extend_to: int | None) -> None:
    """CZ — 104-й столбец: новый столбец правее него не попадает в SUM(J:CZ)."""
    plan = plan_date_column({date(2026, 1, 1): [last_col]}, last_col, date(2026, 6, 1))
    assert (plan.col, plan.extend_sum_to) == (new_col, extend_to)


# ─────────────── Формулы SUM ───────────────


@pytest.mark.parametrize(
    ("formula", "new_last", "expected"),
    [
        ("=SUM(J5:CZ5)", 105, "=SUM(J5:DA5)"),
        ("=SUM(J5:CZ5)", 130, "=SUM(J5:DZ5)"),
        ("=SUM(J5:DA5)", 106, "=SUM(J5:DB5)"),  # уже растянутую растягиваем дальше
        ("=SUM(J5:CZ5)", 104, None),  # диапазон и так достаточен
        ("=SUM(J5:DA5)", 105, None),
        ("=sum(j5:cz5)", 105, "=SUM(J5:DA5)"),
        ("=SUM(J52:CZ52)", 105, "=SUM(J52:DA52)"),
        ("=SUM(J5:CZ6)", 105, None),  # разные строки — не наша формула
        ("=SUM(H5:H51)", 105, None),  # другой диапазон
        ("=E5*D5", 105, None),
        ("5", 105, None),
        ("", 105, None),
    ],
)
def test_extend_sum_formula(formula: str, new_last: int, expected: str | None) -> None:
    assert extend_sum_formula(formula, new_last) == expected


def test_extend_sum_formulas_only_changed_rows() -> None:
    formulas = {5: "=SUM(J5:CZ5)", 6: "=E6*D6", 7: "=SUM(J7:DA7)", 52: "=SUM(J52:CZ52)"}
    assert extend_sum_formulas(formulas, 105) == {5: "=SUM(J5:DA5)", 52: "=SUM(J52:DA52)"}


# ─────────────── Запросы batchUpdate ───────────────


def test_insert_date_column_requests() -> None:
    insert, write = insert_date_column_requests(1000, 50, date(2026, 11, 2))
    assert insert == {
        "insertDimension": {
            "range": {"sheetId": 1000, "dimension": "COLUMNS", "startIndex": 49, "endIndex": 50},
            "inheritFromBefore": True,
        }
    }
    update = write["updateCells"]
    assert update["start"] == {"sheetId": 1000, "rowIndex": 2, "columnIndex": 49}  # AX3
    assert update["rows"][0]["values"][0]["userEnteredValue"] == {
        "numberValue": date_to_serial(date(2026, 11, 2))
    }
    assert update["rows"][1]["values"][0]["userEnteredValue"] == {"stringValue": "объем"}
    assert update["fields"] == "userEnteredValue"


def test_extend_sum_requests_target_column_i() -> None:
    reqs = extend_sum_requests(1000, {52: "=SUM(J52:DA52)", 5: "=SUM(J5:DA5)"})
    starts = [r["updateCells"]["start"] for r in reqs]
    assert starts == [
        {"sheetId": 1000, "rowIndex": 4, "columnIndex": 8},  # I5, по возрастанию строк
        {"sheetId": 1000, "rowIndex": 51, "columnIndex": 8},  # I52
    ]
    assert reqs[0]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] == {
        "formulaValue": "=SUM(J5:DA5)"
    }


# ─────────────── Объёмы ───────────────


def test_compute_add_to_empty_and_existing() -> None:
    changes = compute_changes({18: 5, 19: ""}, {17: 15, 18: 10, 19: 2.5})
    assert [(c.row, c.old, c.new) for c in changes] == [(17, 0, 15), (18, 5, 15), (19, 0, 2.5)]


def test_compute_set_replaces() -> None:
    [change] = compute_changes({17: 15}, {17: 4}, "set")
    assert (change.old, change.new) == (15, 4)


def test_compute_zero() -> None:
    assert compute_changes({17: 15}, {17: 0}, "add") == []  # прибавить 0 — нечего писать
    [change] = compute_changes({17: 15}, {17: 0}, "set")
    assert (change.old, change.new) == (15, 0)
    assert sheet_value(change.new) == ""  # ноль очищает ячейку


def test_compute_rounds_float_noise() -> None:
    [change] = compute_changes({17: 0.1}, {17: 0.2})
    assert change.new == 0.3  # не 0.30000000000000004


def test_compute_text_number_in_cell() -> None:
    [change] = compute_changes({17: "5"}, {17: 1})
    assert change.new == 6


def test_compute_orders_by_row() -> None:
    assert [c.row for c in compute_changes({}, {19: 1, 17: 1, 18: 1})] == [17, 18, 19]


@pytest.mark.parametrize("bad", [-1, math.nan, math.inf, "5", None, True])
def test_compute_rejects_bad_quantity(bad: Any) -> None:
    with pytest.raises(ValueError):
        compute_changes({}, {17: bad})


def test_compute_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        compute_changes({}, {17: 1}, "append")  # type: ignore[arg-type]


@pytest.mark.parametrize("garbage", ["ошибка", "n/a", True])
def test_compute_refuses_to_overwrite_text(garbage: Any) -> None:
    with pytest.raises(CellNotNumericError) as e:
        compute_changes({17: garbage}, {17: 1})
    assert e.value.row == 17


@pytest.mark.parametrize(
    ("value", "expected"), [(0, ""), (15.0, 15), (0.5, 0.5), (2.35, 2.35), (100, 100)]
)
def test_sheet_value(value: float, expected: Any) -> None:
    result = sheet_value(value)
    assert result == expected
    assert isinstance(result, int) == isinstance(expected, int)


def test_column_values_handles_trimmed_tail() -> None:
    # Sheets отдаёт [[1], [], [3]] и обрезает хвост: строки 8+ просто отсутствуют
    assert column_values(5, [[1], [], [3]]) == {5: 1, 6: None, 7: 3}
    assert two_column_values(5, [[13, "Фреон"], [14]]) == {5: (13, "Фреон"), 6: (14, None)}


# ─────────────── Сдвиг строк ───────────────


def _ab(sample_rows: list[list[Any]], first: int, last: int) -> dict[int, tuple[Any, Any]]:
    return two_column_values(first, [r[:2] for r in sample_rows[first - 1 : last]])


def test_no_shift_when_sheet_matches_catalog(
    catalog: Catalog, sample_rows: list[list[Any]]
) -> None:
    ab = _ab(sample_rows, 5, 71)
    assert find_shifted_rows(catalog.positions, ab) == []


def test_shift_detected_when_row_inserted(catalog: Catalog, sample_rows: list[list[Any]]) -> None:
    """Вставили строку выше 17-й: теперь в 17-й стоит то, что раньше было в 16-й."""
    shifted = sample_rows[:16] + [["x", "Новая работа"]] + sample_rows[16:]
    ab = _ab(shifted, 17, 19)
    assert find_shifted_rows([p for p in catalog.positions if p.row in (17, 18, 19)], ab) == [
        17,
        18,
        19,
    ]


def test_shift_detected_by_number_alone(catalog: Catalog, sample_rows: list[list[Any]]) -> None:
    """У строк 48 и 49 одинаковые названия — различает их только № в колонке A."""
    ab = _ab(sample_rows, 48, 49)
    ab[48], ab[49] = ab[49], ab[48]  # поменяли местами
    assert find_shifted_rows([p for p in catalog.positions if p.row in (48, 49)], ab) == [48, 49]


def test_row_without_number_is_not_a_shift(catalog: Catalog, sample_rows: list[list[Any]]) -> None:
    """Строка 71 в образце без № в колонке A — это норма, а не сдвиг."""
    assert find_shifted_rows([catalog.by_row(71)], _ab(sample_rows, 71, 71)) == []  # type: ignore[list-item]


# ─────────────── Синонимы ───────────────


def test_parse_synonyms_basic(catalog: Catalog) -> None:
    positions = {p.row: p for p in catalog.positions}
    rows = [
        ["Фраза мастера", "Строка позиции", "Название"],
        ["Шестёрка медь", 17],
        ["гофра  20", "64", catalog.by_row(64).name],
        ["", 5],
        [],
    ]
    synonyms, warnings = parse_synonyms(rows, positions)
    assert synonyms == {"шестерка медь": 17, "гофра 20": 64}  # ключи нормализованы
    assert warnings == []


def test_parse_synonyms_problems_are_warnings(catalog: Catalog) -> None:
    positions = {p.row: p for p in catalog.positions}
    rows = [
        ["Фраза", "Строка"],
        ["без номера"],
        ["не число", "abc"],
        ["вне сметы", 999],
        ["это заголовок раздела", 4],  # строка 4 — раздел «ОВ 3», не позиция
        ["сдвиг", 17, "Совсем другая позиция"],
        ["дубль", 17],
        ["дубль", 18],
        ["ок", 19],
    ]
    synonyms, warnings = parse_synonyms(rows, positions)
    assert synonyms == {"дубль": 17, "ок": 19}
    assert len(warnings) == 6
    assert any("сдвинули" in w for w in warnings)
    assert any("уже указывает на 17" in w for w in warnings)


def test_parse_synonyms_without_catalog_trusts_rows() -> None:
    synonyms, warnings = parse_synonyms([["a", 17], ["b", 999]])
    assert synonyms == {"a": 17, "b": 999} and warnings == []


# ─────────────── Ссылка ───────────────


def test_build_row_link() -> None:
    assert (
        build_row_link("ABC", 1000, 17)
        == "https://docs.google.com/spreadsheets/d/ABC/edit#gid=1000&range=B17"
    )
    assert build_row_link("ABC", None, 17).endswith("/edit#range=B17")
