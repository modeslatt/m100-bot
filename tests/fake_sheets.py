"""Имитация gspread для тестов SheetsClient: сетка ячеек, A1-адреса, batchUpdate.

Повторяет только то, что нужно клиенту, но с важными свойствами настоящих Sheets:
- пустые «хвосты» строк и пустые последние строки в ответе обрезаются;
- `=SUM(J5:CZ5)` считается при чтении «как значение» и остаётся строкой при чтении «как формула»;
- вставка столбца сдвигает ссылки в формулах, и диапазон, внутри которого вставили столбец,
  растягивается — а вставка сразу за его концом нет (так ведут себя Google Sheets).
Сбои подставляются через `faults` (до выполнения) и `faults_after` (после — «потерянный ответ»).
"""

import re
from collections import Counter
from datetime import date, datetime
from typing import Any

from gspread.exceptions import SpreadsheetNotFound, WorksheetNotFound

from m100bot.sheets.addressing import col_index, col_letter, date_to_serial

_SUM = re.compile(r"^=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)$")
_CELL = re.compile(r"^([A-Z]+)(\d+)$")
_RANGE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$")
_ROWS = re.compile(r"^(\d+):(\d+)$")


class FakeWorksheet:
    def __init__(self, sh: "FakeSpreadsheet", title: str, gid: int, rows: list[list[Any]]) -> None:
        self._sh = sh
        self.title = title
        self.id = gid
        self.grid: list[list[Any]] = [list(r) for r in rows] or [[None]]
        self._pad()

    # ── размеры ──

    def _pad(self) -> None:
        width = max(len(r) for r in self.grid)
        for r in self.grid:
            r.extend([None] * (width - len(r)))

    @property
    def row_count(self) -> int:
        return len(self.grid)

    @property
    def col_count(self) -> int:
        return len(self.grid[0])

    def _grow(self, row: int, col: int) -> None:
        while len(self.grid) < row:
            self.grid.append([None] * self.col_count)
        if col > self.col_count:
            for r in self.grid:
                r.extend([None] * (col - len(r)))

    def set_cell(self, row: int, col: int, value: Any) -> None:
        self._grow(row, col)
        self.grid[row - 1][col - 1] = value

    def get_cell(self, row: int, col: int) -> Any:
        if row > self.row_count or col > self.col_count:
            return None
        return self.grid[row - 1][col - 1]

    # ── чтение ──

    def _bounds(self, rng: str | None) -> tuple[int, int, int, int]:
        if rng is None:
            return 1, self.row_count, 1, self.col_count
        rng = rng.split("!")[-1]
        if m := _ROWS.match(rng):
            return int(m[1]), int(m[2]), 1, self.col_count
        if m := _RANGE.match(rng):
            return int(m[2]), int(m[4]), col_index(m[1]), col_index(m[3])
        if m := _CELL.match(rng):
            return int(m[2]), int(m[2]), col_index(m[1]), col_index(m[1])
        raise AssertionError(f"имитация не умеет диапазон {rng!r}")

    def _render(self, row: int, col: int, option: Any) -> Any:
        v = self.get_cell(row, col)
        if v is None:
            return ""
        as_value = "formula" not in str(option).lower()
        if as_value and isinstance(v, str) and (m := _SUM.match(v)):
            c0, c1 = col_index(m[1]), col_index(m[3])
            return sum(
                x
                for r in range(int(m[2]), int(m[4]) + 1)
                for c in range(c0, c1 + 1)
                if isinstance(x := self.get_cell(r, c), int | float)
            )
        return v

    def get_values(self, range_name: str | None = None, value_render_option: Any = None, **_: Any):
        self._sh.fault("get_values")
        r0, r1, c0, c1 = self._bounds(range_name)
        out = [
            [self._render(r, c, value_render_option) for c in range(c0, c1 + 1)]
            for r in range(r0, min(r1, self.row_count) + 1)
        ]
        for row in out:  # Sheets обрезает пустые хвосты строк и пустые последние строки
            while row and row[-1] == "":
                row.pop()
        while out and not out[-1]:
            out.pop()
        return out

    def batch_get(self, ranges: list[str], value_render_option: Any = None, **_: Any):
        self._sh.fault("batch_get")
        return [self.get_values(r, value_render_option) for r in ranges]

    # ── запись ──

    def batch_update(self, data: list[dict[str, Any]], raw: bool = True, **_: Any) -> dict:
        self._sh.fault("ws.batch_update")
        self._sh.value_writes.append(data)
        for item in data:
            self._write_block(item["range"], item["values"])
        self._sh.fault_after("ws.batch_update")
        return {}

    def update(self, values: list[list[Any]], range_name: str, raw: bool = True, **_: Any):
        self._sh.fault("ws.update")
        self._write_block(range_name, values)
        return {}

    def append_row(self, values: list[Any], table_range: str | None = None, **_: Any):
        self._sh.fault("ws.append_row")
        used = [i for i, r in enumerate(self.grid, start=1) if any(v not in (None, "") for v in r)]
        row = (used[-1] if used else 0) + 1
        for j, v in enumerate(values, start=1):
            self.set_cell(row, j, v)
        return {}

    def _write_block(self, rng: str, values: list[list[Any]]) -> None:
        r0, _, c0, _ = self._bounds(rng)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self.set_cell(r0 + i, c0 + j, None if v == "" else v)

    # ── вставка столбца ──

    def insert_column(self, index0: int) -> None:
        for r in self.grid:
            r.insert(index0, None)
        ins = index0 + 1
        for r in self.grid:
            for j, v in enumerate(r):
                if isinstance(v, str) and (m := _SUM.match(v)):
                    c0, c1 = col_index(m[1]), col_index(m[3])
                    c0 += c0 >= ins
                    c1 += c1 >= ins
                    r[j] = f"=SUM({col_letter(c0)}{m[2]}:{col_letter(c1)}{m[4]})"

    def insert_row(self, index0: int) -> None:
        self.grid.insert(index0, [None] * self.col_count)


