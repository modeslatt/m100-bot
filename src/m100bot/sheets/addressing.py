"""Адреса ячеек, формулы и запросы к таблице — чистые функции без сети.

Вся арифметика столбцов и строк живёт здесь, чтобы `SheetsClient` только вызывал gspread,
а ошибки вроде «столбец ушёл правее CZ» ловились обычными тестами.
Нумерация строк и столбцов — как в таблице, с 1.
"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from m100bot.matching.normalize import normalize_text, parse_number
from m100bot.sheets.catalog import (
    COL_DONE,
    DATE_ROW,
    EXCEL_EPOCH,
    FIRST_DATE_COL,
    SUM_LAST_COL,
    Position,
)

VOLUME_LABEL = "объем"  # подпись под каждой датой (строка 4)
Mode = Literal["add", "set"]


# ─────────────── Буквы столбцов и A1-адреса ───────────────


def col_letter(col: int) -> str:
    """1 → A, 26 → Z, 27 → AA, 104 → CZ."""
    if col < 1:
        raise ValueError(f"номер столбца должен быть ≥ 1, получено {col}")
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def col_index(letters: str) -> int:
    """A → 1, AA → 27, CZ → 104."""
    if not re.fullmatch(r"[A-Za-z]+", letters):
        raise ValueError(f"не буквы столбца: {letters!r}")
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - ord("A") + 1
    return n


def cell(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def column_range(col: int, first_row: int, last_row: int) -> str:
    """AX5:AX71."""
    letter = col_letter(col)
    return f"{letter}{first_row}:{letter}{last_row}"


def date_to_serial(d: date) -> int:
    """Серийный номер даты в Google Sheets: 2026-08-19 → 46253."""
    return (d - EXCEL_EPOCH).days


def build_row_link(spreadsheet_id: str, gid: int | None, row: int, col: str = "B") -> str:
    """Ссылка, которая открывает таблицу на ячейке позиции. gid неизвестен → без него."""
    fragment = f"range={col}{row}"
    if gid is not None:
        fragment = f"gid={gid}&{fragment}"
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#{fragment}"


# ─────────────── Столбец даты ───────────────


@dataclass(frozen=True)
class DatePlan:
    col: int  # столбец, куда пишем объёмы за дату
    exists: bool  # столбец уже есть (иначе его надо создать)
    duplicates: tuple[int, ...] = ()  # все столбцы с этой датой, если их больше одного
    extend_sum_to: int | None = None  # до какого столбца растянуть SUM(J:CZ) в колонке I


def plan_date_column(date_columns: dict[date, list[int]], last_col: int, d: date) -> DatePlan:
    """Где писать объёмы за дату d.

    Дата есть → первый из её столбцов (при дубликате остальные игнорируем).
    Даты нет → новый столбец сразу после последней даты. Если он правее границы формулы
    `SUM(J:CZ)`, итог в колонке I его не увидит — просим растянуть формулы.
    """
    cols = date_columns.get(d)
    if cols:
        return DatePlan(col=cols[0], exists=True, duplicates=tuple(cols) if len(cols) > 1 else ())
    col = max(last_col, FIRST_DATE_COL - 1) + 1
    return DatePlan(col=col, exists=False, extend_sum_to=col if col > SUM_LAST_COL else None)


_SUM_FORMULA = re.compile(rf"^=SUM\({col_letter(FIRST_DATE_COL)}(\d+):([A-Z]{{1,3}})(\d+)\)$")


def extend_sum_formula(formula: str, new_last_col: int) -> str | None:
    """`=SUM(J5:CZ5)` → `=SUM(J5:DA5)`, если конец диапазона левее new_last_col.

    None — формула другого вида (её не трогаем) или диапазон и так достаточно широкий.
    """
    m = _SUM_FORMULA.match(formula.strip().upper())
    if not m or m.group(1) != m.group(3):
        return None
    if col_index(m.group(2)) >= new_last_col:
        return None
    return f"=SUM({col_letter(FIRST_DATE_COL)}{m.group(1)}:{col_letter(new_last_col)}{m.group(1)})"


def extend_sum_formulas(formulas: dict[int, str], new_last_col: int) -> dict[int, str]:
    """Строка → новая формула, только для тех строк, где формулу действительно надо менять."""
    out: dict[int, str] = {}
    for row, formula in formulas.items():
        new = extend_sum_formula(formula, new_last_col)
        if new is not None:
            out[row] = new
    return out


def insert_date_column_requests(
    sheet_id: int, col: int, d: date, label: str = VOLUME_LABEL
) -> list[dict[str, Any]]:
    """Запросы spreadsheets.batchUpdate: вставить столбец и записать дату и «объем».

    Формат и ширина наследуются от столбца слева (inheritFromBefore). Дата пишется
    серийным числом — формат даты уже стоит в соседних ячейках, поэтому она отобразится
    как дата и не зависит от региональных настроек таблицы.
    """
    idx = col - 1
    return [
        {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                },
                "inheritFromBefore": True,
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": DATE_ROW - 1, "columnIndex": idx},
                "rows": [
                    {"values": [{"userEnteredValue": {"numberValue": date_to_serial(d)}}]},
                    {"values": [{"userEnteredValue": {"stringValue": label}}]},
                ],
                "fields": "userEnteredValue",
            }
        },
    ]


def extend_sum_requests(sheet_id: int, formulas: dict[int, str]) -> list[dict[str, Any]]:
    """Запросы batchUpdate: записать новые формулы в колонку I (по запросу на строку)."""
    return [
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": COL_DONE - 1},
                "rows": [{"values": [{"userEnteredValue": {"formulaValue": formula}}]}],
                "fields": "userEnteredValue",
            }
        }
        for row, formula in sorted(formulas.items())
    ]


# ─────────────── Объёмы ───────────────


class CellNotNumericError(ValueError):
    """В ячейке объёма стоит текст, который нельзя тихо перезаписать."""

    def __init__(self, row: int, value: Any) -> None:
        super().__init__(f"строка {row}: в ячейке не число ({value!r})")
        self.row = row


@dataclass(frozen=True)
class CellChange:
    row: int
    old: float
    new: float


def column_values(first_row: int, values: list[list[Any]]) -> dict[int, Any]:
    """Ответ Sheets по одному столбцу (хвост пустых строк обрезан) → {строка: значение}."""
    return {first_row + i: (r[0] if r else None) for i, r in enumerate(values)}


def two_column_values(first_row: int, values: list[list[Any]]) -> dict[int, tuple[Any, Any]]:
    """То же для двух столбцов (A:B): {строка: (A, B)}."""
    out: dict[int, tuple[Any, Any]] = {}
    for i, r in enumerate(values):
        out[first_row + i] = (r[0] if len(r) > 0 else None, r[1] if len(r) > 1 else None)
    return out


def _current_number(row: int, value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        raise CellNotNumericError(row, value)
    if isinstance(value, int | float):
        return float(value)
    parsed = parse_number(value)
    if parsed is None:
        raise CellNotNumericError(row, value)
    return parsed


def compute_changes(
    current: dict[int, Any], volumes: dict[int, float], mode: Mode = "add"
) -> list[CellChange]:
    """Что записать в ячейки даты.

    add — прибавить к текущему значению (пустая ячейка = 0); нулевой объём пропускаем.
    set — заменить значение. Объём должен быть конечным числом ≥ 0.
    """
    if mode not in ("add", "set"):
        raise ValueError(f"mode должен быть 'add' или 'set', получено {mode!r}")
    changes: list[CellChange] = []
    for row, qty in sorted(volumes.items()):
        if isinstance(qty, bool) or not isinstance(qty, int | float):
            raise ValueError(f"строка {row}: объём должен быть числом, получено {qty!r}")
        if not math.isfinite(qty) or qty < 0:
            raise ValueError(f"строка {row}: недопустимый объём {qty!r}")
        if mode == "add" and qty == 0:
            continue
        old = _current_number(row, current.get(row))
        new = old + qty if mode == "add" else float(qty)
        changes.append(CellChange(row, old, round(new, 6)))
    return changes


def sheet_value(value: float) -> int | float | str:
    """Значение для ячейки: целое без «.0»; ноль очищает ячейку (пустые ячейки — норма)."""
    if value == 0:
        return ""
    return int(value) if float(value).is_integer() else value


def find_shifted_rows(positions: Iterable[Position], ab: dict[int, tuple[Any, Any]]) -> list[int]:
    """Строки, где № (колонка A) или название (B) в таблице уже не те, что в справочнике.

    Так ловим ситуацию «кто-то вставил или удалил строку, пока отчёт ждал подтверждения»:
    объём иначе тихо ушёл бы в соседнюю позицию.
    """
    bad: list[int] = []
    for p in positions:
        num_cell, name_cell = ab.get(p.row, (None, None))
        same_name = isinstance(name_cell, str) and normalize_text(name_cell) == p.norm_name
        num = parse_number(num_cell)
        same_num = num == (float(p.number) if p.number is not None else None)
        if not (same_name and same_num):
            bad.append(p.row)
    return bad


# ─────────────── Лист «Синонимы» ───────────────

SYNONYMS_HEADER = ["Фраза мастера", "Строка позиции", "Название позиции (для проверки)"]


def _to_row_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_synonyms(
    rows: list[list[Any]], positions: dict[int, Position] | None = None
) -> tuple[dict[str, int], list[str]]:
    """Лист «Синонимы» (A — фраза, B — строка позиции, C — название) → ({фраза: строка}, замечания).

    Ключи нормализованы так же, как фразы мастера. Некорректные строки пропускаются с
    замечанием, а не роняют бота. Если задан справочник, проверяем, что строка — позиция
    сметы и что название в C (если заполнено) совпадает: иначе таблицу сдвинули, и
    синоним теперь указывал бы на другую позицию.
    """
    result: dict[str, int] = {}
    warnings: list[str] = []
    for i, r in enumerate(rows, start=1):
        phrase = str(r[0]).strip() if r and r[0] is not None else ""
        if not phrase:
            continue
        row = _to_row_number(r[1]) if len(r) > 1 else None
        if row is None:
            if i > 1:  # первая строка — заголовок
                warnings.append(f"Синонимы, строка {i}: «{phrase}» без номера строки позиции")
            continue
        key = normalize_text(phrase)
        if positions is not None:
            pos = positions.get(row)
            if pos is None:
                warnings.append(f"Синонимы, строка {i}: «{phrase}» → {row}, но это не позиция")
                continue
            title = r[2] if len(r) > 2 else None
            if title not in (None, "") and normalize_text(str(title)) != pos.norm_name:
                warnings.append(
                    f"Синонимы, строка {i}: «{phrase}» → {row}, но там теперь другая позиция "
                    "(таблицу сдвинули?) — пропущено"
                )
                continue
        if key in result and result[key] != row:
            warnings.append(f"Синонимы, строка {i}: «{phrase}» уже указывает на {result[key]}")
            continue
        result[key] = row
    return result, warnings


def find_synonym_row(rows: list[list[Any]], phrase: str) -> int | None:
    """Номер строки листа «Синонимы» с этой фразой (после нормализации) или None."""
    key = normalize_text(phrase)
    for i, r in enumerate(rows, start=1):
        if i > 1 and r and r[0] is not None and normalize_text(str(r[0])) == key:
            return i
    return None
