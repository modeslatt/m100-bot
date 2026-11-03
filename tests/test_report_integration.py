"""ReportService + настоящий SheetsClient (на имитации gspread) + настоящий Storage.

Подделан только внешний мир: Google (fake_sheets), Битрикс и LLM-матчер. Так проверяется,
что настоящий клиент подходит сервису по интерфейсу и что prev_value берётся из реальных
ячеек образца, а не из подделки.
"""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fake_sheets import FakeGspread, make_gspread
from test_report_service import FakeBitrix, FakeMatcher, ok

from m100bot.services.report import ReportService
from m100bot.sheets.addressing import col_index
from m100bot.sheets.catalog import Catalog
from m100bot.sheets.client import SheetsClient
from m100bot.storage.db import Storage

USER = 7
EXISTING = date(2026, 8, 25)  # в образце в этот день: строка 17 = 15, строка 18 = 15
NEW = date(2026, 11, 2)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = Storage(tmp_path / "bot.sqlite3")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def fake(sample_rows: list[list[Any]]) -> FakeGspread:
    return make_gspread(sample_rows)


@pytest.fixture
def make_service(fake: FakeGspread, storage: Storage, catalog: Catalog):
    def build(today: date, bitrix: FakeBitrix | None = None) -> ReportService:
        client = SheetsClient(
            Path("x.json"), fake.spreadsheet_id, "Лист1", retry_wait_s=0, gspread_client=fake
        )
        return ReportService(client, storage, FakeMatcher(), bitrix, today=lambda: today)

    return build


def cell(fake: FakeGspread, row: int, col: int) -> Any:
    return fake.spreadsheet.sheets["Лист1"].get_cell(row, col)


async def test_prev_value_comes_from_the_real_cells(
    make_service, fake: FakeGspread, storage: Storage
) -> None:
    service = make_service(EXISTING)
    await storage.save_draft(USER, "текст", [ok(17, 5), ok(18, 3)])

    text = await service.confirm(USER, EXISTING)

    column = col_index("N")  # 25.08 в образце — столбец N
    assert (cell(fake, 17, column), cell(fake, 18, column)) == (20, 18)
    records = await storage.get_writes(USER, EXISTING)
    assert [(r.row, r.qty, r.prev_value) for r in records] == [(17, 5, 15.0), (18, 3, 15.0)]
    assert "всего за день 20" in text


async def test_new_day_creates_column_and_journals_zero(
    make_service, fake: FakeGspread, storage: Storage
) -> None:
    service = make_service(NEW)
    await storage.save_draft(USER, "текст", [ok(17, 15)])

    await service.confirm(USER, NEW)

    assert cell(fake, 17, col_index("AX")) == 15  # столбец создан после последней даты
    assert (await storage.get_writes())[0].prev_value == 0.0
    assert fake.spreadsheet.structure_updates  # вставка столбца была


async def test_full_flow_through_outbox(make_service, storage: Storage, catalog: Catalog) -> None:
    bitrix = FakeBitrix()
    service = make_service(NEW, bitrix)
    service.matcher = FakeMatcher([ok(17, 15), ok(55, 56)])

    await service.recognize(USER, "медь 6-ка 15 м, кабель 3х2,5 — 56 м")
    await service.confirm(USER, NEW, master="Иван Петров")
    assert bitrix.calls == []
    await service.flush_outbox()

    [(d, master, lines, url)] = bitrix.calls
    assert (d, master) == (NEW, "Иван Петров")
    assert len(lines) == 2 and lines[0].endswith("— 15 м") and lines[1].endswith("— 56 м")
    assert url.startswith("https://docs.google.com/spreadsheets/d/SHEET_ID/edit#gid=1000&range=B17")


async def test_recognize_computes_remaining_from_real_sheet(
    make_service, storage: Storage, catalog: Catalog
) -> None:
    service = make_service(NEW)
    matcher = FakeMatcher([ok(17, 15)])
    service.matcher = matcher

    [item] = await service.recognize(USER, "медь")

    assert item.remaining == 265 - 140  # выполнено 140 по формуле I17


async def test_correction_after_confirm_sets_value(
    make_service, fake: FakeGspread, storage: Storage
) -> None:
    service = make_service(NEW)
    await storage.save_draft(USER, "текст", [ok(17, 15)])
    await service.confirm(USER, NEW)

    await service.correct_today(USER, 17, 12)

    assert cell(fake, 17, col_index("AX")) == 12
    last = (await storage.get_writes())[-1]
    assert (last.mode, last.qty, last.prev_value) == ("set", 12, 15.0)
