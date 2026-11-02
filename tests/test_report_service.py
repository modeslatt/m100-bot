"""ReportService с поддельными SheetsClient, BitrixClient и матчером; Storage — настоящий."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.services.report import ReportError, ReportService, backoff_seconds
from m100bot.sheets.addressing import CellChange, Mode
from m100bot.sheets.catalog import Catalog
from m100bot.sheets.client import SheetChangedError, SheetsError
from m100bot.storage.db import Storage

D = date(2026, 11, 2)
USER = 111


# ─────────────── Подделки ───────────────


class FakeSheets:
    """Таблица в памяти. Считает «было → стало» так же, как настоящий SheetsClient."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.cells: dict[tuple[date, int], float] = {}
        self.done: dict[int, float] = {}
        self.synonyms: dict[str, int] = {}
        self.write_errors: list[Exception] = []
        self.catalog_error: Exception | None = None
        self.done_error: Exception | None = None
        self.synonyms_error: Exception | None = None
        self.writes: list[tuple[date, dict[int, float], str]] = []
        self.calls: Counter[str] = Counter()

    async def load_catalog(self) -> Catalog:
        self.calls["load_catalog"] += 1
        if self.catalog_error:
            raise self.catalog_error
        return self.catalog

    async def load_synonyms(self) -> dict[str, int]:
        self.calls["load_synonyms"] += 1
        if self.synonyms_error:
            raise self.synonyms_error
        return self.synonyms

    async def read_done(self, rows: Iterable[int]) -> dict[int, float]:
        self.calls["read_done"] += 1
        if self.done_error:
            raise self.done_error
        return {r: self.done.get(r, 0.0) for r in rows}

    async def write_volumes(
        self, d: date, volumes: dict[int, float], *, mode: Mode = "add"
    ) -> list[CellChange]:
        self.calls["write_volumes"] += 1
        if self.write_errors:
            raise self.write_errors.pop(0)
        changes = []
        for row, qty in sorted(volumes.items()):
            old = self.cells.get((d, row), 0.0)
            new = round(old + qty if mode == "add" else qty, 6)
            self.cells[(d, row)] = new
            changes.append(CellChange(row, old, new))
        self.writes.append((d, dict(volumes), mode))
        return changes

    def row_link(self, row: int) -> str:
        return f"https://sheet.test/edit#range=B{row}"


class FakeBitrix:
    def __init__(self) -> None:
        self.calls: list[tuple[date, str, list[str], str]] = []
        self.errors: list[Exception] = []

    async def upsert_daily_report(
        self, d: date, master: str, lines: list[str], sheet_url: str
    ) -> int:
        if self.errors:
            raise self.errors.pop(0)
        self.calls.append((d, master, list(lines), sheet_url))
        return len(self.calls)


class FakeMatcher:
    def __init__(self, catalog: Catalog, items: list[MatchedItem] | None = None) -> None:
        self.catalog = catalog
        self.items = items or []
        self.refreshed: list[tuple[Catalog, dict[str, int] | None]] = []
        self.seen: list[tuple[str, Mapping[int, float] | None]] = []

    def refresh(self, catalog: Catalog, synonyms: dict[str, int] | None = None) -> None:
        self.catalog = catalog
        self.refreshed.append((catalog, synonyms))

    async def match(
        self, text: str, remaining: Mapping[int, float] | None = None
    ) -> list[MatchedItem]:
        self.seen.append((text, remaining))
        return list(self.items)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ok(row: int, qty: float, phrase: str = "") -> MatchedItem:
    return MatchedItem(
        phrase=phrase or f"позиция {row}", qty=qty, unit="м", status=MatchStatus.OK, row=row
    )


def ambiguous(phrase: str = "хомуты") -> MatchedItem:
    return MatchedItem(
        phrase=phrase, qty=12, unit="шт", status=MatchStatus.AMBIGUOUS, candidates=[43, 44]
    )


def clarify(phrase: str = "медь тянули") -> MatchedItem:
    return MatchedItem(phrase=phrase, qty=None, unit=None, status=MatchStatus.CLARIFY, row=17)


# ─────────────── Фикстуры ───────────────


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = Storage(tmp_path / "bot.sqlite3")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def sheets(catalog: Catalog) -> FakeSheets:
    return FakeSheets(catalog)