class FakeSpreadsheet:
    def __init__(self, sheets: dict[str, list[list[Any]]]) -> None:
        self.sheets = {
            title: FakeWorksheet(self, title, gid=1000 + i, rows=rows)
            for i, (title, rows) in enumerate(sheets.items())
        }
        self.calls: Counter[str] = Counter()
        self.faults: dict[str, list[Exception]] = {}
        self.faults_after: dict[str, list[Exception]] = {}
        self.value_writes: list[list[dict[str, Any]]] = []
        self.structure_updates: list[list[dict[str, Any]]] = []

    def fault(self, name: str) -> None:
        self.calls[name] += 1
        if queue := self.faults.get(name):
            raise queue.pop(0)

    def fault_after(self, name: str) -> None:
        if queue := self.faults_after.get(name):
            raise queue.pop(0)

    def worksheet(self, title: str) -> FakeWorksheet:
        self.fault("worksheet")
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        self.fault("add_worksheet")
        self.sheets[title] = FakeWorksheet(
            self, title, gid=1000 + len(self.sheets), rows=[[None] * cols for _ in range(rows)]
        )
        return self.sheets[title]

    def batch_update(self, body: dict[str, Any]) -> dict:
        self.fault("batch_update")
        requests = body["requests"]
        self.structure_updates.append(requests)
        for req in requests:
            ((kind, spec),) = req.items()
            ws = next(w for w in self.sheets.values() if w.id == _sheet_id(kind, spec))
            if kind == "insertDimension":
                assert spec["range"]["dimension"] == "COLUMNS"
                assert spec["inheritFromBefore"] is True
                ws.insert_column(spec["range"]["startIndex"])
            elif kind == "updateCells":
                start = spec["start"]
                for i, row in enumerate(spec["rows"]):
                    for j, v in enumerate(row["values"]):
                        ((_, value),) = v["userEnteredValue"].items()
                        ws.set_cell(start["rowIndex"] + 1 + i, start["columnIndex"] + 1 + j, value)
            else:
                raise AssertionError(f"имитация не знает запрос {kind!r}")
        self.fault_after("batch_update")
        return {}


def _sheet_id(kind: str, spec: dict[str, Any]) -> int:
    return spec["range"]["sheetId"] if kind == "insertDimension" else spec["start"]["sheetId"]


class FakeGspread:
    def __init__(self, spreadsheet: FakeSpreadsheet, spreadsheet_id: str = "SHEET_ID") -> None:
        self.spreadsheet = spreadsheet
        self.spreadsheet_id = spreadsheet_id

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        self.spreadsheet.fault("open_by_key")
        if key != self.spreadsheet_id:
            raise SpreadsheetNotFound(None)  # type: ignore[arg-type]
        return self.spreadsheet


# ─────────────── Данные: образец и граничные случаи ───────────────


def sample_sheet_rows(rows: list[list[Any]], *, dates_to_col: int | None = None) -> list[list[Any]]:
    """Строки образца: даты-datetime → серийные числа, как их отдаёт Sheets.

    dates_to_col — заполнить строку дат подряд от J до этого столбца (для граничных тестов);
    данные объёмов при этом очищаются.
    """
    out = [list(r) for r in rows]
    for row in out:
        for j, v in enumerate(row):
            if isinstance(v, datetime):
                row[j] = date_to_serial(v.date())
    if dates_to_col is not None:
        width = max(len(r) for r in out)
        for row in out[2:]:
            row[9:] = [None] * (width - 9)
        out[2].extend([None] * max(0, dates_to_col - len(out[2])))
        out[3].extend([None] * max(0, dates_to_col - len(out[3])))
        for c in range(10, dates_to_col + 1):
            out[2][c - 1] = date_to_serial(date(2026, 1, 1)) + (c - 10)
            out[3][c - 1] = "объем"
    return out


def make_gspread(rows: list[list[Any]], **kw: Any) -> FakeGspread:
    return FakeGspread(FakeSpreadsheet({"Лист1": sample_sheet_rows(rows, **kw)}))
