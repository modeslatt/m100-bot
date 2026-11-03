"""Google Sheets через gspread (сервисный аккаунт).

gspread синхронный — вызовы оборачиваем в asyncio.to_thread, чтобы не блокировать бота.
Вся арифметика адресов и формул — в `addressing.py` (чистые функции, покрыты тестами);
здесь только обмен с Google, повторы и перевод ошибок в понятные сообщения.

Повторы (tenacity) ставим вокруг блоков, безопасных к повтору «целиком»:
- создание столбца даты сначала смотрит, нет ли его уже, — повтор после потерянного ответа
  не вставит второй столбец;
- объёмы считаются один раз и пишутся абсолютными значениями — повтор записи тех же
  значений ничего не удваивает (а повтор «прочитать и прибавить» удвоил бы).
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import gspread
import requests.exceptions
from google.auth.exceptions import TransportError
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import ValueRenderOption
from loguru import logger
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from m100bot.matching.normalize import parse_number
from m100bot.sheets.addressing import (
    SYNONYMS_HEADER,
    CellChange,
    CellNotNumericError,
    Mode,
    RowId,
    build_row_link,
    cell,
    cell_numbers,
    col_letter,
    column_range,
    column_values,
    compute_changes,
    extend_sum_formulas,
    extend_sum_requests,
    find_shifted_rows,
    insert_date_column_requests,
    parse_synonyms,
    plan_date_column,
    row_id,
    sheet_value,
    two_column_values,
    validate_volumes,
)
from m100bot.sheets.catalog import (
    COL_DONE,
    COL_NAME,
    COL_NUM,
    DATE_ROW,
    FIRST_DATA_ROW,
    Catalog,
    parse_catalog,
    parse_date_columns,
)

if TYPE_CHECKING:
    from m100bot.config import SheetsSettings

_UNAVAILABLE_STATUS = {429, 500, 502, 503, 504}
_MSG_UNAVAILABLE = "Таблица недоступна. Попробуйте позже."


class SheetsError(RuntimeError):
    """Не удалось поработать с таблицей. Текст можно показать мастеру."""


class SheetChangedError(SheetsError):
    """Таблицу изменили (сдвинули строки), пока отчёт ждал подтверждения."""


class WriteConflictError(SheetsError):
    """Прерванную запись нельзя ни подтвердить, ни повторить: ячейки уже не «до» и не «после»."""


BeforeWrite = Callable[[list[CellChange]], Awaitable[None]]


def _is_transient(exc: BaseException) -> bool:
    """Временный сбой: сеть, таймаут, перегрузка Google — имеет смысл повторить."""
    if isinstance(exc, APIError):
        return exc.response.status_code in _UNAVAILABLE_STATUS
    return isinstance(
        exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout | TransportError
    )


class SheetsClient:
    def __init__(
        self,
        credentials_file: Path,
        spreadsheet_id: str,
        worksheet: str,
        synonyms_worksheet: str = "Синонимы",
        *,
        cache_ttl_s: float = 300.0,
        timeout_s: float = 30.0,
        attempts: int = 3,
        retry_wait_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        gspread_client: Any = None,
    ) -> None:
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._worksheet_name = worksheet
        self._synonyms_name = synonyms_worksheet
        self._cache_ttl_s = cache_ttl_s
        self._timeout_s = timeout_s
        self._attempts = attempts
        self._retry_wait_s = retry_wait_s
        self._clock = clock
        self._injected_client = gspread_client

        self._sh: Any = None
        self._ws: Any = None
        self._gid: int | None = None
        self._catalog: Catalog | None = None
        self._catalog_at: float | None = None
        self._synonyms: dict[str, int] | None = None
        self._synonyms_at: float | None = None
        self._lock = asyncio.Lock()  # одна изменяющая операция за раз

    @classmethod
    def from_settings(cls, s: "SheetsSettings") -> "SheetsClient":
        return cls(
            s.google_credentials_file,
            s.spreadsheet_id,
            s.worksheet_name,
            s.synonyms_worksheet_name,
        )

    # ─────────────── Публичный интерфейс ───────────────

    async def load_catalog(self, *, force: bool = False) -> Catalog:
        """Справочник позиций и столбцы дат. Кэш на `cache_ttl_s` (5 мин), force — читать заново."""
        if not force and self._catalog is not None and self._fresh(self._catalog_at):
            return self._catalog
        return await asyncio.to_thread(self._call, self._load_catalog_once)

    async def load_synonyms(self) -> dict[str, int]:
        """Лист «Синонимы»: {нормализованная фраза: строка позиции}. Листа нет — создаётся."""
        if self._synonyms is not None and self._fresh(self._synonyms_at):
            return self._synonyms
        catalog = await self.load_catalog()
        rows = await asyncio.to_thread(self._call, self._read_synonyms_once)
        synonyms, warnings = parse_synonyms(rows, {p.row: p for p in catalog.positions})
        for w in warnings:
            logger.warning(w)
        self._synonyms, self._synonyms_at = synonyms, self._clock()
        return synonyms

    async def ensure_date_column(self, d: date) -> int:
        """Номер столбца даты; если его нет — создаёт после последней даты.

        В новом столбце: дата в строке 3, «объем» в строке 4, формат от соседнего слева.
        Если новый столбец правее CZ, формулы `=SUM(J:CZ)` в колонке I растягиваются.
        """
        async with self._lock:
            try:
                return await asyncio.to_thread(self._call, self._ensure_once, d)
            finally:
                self.invalidate_cache()

    async def write_volumes(
        self,
        d: date,
        volumes: dict[int, float],
        *,
        mode: Mode = "add",
        expected: Mapping[int, RowId] | None = None,
        before_write: BeforeWrite | None = None,
    ) -> list[CellChange]:
        """Записать объёмы за дату одним batch_update. Возвращает, что было и что стало.

        mode: add — прибавить, set — заменить (0 очищает ячейку), delta — прибавить разницу.
        expected — отпечатки позиций (№ и название), которые видел мастер: если в таблице
        по этим строкам уже другое (строки сдвинули) — SheetChangedError, ничего не пишем.
        Для строк без отпечатка эталон — последний загруженный справочник.
        before_write(changes) вызывается после расчёта, до записи: сюда вызывающий кладёт
        «намерение», чтобы после потерянного ответа понять, дошла ли запись (`resume_write`).
        """
        if not volumes:
            return []
        validate_volumes(volumes, mode)  # до обращения к сети
        ids = await self._row_ids(volumes, expected)
        async with self._lock:
            try:
                col = await asyncio.to_thread(self._call, self._ensure_once, d)
                current = await asyncio.to_thread(
                    self._call, self._read_state_once, col, sorted(volumes), ids
                )
                try:
                    changes = compute_changes(current, volumes, mode)
                except CellNotNumericError as e:
                    raise SheetsError(
                        f"В таблице в строке {e.row} за эту дату стоит не число — "
                        "исправьте вручную."
                    ) from e
                if not changes:
                    return []
                if before_write is not None:
                    await before_write(changes)
                await asyncio.to_thread(self._call, self._write_once, col, changes)
                logger.info(
                    "Записано в столбец {} ({}): {}",
                    col_letter(col),
                    mode,
                    ", ".join(f"{c.row}: {c.old:g}→{c.new:g}" for c in changes),
                )
                return changes
            finally:
                self.invalidate_cache()

    async def resume_write(
        self, d: date, changes: list[CellChange], *, expected: Mapping[int, RowId] | None = None
    ) -> list[CellChange]:
        """Довести прерванную запись (ответ Sheets потерялся, процесс упал до журнала).

        Ячейки уже «после» — запись дошла, ничего не пишем. Все ещё «до» — пишем те же
        значения. Иначе (кто-то менял ячейки) — WriteConflictError: молча не трогаем.
        """
        if not changes:
            return []
        rows = sorted(c.row for c in changes)
        ids = await self._row_ids(dict.fromkeys(rows, 0.0), expected)
        async with self._lock:
            try:
                col = await asyncio.to_thread(self._call, self._ensure_once, d)
                current = await asyncio.to_thread(self._call, self._read_state_once, col, rows, ids)
                try:
                    now = cell_numbers(current, rows)
                except CellNotNumericError as e:
                    raise WriteConflictError(
                        f"В таблице в строке {e.row} за эту дату стоит не число — "
                        "проверьте вручную."
                    ) from e
                if all(math.isclose(now[c.row], c.new, abs_tol=1e-6) for c in changes):
                    logger.info("Прерванная запись за {} уже в таблице — повторно не пишу", d)
                    return changes
                if all(math.isclose(now[c.row], c.old, abs_tol=1e-6) for c in changes):
                    await asyncio.to_thread(self._call, self._write_once, col, changes)
                    logger.info("Прерванная запись за {} выполнена повторно", d)
                    return changes
                logger.error("Прерванная запись за {}: ячейки изменились: {}", d, now)
                raise WriteConflictError(
                    "Ячейки в таблице изменились после прерванной записи — проверьте их вручную."
                )
            finally:
                self.invalidate_cache()

    async def _row_ids(
        self, volumes: Mapping[int, float], expected: Mapping[int, RowId] | None
    ) -> dict[int, RowId]:
        """Отпечатки строк для сверки: переданные, а для остальных — из справочника."""
        ids = {r: expected[r] for r in volumes if expected and r in expected}
        missing = [r for r in volumes if r not in ids]
        if missing:
            catalog = self._catalog or await self.load_catalog()
            unknown = sorted(r for r in missing if catalog.by_row(r) is None)
            if unknown:
                raise ValueError(f"строки {unknown} не являются позициями сметы")
            for r in missing:
                pos = catalog.by_row(r)
                assert pos is not None
                ids[r] = row_id(pos)
        return ids

    async def read_volumes(self, d: date, rows: Iterable[int]) -> dict[int, float]:
        """Объёмы за дату по строкам (пустая ячейка = 0). Даты нет в таблице — все нули."""
        wanted = sorted(set(rows))
        if not wanted:
            return {}
        catalog = await self.load_catalog(force=True)
        col = catalog.column_for(d)
        if col is None:
            return {r: 0.0 for r in wanted}
        return await asyncio.to_thread(self._call, self._read_numbers_once, col, wanted)

    async def read_done(self, rows: Iterable[int]) -> dict[int, float]:
        """«Выполненный объем» (колонка I, значение формулы) по строкам."""
        wanted = sorted(set(rows))
        if not wanted:
            return {}
        return await asyncio.to_thread(self._call, self._read_numbers_once, COL_DONE, wanted)

    def row_link(self, row: int) -> str:
        """Ссылка на строку позиции. gid известен после первого обращения к таблице."""
        return build_row_link(self._spreadsheet_id, self._gid, row)

    def invalidate_cache(self) -> None:
        """Справочник считаем устаревшим; прошлую версию оставляем как эталон для сверки строк."""
        self._catalog_at = None

    # ─────────────── Подключение и повторы ───────────────

    def _fresh(self, at: float | None) -> bool:
        return at is not None and self._clock() - at < self._cache_ttl_s

    def _spreadsheet(self) -> Any:
        if self._sh is None:
            gc = self._injected_client
            if gc is None:
                gc = gspread.service_account(filename=str(self._credentials_file))
                gc.set_timeout(self._timeout_s)  # без таймаута зависший запрос блокирует поток
            self._sh = gc.open_by_key(self._spreadsheet_id)
        return self._sh

    def _worksheet(self) -> Any:
        if self._ws is None:
            self._ws = self._spreadsheet().worksheet(self._worksheet_name)
            self._gid = self._ws.id
        return self._ws

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Выполнить блок с повторами при временных сбоях; прочие ошибки → SheetsError."""
        retrying = Retrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential(multiplier=self._retry_wait_s, max=10),
            retry=retry_if_exception(_is_transient),
            reraise=True,
            before_sleep=self._log_retry,
        )
        try:
            return retrying(fn, *args)
        except SheetsError:
            raise
        except Exception as e:
            raise self._translate(e) from e

    @staticmethod
    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning("Sheets: попытка {} не удалась ({}), повторяю", state.attempt_number, exc)

    @staticmethod
    def _translate(exc: Exception) -> SheetsError:
        status = exc.response.status_code if isinstance(exc, APIError) else None
        logger.error("Sheets: {} (HTTP {}): {}", type(exc).__name__, status, exc)
        if isinstance(exc, FileNotFoundError):
            return SheetsError("Не найден файл ключа Google (GOOGLE_CREDENTIALS_FILE).")
        if status == 400 and "Office file" in str(exc):
            return SheetsError(
                "По SPREADSHEET_ID лежит Excel-файл, а не Google Таблица. Откройте его в Google "
                "Drive и выберите «Файл → Сохранить как Google Таблицы», затем возьмите ID новой."
            )
        if isinstance(exc, SpreadsheetNotFound) or status == 404:
            return SheetsError(
                "Таблица не найдена. Проверьте SPREADSHEET_ID и что таблица "
                "открыта сервисному аккаунту."
            )
        if isinstance(exc, WorksheetNotFound):
            return SheetsError("В таблице нет нужного листа. Проверьте WORKSHEET_NAME.")
        if status in (401, 403):
            return SheetsError("Нет доступа к таблице: дайте сервисному аккаунту права редактора.")
        if _is_transient(exc):
            return SheetsError(_MSG_UNAVAILABLE)
        return SheetsError("Не удалось поработать с таблицей. Попробуйте позже.")

    # ─────────────── Блоки, выполняемые в потоке ───────────────

    def _load_catalog_once(self) -> Catalog:
        ws = self._worksheet()
        rows = [list(r) for r in ws.get_values(value_render_option=ValueRenderOption.unformatted)]
        catalog = parse_catalog(rows)
        for d, cols in catalog.duplicate_dates().items():
            letters = ", ".join(col_letter(c) for c in cols)
            logger.warning("Дата {} встречается в столбцах {} — писать буду в первый", d, letters)
        self._catalog, self._catalog_at = catalog, self._clock()
        return catalog

    def _ensure_once(self, d: date) -> int:
        """Блок безопасен к повтору целиком: сначала смотрим, нет ли столбца уже."""
        ws = self._worksheet()
        header = [
            list(r)
            for r in ws.get_values(
                f"1:{DATE_ROW}", value_render_option=ValueRenderOption.unformatted
            )
        ]
        date_columns, last_col = parse_date_columns(header)
        plan = plan_date_column(date_columns, last_col, d)
        if plan.exists:
            if plan.duplicates:
                letters = ", ".join(col_letter(c) for c in plan.duplicates)
                logger.warning("Дата {} в столбцах {} — пишу в первый", d, letters)
            return plan.col

        requests_body = insert_date_column_requests(ws.id, plan.col, d)
        if plan.extend_sum_to is not None:
            logger.warning(
                "Новый столбец {} правее границы формулы SUM(J:CZ) — растягиваю формулы",
                col_letter(plan.col),
            )
            done_col = col_letter(COL_DONE)
            raw = ws.get_values(
                f"{done_col}{FIRST_DATA_ROW}:{done_col}{ws.row_count}",
                value_render_option=ValueRenderOption.formula,
            )
            formulas = {
                r: v
                for r, v in column_values(FIRST_DATA_ROW, [list(x) for x in raw]).items()
                if isinstance(v, str)
            }
            requests_body += extend_sum_requests(
                ws.id, extend_sum_formulas(formulas, plan.extend_sum_to)
            )
        self._spreadsheet().batch_update({"requests": requests_body})
        logger.info("Создан столбец {} для даты {}", col_letter(plan.col), d)
        return plan.col

    def _read_state_once(
        self, col: int, rows: list[int], ids: Mapping[int, RowId]
    ) -> dict[int, Any]:
        """Сверить № и названия строк с отпечатками и прочитать ячейки даты. Только чтение."""
        ws = self._worksheet()
        first, last = min(rows), max(rows)
        ab_a1 = f"{col_letter(COL_NUM)}{first}:{col_letter(COL_NAME)}{last}"
        ab_raw, cur_raw = ws.batch_get(
            [ab_a1, column_range(col, first, last)],
            value_render_option=ValueRenderOption.unformatted,
        )
        ab = two_column_values(first, [list(r) for r in ab_raw])
        shifted = find_shifted_rows(ids, ab)
        if shifted:
            logger.error("Строки {} в таблице не совпадают с тем, что видел мастер", shifted)
            raise SheetChangedError(
                "Таблицу изменили: строки сместились. Обновите отчёт и повторите."
            )
        return column_values(first, [list(r) for r in cur_raw])

    def _write_once(self, col: int, changes: list[CellChange]) -> None:
        """Одна batch_update на весь отчёт. Значения абсолютные → повтор безопасен."""
        data = [{"range": cell(c.row, col), "values": [[sheet_value(c.new)]]} for c in changes]
        self._worksheet().batch_update(data, raw=True)  # RAW: «=...» от мастера не станет формулой

    def _read_numbers_once(self, col: int, rows: list[int]) -> dict[int, float]:
        first, last = rows[0], rows[-1]
        raw = self._worksheet().get_values(
            column_range(col, first, last), value_render_option=ValueRenderOption.unformatted
        )
        values = column_values(first, [list(r) for r in raw])
        return {r: parse_number(values.get(r)) or 0.0 for r in rows}

    # ── синонимы ──

    def _synonyms_worksheet(self) -> Any:
        sh = self._spreadsheet()
        try:
            return sh.worksheet(self._synonyms_name)
        except WorksheetNotFound:
            logger.info("Создаю лист «{}»", self._synonyms_name)
            ws = sh.add_worksheet(title=self._synonyms_name, rows=200, cols=len(SYNONYMS_HEADER))
            ws.update([SYNONYMS_HEADER], "A1", raw=True)
            return ws

    def _read_synonyms_once(self) -> list[list[Any]]:
        ws = self._synonyms_worksheet()
        rows = ws.get_values(value_render_option=ValueRenderOption.unformatted)
        return [list(r) for r in rows]