@pytest.fixture
def bitrix() -> FakeBitrix:
    return FakeBitrix()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(
    sheets: FakeSheets, storage: Storage, bitrix: FakeBitrix, catalog: Catalog, clock: Clock
) -> ReportService:
    return ReportService(
        sheets, storage, FakeMatcher(catalog), bitrix, today=lambda: D, clock=clock
    )


async def draft(storage: Storage, *items: MatchedItem, text: str = "исходный текст") -> None:
    await storage.save_draft(USER, text, list(items))


# ─────────────── recognize ───────────────


async def test_recognize_saves_draft_and_passes_remaining(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.done = {17: 140.0}
    matcher = FakeMatcher(catalog, [ok(17, 15)])
    service = ReportService(sheets, storage, matcher, today=lambda: D)

    items = await service.recognize(USER, "медь 6-ка 15 м")

    assert items == [ok(17, 15)]
    saved = await storage.get_draft(USER)
    assert saved is not None and saved.source_text == "медь 6-ка 15 м" and saved.items == items
    _, remaining = matcher.seen[0]
    assert remaining is not None
    assert remaining[17] == 265 - 140  # план − выполнено
    assert remaining[18] == 162  # ничего не сделано — весь план


async def test_recognize_refreshes_matcher_only_when_table_data_changed(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    matcher = FakeMatcher(catalog, [ok(17, 15)])
    matcher.catalog = object()  # type: ignore[assignment]  # матчер ещё не видел справочник
    service = ReportService(sheets, storage, matcher, today=lambda: D)

    await service.recognize(USER, "a")
    await service.recognize(USER, "b")  # справочник и синонимы те же — повторно не обновляем
    assert len(matcher.refreshed) == 1 and matcher.refreshed[0][0] is catalog

    sheets.catalog = type(catalog)(catalog.positions, catalog.date_columns, catalog.last_col)
    await service.recognize(USER, "c")  # таблицу перечитали — подхватываем новое
    assert len(matcher.refreshed) == 2


async def test_empty_result_keeps_previous_draft(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    await draft(storage, ok(17, 15))
    service = ReportService(sheets, storage, FakeMatcher(catalog, []), today=lambda: D)
    assert await service.recognize(USER, "привет") == []
    saved = await storage.get_draft(USER)
    assert saved is not None and saved.items == [ok(17, 15)]


async def test_recognize_survives_unreadable_remaining_and_synonyms(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.done_error = SheetsError("Таблица недоступна.")
    sheets.synonyms_error = SheetsError("Таблица недоступна.")
    matcher = FakeMatcher(catalog, [ok(17, 15)])
    service = ReportService(sheets, storage, matcher, today=lambda: D)
    assert await service.recognize(USER, "медь") == [ok(17, 15)]
    assert matcher.seen[0][1] is None  # без остатков: просто нет предупреждения ⚠️


async def test_recognize_needs_the_catalog(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.catalog_error = SheetsError("Таблица недоступна. Попробуйте позже.")
    service = ReportService(sheets, storage, FakeMatcher(catalog, [ok(17, 1)]), today=lambda: D)
    with pytest.raises(SheetsError, match="недоступна"):
        await service.recognize(USER, "медь")
    assert await storage.get_draft(USER) is None


# ─────────────── confirm: успех ───────────────


async def test_confirm_writes_only_ready_items_aggregated_by_row(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ok(17, 15), ok(17, 5), ok(44, 10), ambiguous(), clarify())
    text = await service.confirm(USER, D)

    assert sheets.writes == [(D, {17: 20.0, 44: 10.0}, "add")]  # одна запись, строки сложены
    assert "✅ Записано в таблицу за 02.11.2026" in text
    assert "Фреонопровод медный" in text and "20 м" in text
    assert "❓ Не записано, нужно уточнить: «хомуты», «медь тянули»" in text


async def test_unresolved_items_stay_in_draft(service: ReportService, storage: Storage) -> None:
    await draft(storage, ok(17, 15), ambiguous(), text="мой текст")
    await service.confirm(USER, D)
    left = await storage.get_draft(USER)
    assert left is not None
    assert left.items == [ambiguous()] and left.source_text == "мой текст"


async def test_confirm_deletes_draft_when_everything_written(
    service: ReportService, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    assert await storage.get_draft(USER) is None


async def test_every_write_is_journaled_with_prev_value(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.cells[(D, 17)] = 15.0  # в ячейке уже было 15
    await draft(storage, ok(17, 5), ok(44, 10))
    await service.confirm(USER, D)

    records = await storage.get_writes(USER, D)
    assert [(r.row, r.qty, r.mode, r.prev_value) for r in records] == [
        (17, 5, "add", 15.0),
        (44, 10, "add", 0.0),
    ]


async def test_second_report_same_day_accumulates_and_journals_prev(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(17, 5), ok(18, 7))
    text = await service.confirm(USER, D)

    assert sheets.cells[(D, 17)] == 20
    records = await storage.get_writes(USER, D)
    assert [(r.row, r.prev_value) for r in records] == [(17, 0.0), (17, 15.0), (18, 0.0)]
    assert "всего за день 20" in text  # мастер видит итог по ячейке


async def test_over_remaining_warning_in_text(service: ReportService, storage: Storage) -> None:
    over = ok(17, 300)
    over.over_remaining = True
    await draft(storage, over)
    text = await service.confirm(USER, D)
    assert "⚠️ Больше остатка по смете: Фреонопровод" in text


# ─────────────── confirm: нечего записывать ───────────────


async def test_confirm_without_draft(service: ReportService, sheets: FakeSheets) -> None:
    with pytest.raises(ReportError, match="Нет отчёта"):
        await service.confirm(USER, D)
    assert sheets.calls["write_volumes"] == 0


async def test_confirm_with_only_unresolved_items(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ambiguous(), clarify())
    with pytest.raises(ReportError, match="Нечего записывать"):
        await service.confirm(USER, D)
    assert sheets.calls["write_volumes"] == 0
    assert await storage.get_draft(USER) is not None


async def test_item_without_quantity_is_not_written(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sneaky = MatchedItem(phrase="x", qty=None, unit="м", status=MatchStatus.OK, row=17)
    await draft(storage, sneaky, ok(44, 10))
    await service.confirm(USER, D)
    assert sheets.writes[0][1] == {44: 10.0}


# ─────────────── confirm: ошибка записи в Sheets ───────────────


async def test_sheets_failure_keeps_draft_and_tells_master(
    service: ReportService,
    sheets: FakeSheets,
    storage: Storage,
    bitrix: FakeBitrix,
) -> None:
    sheets.write_errors = [SheetsError("Таблица недоступна. Попробуйте позже.")]
    await draft(storage, ok(17, 15), ambiguous())

    with pytest.raises(ReportError) as e:
        await service.confirm(USER, D)

    message = str(e.value)
    assert "Таблица недоступна" in message and "Отчёт сохранён" in message and "Записать" in message
    left = await storage.get_draft(USER)
    assert left is not None and left.items == [ok(17, 15), ambiguous()]  # черновик цел
    assert await storage.get_writes() == []  # в журнал ничего не попало
    assert await storage.pending_outbox() == []  # и в Битрикс тоже
    assert bitrix.calls == []


async def test_retry_after_failure_writes_exactly_once(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.write_errors = [SheetsError("Таблица недоступна.")]
    await draft(storage, ok(17, 15))
    with pytest.raises(ReportError):
        await service.confirm(USER, D)

    await service.confirm(USER, D)  # мастер нажал «Записать» ещё раз

    assert sheets.cells[(D, 17)] == 15  # не 30
    assert len(await storage.get_writes()) == 1
    assert await storage.get_draft(USER) is None


async def test_shifted_rows_tell_master_to_resend(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.write_errors = [SheetChangedError("строки сместились")]
    await draft(storage, ok(17, 15))
    with pytest.raises(ReportError, match="Отправьте отчёт заново"):
        await service.confirm(USER, D)
    assert await storage.get_draft(USER) is not None  # исходный текст сохранён для повтора


async def test_invalid_data_is_reported_not_raised_raw(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.write_errors = [ValueError("строки [999] не являются позициями сметы")]
    await draft(storage, ok(17, 15))
    with pytest.raises(ReportError, match="некорректные позиции"):
        await service.confirm(USER, D)
    assert await storage.get_draft(USER) is not None


async def test_catalog_failure_stops_before_any_write(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.catalog_error = SheetsError("Таблица недоступна. Попробуйте позже.")
    await draft(storage, ok(17, 15))
    with pytest.raises(ReportError, match="Таблица недоступна"):
        await service.confirm(USER, D)
    assert sheets.calls["write_volumes"] == 0
    assert await storage.get_draft(USER) is not None


async def test_journal_failure_after_sheets_write_says_do_not_repeat(
    service: ReportService,
    sheets: FakeSheets,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таблица уже изменена, а сохранить журнал не вышло: повтор удвоил бы объёмы."""

    async def broken(*a: Any, **kw: Any) -> None:
        raise OSError("диск недоступен")

    monkeypatch.setattr(storage, "record_confirmation", broken)
    await draft(storage, ok(17, 15))
    with pytest.raises(ReportError, match="Повторно не нажимайте"):
        await service.confirm(USER, D)
    assert sheets.cells[(D, 17)] == 15  # запись в таблице есть


# ─────────────── Повторные и параллельные нажатия ───────────────


async def test_double_tap_writes_once(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    results = await asyncio.gather(
        service.confirm(USER, D), service.confirm(USER, D), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, ReportError) for r in results) == 1
    assert sheets.cells[(D, 17)] == 15 and sheets.calls["write_volumes"] == 1


async def test_users_do_not_block_or_mix(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await storage.save_draft(1, "a", [ok(17, 10)])
    await storage.save_draft(2, "b", [ok(17, 5)])
    await asyncio.gather(service.confirm(1, D), service.confirm(2, D))
    assert sheets.cells[(D, 17)] == 15
    assert {r.user_id for r in await storage.get_writes()} == {1, 2}


# ─────────────── Битрикс через outbox ───────────────


async def test_confirm_never_calls_bitrix_directly(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15), ok(44, 10))
    await service.confirm(USER, D, master="Иван Петров")

    assert bitrix.calls == []  # напрямую — никогда
    [queued] = await storage.pending_outbox()
    assert queued.work_date == D
    assert queued.payload["master"] == "Иван Петров"
    assert queued.payload["user_id"] == USER
    assert queued.payload["sheet_url"] == "https://sheet.test/edit#range=B17"
    assert len(queued.payload["lines"]) == 2
    assert queued.payload["lines"][0].endswith("— 15 м")


async def test_bitrix_down_does_not_affect_confirm(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    bitrix.errors = [RuntimeError("Битрикс лежит")] * 5
    await draft(storage, ok(17, 15))
    text = await service.confirm(USER, D)  # мастер получает ✅ как обычно
    assert text.startswith("✅")
    assert len(await storage.pending_outbox()) == 1


async def test_outbox_snapshot_covers_the_whole_day(
    service: ReportService, storage: Storage
) -> None:
    """Элемент за дату обновляется целиком — второй отчёт не должен затереть первый."""
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(44, 10), ok(17, 5))
    await service.confirm(USER, D)

    first, second = await storage.pending_outbox()
    assert len(first.payload["lines"]) == 1
    assert [line.rsplit("— ", 1)[1] for line in second.payload["lines"]] == ["20 м", "10 шт"]


async def test_no_bitrix_configured_means_no_outbox(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    service = ReportService(sheets, storage, FakeMatcher(catalog), None, today=lambda: D)
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    assert await storage.pending_outbox() == []
    assert await service.flush_outbox() == await service.flush_outbox()  # и не падает


async def test_flush_sends_and_marks_done(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D, master="Иван")

    result = await service.flush_outbox()

    assert (result.sent, result.failed) == (1, 0)
    [(d, master, lines, url)] = bitrix.calls
    assert (d, master) == (D, "Иван") and lines[0].endswith("— 15 м")
    assert url.startswith("https://sheet.test")
    assert await storage.pending_outbox() == []
    assert (await service.flush_outbox()).sent == 0  # второй раз слать нечего


async def test_flush_failure_keeps_item_with_sanitized_error(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    secret = "https://portal.bitrix24.by/rest/1/SECRETTOKEN/crm.item.add.json"
    bitrix.errors = [RuntimeError(f"Client error '404' for url '{secret}'")]
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)

    result = await service.flush_outbox()

    assert (result.sent, result.failed) == (0, 1)
    [item] = await storage.pending_outbox()
    assert item.attempts == 1
    assert item.last_error is not None
    assert "SECRETTOKEN" not in item.last_error and "<url>" in item.last_error


async def test_flush_backoff_then_success(
    service: ReportService, bitrix: FakeBitrix, storage: Storage, clock: Clock
) -> None:
    bitrix.errors = [RuntimeError("503")]
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)

    assert (await service.flush_outbox()).failed == 1
    again = await service.flush_outbox()  # сразу же: пауза ещё не прошла
    assert (again.failed, again.waiting) == (0, 1) and bitrix.calls == []

    clock.now += backoff_seconds(1) + 1
    assert (await service.flush_outbox()).sent == 1
    assert len(bitrix.calls) == 1 and await storage.pending_outbox() == []


async def test_older_snapshot_never_overwrites_newer_one(
    service: ReportService, bitrix: FakeBitrix, storage: Storage, clock: Clock
) -> None:
    """Первое задание не ушло; пока ждало, пришло второе (полный снимок дня). Слать нужно
    только второе — иначе повтор старого откатил бы Битрикс назад."""
    bitrix.errors = [RuntimeError("503")]
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await service.flush_outbox()  # первое не ушло

    await draft(storage, ok(17, 5))
    await service.confirm(USER, D)
    clock.now += 3600
    result = await service.flush_outbox()

    assert (result.sent, result.superseded) == (1, 1)
    assert len(bitrix.calls) == 1
    assert bitrix.calls[0][2][0].endswith("— 20 м")  # снимок целого дня
    assert await storage.pending_outbox() == []


async def test_different_days_and_masters_are_sent_separately(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await storage.save_draft(1, "a", [ok(17, 1)])
    await storage.save_draft(2, "b", [ok(17, 2)])
    await service.confirm(1, D)
    await service.confirm(2, D)
    await storage.save_draft(1, "c", [ok(17, 3)])
    await service.confirm(1, date(2026, 11, 3))

    result = await service.flush_outbox()
    assert (result.sent, result.superseded) == (3, 0)


def test_backoff_grows_and_is_capped() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(50) == 900


# ─────────────── correct_today ───────────────


async def test_correct_today_sets_value_and_journals_prev(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)

    text = await service.correct_today(USER, 17, 12)

    assert sheets.cells[(D, 17)] == 12
    assert sheets.writes[-1] == (D, {17: 12}, "set")
    assert "было 15, стало 12 м" in text
    records = await storage.get_writes(USER, D)
    assert [(r.mode, r.qty, r.prev_value) for r in records] == [("add", 15, 0.0), ("set", 12, 15.0)]


async def test_correction_goes_to_bitrix_outbox_as_replacement(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await service.correct_today(USER, 17, 12)

    assert bitrix.calls == []
    await service.flush_outbox()
    [(_, _, lines, _)] = bitrix.calls  # старый снимок заменён новым
    assert lines[0].endswith("— 12 м")  # set заменил, а не прибавил к 15


async def test_correct_today_to_zero_clears(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    text = await service.correct_today(USER, 17, 0)
    assert "Очищено" in text and "было 15" in text
    [_, last] = await storage.get_writes()
    assert (last.mode, last.qty, last.prev_value) == ("set", 0, 15.0)


async def test_correct_today_failure_changes_nothing(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.write_errors = [SheetsError("Таблица недоступна.")]
    with pytest.raises(ReportError, match="Таблица недоступна"):
        await service.correct_today(USER, 17, 12)
    assert await storage.get_writes() == [] and await storage.pending_outbox() == []


@pytest.mark.parametrize("qty", [-1, float("nan"), float("inf")])
async def test_correct_today_rejects_bad_quantity(
    service: ReportService, sheets: FakeSheets, qty: float
) -> None:
    with pytest.raises(ReportError, match="Объём"):
        await service.correct_today(USER, 17, qty)
    assert sheets.calls["write_volumes"] == 0


async def test_correct_today_rejects_non_position_row(
    service: ReportService, sheets: FakeSheets
) -> None:
    with pytest.raises(ReportError, match="не является позицией"):
        await service.correct_today(USER, 52, 5)  # строка «ИТОГО ПО ОВ 3»
    assert sheets.calls["write_volumes"] == 0


# ─────────────── remaining ───────────────


async def test_remaining(service: ReportService, sheets: FakeSheets) -> None:
    sheets.done = {17: 140.0}
    assert await service.remaining(17) == (265.0, 140.0, "м")
    assert await service.remaining(18) == (162.0, 0.0, "м")


async def test_remaining_of_non_position(service: ReportService) -> None:
    with pytest.raises(ValueError, match="не является позицией"):
        await service.remaining(4)
