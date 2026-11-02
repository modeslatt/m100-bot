"""Справочник позиций и столбцы дат из «сырых» значений листа.

Работает со списком строк (list[list[Any]]), поэтому один и тот же код разбирает
и ответ gspread (`worksheet.get_values(value_render_option="UNFORMATTED_VALUE")`),
и локальный xlsx в тестах. Нумерация строк/колонок — как в таблице (с 1).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from m100bot.matching.features import Features, extract_features
from m100bot.matching.normalize import normalize_text, normalize_unit, parse_number

# Раскладка листа (см. CLAUDE.md → «Структура таблицы»)
COL_NUM, COL_NAME, COL_UNIT, COL_QTY, COL_PRICE = 1, 2, 3, 4, 5
COL_DONE = 9  # I «Выполненный объем» (=SUM(J:CZ))
FIRST_DATE_COL = 10  # J
DATE_ROW = 3
SUBHEADER_ROW = 4  # «объем» под каждой датой
FIRST_DATA_ROW = 5
SUM_LAST_COL = 104  # CZ — правая граница формулы SUM(J:CZ)

_SECTION = re.compile(r"^(ов|эм)\s*\d+$")
_EXCEL_EPOCH = date(1899, 12, 30)


@dataclass(frozen=True)
class Position:
    row: int
    number: int | None
    name: str
    unit: str
    planned: float
    price: float | None
    section: str | None
    group: str | None
    features: Features = field(compare=False, hash=False)

    @property
    def norm_name(self) -> str:
        return normalize_text(self.name)


@dataclass
class Catalog:
    positions: list[Position]
    date_columns: dict[date, list[int]]  # дата → номера колонок (бывают дубликаты)
    last_col: int  # последняя занятая колонка в строке дат

    def by_row(self, row: int) -> Position | None:
        return next((p for p in self.positions if p.row == row), None)

    def column_for(self, d: date) -> int | None:
        cols = self.date_columns.get(d)
        return cols[0] if cols else None

    def duplicate_dates(self) -> dict[date, list[int]]:
        return {d: c for d, c in self.date_columns.items() if len(c) > 1}


def _cell(row: list[Any], col: int) -> Any:
    return row[col - 1] if len(row) >= col else None


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):  # серийный номер даты Google/Excel
        return _EXCEL_EPOCH + timedelta(days=int(value))
    s = str(value).strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_date_columns(rows: list[list[Any]]) -> tuple[dict[date, list[int]], int]:
    header = rows[DATE_ROW - 1] if len(rows) >= DATE_ROW else []
    result: dict[date, list[int]] = {}
    last = FIRST_DATE_COL - 1
    for col in range(FIRST_DATE_COL, len(header) + 1):
        d = _to_date(_cell(header, col))
        if d:
            result.setdefault(d, []).append(col)
            last = col
    return result, last


def parse_catalog(rows: list[list[Any]]) -> Catalog:
    positions: list[Position] = []
    section: str | None = None
    group: str | None = None

    # с 4-й строки: в B4 стоит раздел «ОВ 3»
    for idx in range(SUBHEADER_ROW, len(rows) + 1):
        row = rows[idx - 1]
        name = _cell(row, COL_NAME)
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        norm = normalize_text(name)

        if _SECTION.match(norm):
            section, group = name, None
            continue
        if norm.startswith(("итого", "всего")):
            continue

        unit = normalize_unit(
            _cell(row, COL_UNIT) if isinstance(_cell(row, COL_UNIT), str) else None
        )
        qty = parse_number(_cell(row, COL_QTY))
        if not unit or qty is None:
            group = name  # подзаголовок группы: «Материалы для сист К1-К7» и т.п.
            continue

        num = parse_number(_cell(row, COL_NUM))
        positions.append(
            Position(
                row=idx,
                number=int(num) if num is not None else None,
                name=name,
                unit=unit,
                planned=qty,
                price=parse_number(_cell(row, COL_PRICE)),
                section=section,
                group=group,
                features=extract_features(name),
            )
        )

    date_columns, last_col = parse_date_columns(rows)
    return Catalog(positions=positions, date_columns=date_columns, last_col=last_col)


def done_volume(rows: list[list[Any]], row: int, last_col: int) -> float:
    """Выполненный объём, посчитанный по ячейкам дат (не доверяем формуле в I)."""
    r = rows[row - 1]
    return sum(parse_number(_cell(r, c)) or 0.0 for c in range(FIRST_DATE_COL, last_col + 1))
