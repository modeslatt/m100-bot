"""ReportService с поддельными SheetsClient, BitrixClient и матчером; Storage — настоящий."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from m100bot.matching.llm import LlmError
from m100bot.matching.models import Intent, MatchedItem, MatchStatus, Understood
from m100bot.services.report import (
    ReportError,
    ReportService,
    backoff_seconds,
    distribute_correction,
)
from m100bot.sheets.addressing import CellChange, Mode
from m100bot.sheets.catalog import Catalog
from m100bot.sheets.client import SheetChangedError, SheetsError
from m100bot.storage.db import NewWrite, Storage

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


@dataclass
class Sent:
    """Один вызов save_report: item_id — что передали (None = создать), id — что вернули."""

    item_id: int | None
    id: int
    title: str
    work_date: date
    master_id: int
    master: str
    lines: list[str]
    sheet_url: str


class FakeBitrix:
    """Элементы CRM в памяти: save_report(None) создаёт новый, save_report(id) обновляет."""

    def __init__(self) -> None:
        self.calls: list[Sent] = []
        self.items: dict[int, Sent] = {}
        self.errors: list[Exception] = []

    async def save_report(
        self,
        item_id: int | None,
        *,
        title: str,
        work_date: date,
        master_id: int,
        master: str,
        lines: list[str],
        sheet_url: str,
    ) -> int:
        if self.errors:
            raise self.errors.pop(0)
        new_id = item_id if item_id is not None else 100 + len(self.items)
        sent = Sent(item_id, new_id, title, work_date, master_id, master, list(lines), sheet_url)
        self.calls.append(sent)
        self.items[new_id] = sent
        return new_id


class FakeMatcher:
    def __init__(self, items: list[MatchedItem] | None = None) -> None:
        self.items = items or []
        self.intent: Intent = "report"
        self.truncated = False
        self.seen: list[tuple[str, Catalog, Mapping[str, int] | None]] = []

    async def understand(
        self, text: str, catalog: Catalog, synonyms: Mapping[str, int] | None = None
    ) -> Understood:
        self.seen.append((text, catalog, synonyms))
        return Understood(self.intent, list(self.items), self.truncated)


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
    # send_now=False: здесь проверяется сама очередь; отправку сразу — отдельные тесты ниже
    return ReportService(
        sheets, storage, FakeMatcher(), bitrix, today=lambda: D, clock=clock, send_now=False
    )


async def draft(storage: Storage, *items: MatchedItem, text: str = "исходный текст") -> None:
    await storage.save_draft(USER, text, list(items))


# ─────────────── recognize ───────────────


async def test_recognize_saves_draft_and_marks_remaining(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.done = {17: 140.0, 18: 160.0}
    matcher = FakeMatcher([ok(17, 15), ok(18, 7)])
    service = ReportService(sheets, storage, matcher, today=lambda: D)

    items = (await service.recognize(USER, "медь 6-ка 15 м, 9-ка 7 м")).items

    # остаток = план − выполнено: 265 − 140 = 125 (хватает), 162 − 160 = 2 (превышение)
    assert [(i.row, i.remaining, i.over_remaining) for i in items] == [
        (17, 125, False),
        (18, 2, True),
    ]
    saved = await storage.get_draft(USER)
    assert saved is not None and saved.source_text == "медь 6-ка 15 м, 9-ка 7 м"
    assert saved.items == items


async def test_recognize_passes_current_table_data_to_matcher(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.synonyms = {"хомутов": 44}
    matcher = FakeMatcher([ok(17, 15)])
    service = ReportService(sheets, storage, matcher, today=lambda: D)
    await service.recognize(USER, "a")

    sheets.catalog = type(catalog)(catalog.positions, catalog.date_columns, catalog.last_col)
    await service.recognize(USER, "b")  # таблицу перечитали — матчер получает новое

    assert [(t, c, s) for t, c, s in matcher.seen] == [
        ("a", catalog, {"хомутов": 44}),
        ("b", sheets.catalog, {"хомутов": 44}),
    ]
    assert matcher.seen[1][1] is sheets.catalog


async def test_recognize_has_time_budget(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    class Slow(FakeMatcher):
        async def understand(self, text, catalog, synonyms=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return []

    service = ReportService(sheets, storage, Slow(), today=lambda: D, recognize_budget_s=0.05)
    with pytest.raises(LlmError, match="слишком много времени"):
        await service.recognize(USER, "медь")
    assert await storage.get_draft(USER) is None


async def test_empty_result_keeps_previous_draft(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    await draft(storage, ok(17, 15))
    service = ReportService(sheets, storage, FakeMatcher([]), today=lambda: D)
    assert (await service.recognize(USER, "привет")).items == []
    saved = await storage.get_draft(USER)
    assert saved is not None and saved.items == [ok(17, 15)]


async def test_recognize_survives_unreadable_remaining_and_synonyms(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.done_error = SheetsError("Таблица недоступна.")
    sheets.synonyms_error = SheetsError("Таблица недоступна.")
    matcher = FakeMatcher([ok(17, 15)])
    service = ReportService(sheets, storage, matcher, today=lambda: D)
    # без остатков просто нет предупреждения ⚠️, без синонимов — работаем без них
    assert (await service.recognize(USER, "медь")).items == [ok(17, 15)]
    assert matcher.seen[0][2] == {}


async def test_recognize_needs_the_catalog(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    sheets.catalog_error = SheetsError("Таблица недоступна. Попробуйте позже.")
    service = ReportService(sheets, storage, FakeMatcher([ok(17, 1)]), today=lambda: D)
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


async def test_each_report_is_its_own_outbox_task(service: ReportService, storage: Storage) -> None:
    """Каждый подтверждённый отчёт — свой элемент CRM, только его объёмы."""
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(44, 10), ok(17, 5))
    await service.confirm(USER, D)

    first, second = await storage.pending_outbox()
    assert first.payload["report_id"] != second.payload["report_id"]
    assert [line.rsplit("— ", 1)[1] for line in first.payload["lines"]] == ["15 м"]
    assert [line.rsplit("— ", 1)[1] for line in second.payload["lines"]] == ["5 м", "10 шт"]


async def test_report_title_has_confirmation_time(
    sheets: FakeSheets, storage: Storage, bitrix: FakeBitrix
) -> None:
    service = ReportService(
        sheets,
        storage,
        FakeMatcher(),
        bitrix,
        today=lambda: D,
        now=lambda: datetime(2026, 11, 2, 14, 35, 59),
        send_now=False,
    )
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    [item] = await storage.pending_outbox()
    assert item.payload["title"] == "Отчет от 02.11.2026 14:35"


async def test_no_bitrix_configured_means_no_outbox(
    sheets: FakeSheets, storage: Storage, catalog: Catalog
) -> None:
    service = ReportService(sheets, storage, FakeMatcher(), None, today=lambda: D)
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
    [sent] = bitrix.calls
    assert sent.item_id is None  # новый элемент
    assert (sent.work_date, sent.master, sent.master_id) == (D, "Иван", USER)
    assert sent.title.startswith("Отчет от ") and sent.lines[0].endswith("— 15 м")
    assert sent.sheet_url.startswith("https://sheet.test")
    assert await storage.pending_outbox() == []
    assert (await service.flush_outbox()).sent == 0  # второй раз слать нечего
    [report] = await storage.reports_with_row(USER, D, 17)
    assert report.bitrix_id == sent.id  # id элемента запомнен для исправлений


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


async def test_two_reports_two_items(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(17, 5))
    await service.confirm(USER, D)

    result = await service.flush_outbox()

    assert (result.sent, result.superseded) == (2, 0)
    assert [(c.item_id, c.lines[0].rsplit("— ", 1)[1]) for c in bitrix.calls] == [
        (None, "15 м"),
        (None, "5 м"),  # второй отчёт — свой элемент, первый не затронут
    ]
    assert len(bitrix.items) == 2


async def test_older_task_of_same_report_never_overwrites_newer_one(
    service: ReportService, bitrix: FakeBitrix, storage: Storage, clock: Clock
) -> None:
    """Задание отчёта не ушло; пока ждало, мастер исправил объём. Слать нужно только
    свежее — иначе повтор старого откатил бы элемент назад."""
    bitrix.errors = [RuntimeError("503")]
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await service.flush_outbox()  # не ушло

    await service.correct_today(USER, 17, 12)
    clock.now += 3600
    result = await service.flush_outbox()

    assert (result.sent, result.superseded) == (1, 1)
    [sent] = bitrix.calls
    assert sent.item_id is None and sent.lines == [
        "Фреонопровод медный, Д6,35 TEC TUBE cips — 12 м (исправлено, было 15 м)"
    ]
    assert await storage.pending_outbox() == []


async def test_legacy_day_snapshots_are_dropped(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    """Задания прошлой версии (снимок дня, без report_id) не отправляются."""
    await storage.record_correction(
        USER, D, NewWrite(17, 1, "add", 0), outbox_payloads=[{"master": "Иван", "lines": []}]
    )
    assert (await service.flush_outbox()).sent == 0
    assert bitrix.calls == [] and await storage.pending_outbox() == []


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


async def test_send_now_after_confirm_and_correction(
    sheets: FakeSheets, storage: Storage, bitrix: FakeBitrix
) -> None:
    service = ReportService(sheets, storage, FakeMatcher(), bitrix, today=lambda: D)
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D, master="Иван")
    await service.drain()
    assert [c.master for c in bitrix.calls] == ["Иван"]  # ушло сразу, без фоновой задачи
    assert await storage.pending_outbox() == []

    await service.correct_today(USER, 17, 12, master="Иван")
    await service.drain()
    first, second = bitrix.calls
    assert second.item_id == first.id  # исправление обновило тот же элемент
    assert second.lines[0].endswith("— 12 м (исправлено, было 15 м)")


async def test_send_now_failure_stays_in_outbox(
    sheets: FakeSheets, storage: Storage, bitrix: FakeBitrix, clock: Clock
) -> None:
    service = ReportService(sheets, storage, FakeMatcher(), bitrix, today=lambda: D, clock=clock)
    bitrix.errors = [RuntimeError("Битрикс лёг")]
    await draft(storage, ok(17, 15))
    text = await service.confirm(USER, D)
    await service.drain()

    assert text.startswith("✅ Записано")  # мастер сбоя Битрикса не видит
    [item] = await storage.pending_outbox()
    assert item.attempts == 1  # очередь повторит позже, с паузой
    clock.now += backoff_seconds(1) + 1
    assert (await service.flush_outbox()).sent == 1


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


async def test_correction_updates_the_report_item(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15), ok(44, 10))
    await service.confirm(USER, D)
    await service.flush_outbox()
    [created] = bitrix.calls

    await service.correct_today(USER, 17, 12)
    assert len(bitrix.calls) == 1  # напрямую — никогда, только через очередь
    await service.flush_outbox()

    updated = bitrix.calls[-1]
    assert updated.item_id == created.id and updated.title == created.title
    assert updated.lines == [
        "Фреонопровод медный, Д6,35 TEC TUBE cips — 12 м (исправлено, было 15 м)",
        created.lines[1],  # остальные строки отчёта — как были
    ]


async def test_correction_spread_over_reports_of_the_day(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    """Строка была в двух отчётах (15 + 5 = 20). Исправили на 12: уменьшение снимается
    с последнего отчёта (5 → 0), остаток — с первого (15 → 12). Меняются оба элемента."""
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(17, 5), ok(44, 1))
    await service.confirm(USER, D)
    await service.flush_outbox()
    first_id, second_id = (c.id for c in bitrix.calls)

    await service.correct_today(USER, 17, 12)
    await service.flush_outbox()

    by_item = {c.item_id: c.lines for c in bitrix.calls[2:]}
    assert by_item[first_id][0].endswith("— 12 м (исправлено, было 15 м)")
    assert by_item[second_id][0].endswith("— 0 м (исправлено, было 5 м)")
    assert by_item[second_id][1].endswith("— 1 шт")


async def test_increase_goes_to_the_latest_report_only(
    service: ReportService, bitrix: FakeBitrix, storage: Storage
) -> None:
    await draft(storage, ok(17, 15))
    await service.confirm(USER, D)
    await draft(storage, ok(17, 5))
    await service.confirm(USER, D)
    await service.flush_outbox()

    await service.correct_today(USER, 17, 30)
    result = await service.flush_outbox()

    assert result.sent == 1  # первый отчёт не тронут
    assert bitrix.calls[-1].lines[0].endswith("— 15 м (исправлено, было 5 м)")


@pytest.mark.parametrize(
    ("values", "target", "expected"),
    [
        ([15], 12, [12]),
        ([15, 5], 30, [15, 15]),
        ([15, 5], 18, [15, 3]),
        ([15, 5], 12, [12, 0]),
        ([15, 5], 0, [0, 0]),
        ([15, 5], 20, [15, 5]),
        ([], 5, []),
        ([0.1, 0.2], 0.4, [0.1, 0.3]),
    ],
)
def test_distribute_correction(values: list[float], target: float, expected: list[float]) -> None:
    assert distribute_correction(values, target) == expected


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
