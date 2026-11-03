"""Storage на настоящей SQLite во временной папке: черновики, журнал, outbox, транзакции."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from loguru import logger

from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.storage.db import NewWrite, Storage, fold_writes

D = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def item(row: int | None, qty: float | None, status: MatchStatus = MatchStatus.OK) -> MatchedItem:
    return MatchedItem(phrase=f"фраза {row}", qty=qty, unit="м", status=status, row=row)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = Storage(tmp_path / "data" / "bot.sqlite3", now=lambda: NOW)
    await s.init()
    yield s
    await s.close()


# ─────────────── Открытие ───────────────


async def test_init_creates_folder_and_tables(storage: Storage) -> None:
    assert storage.path.exists()
    cur = await storage.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in await cur.fetchall()}
    assert {"drafts", "writes", "reports", "report_lines", "bitrix_outbox"} <= tables


async def test_init_is_idempotent(storage: Storage) -> None:
    await storage.save_draft(1, "текст", [item(17, 15)])
    await storage.init()  # повторный вызов не должен ни сломать, ни стереть
    assert await storage.get_draft(1) is not None


async def test_data_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    async with Storage(path, now=lambda: NOW) as s:
        await s.save_draft(1, "текст", [item(17, 15)])
        await s.record_correction(1, D, NewWrite(17, 5, "set", 3), outbox_payloads=[{"a": 1}])
    async with Storage(path) as s:
        assert (await s.get_draft(1)).items[0].qty == 15  # type: ignore[union-attr]
        assert len(await s.get_writes()) == 1
        assert len(await s.pending_outbox()) == 1


async def test_use_before_init_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="init"):
        await Storage(tmp_path / "x.sqlite3").get_draft(1)


# ─────────────── Черновики ───────────────


async def test_draft_roundtrip(storage: Storage) -> None:
    items = [item(17, 15), item(None, None, MatchStatus.NOT_FOUND)]
    items[1].candidates = [18, 21]
    await storage.save_draft(7, "медь 6-ка 15 м", items)
    draft = await storage.get_draft(7)
    assert draft is not None
    assert draft.source_text == "медь 6-ка 15 м"
    assert draft.items == items
    assert draft.updated_at == "2026-11-02T12:00:00+00:00"


async def test_new_draft_replaces_old_one(storage: Storage) -> None:
    await storage.save_draft(7, "старый", [item(17, 1)])
    await storage.save_draft(7, "новый", [item(18, 2)])
    draft = await storage.get_draft(7)
    assert draft is not None and draft.source_text == "новый" and draft.items[0].row == 18


async def test_drafts_are_per_user(storage: Storage) -> None:
    await storage.save_draft(1, "a", [item(17, 1)])
    await storage.save_draft(2, "b", [item(18, 2)])
    await storage.delete_draft(1)
    assert await storage.get_draft(1) is None
    assert await storage.get_draft(2) is not None


async def test_missing_draft_is_none(storage: Storage) -> None:
    assert await storage.get_draft(404) is None


async def test_unreadable_draft_is_none_and_kept(storage: Storage) -> None:
    """Формат MatchedItem мог измениться после обновления: не падаем и не стираем данные."""
    await storage.save_draft(1, "текст", [item(17, 1)])
    await storage.db.execute("UPDATE drafts SET items_json='[{\"сломано\": true}]'")
    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        assert await storage.get_draft(1) is None
    finally:
        logger.remove(sink)
    assert errors and "не читается" in errors[0]
    cur = await storage.db.execute("SELECT COUNT(*) AS n FROM drafts")
    assert (await cur.fetchone())["n"] == 1


# ─────────────── Журнал ───────────────


async def test_confirmation_journals_writes_with_prev_value(storage: Storage) -> None:
    await storage.save_draft(1, "текст", [item(17, 15)])
    ids = await storage.record_confirmation(
        1,
        D,
        [NewWrite(17, 15, "add", 0.0), NewWrite(44, 10, "add", 5.0)],
        outbox_payload=None,
        leftover=[],
    )
    assert len(ids) == 2
    records = await storage.get_writes(1, D)
    assert [(r.row, r.qty, r.mode, r.prev_value) for r in records] == [
        (17, 15, "add", 0.0),
        (44, 10, "add", 5.0),
    ]
    assert records[0].created_at == "2026-11-02T12:00:00+00:00" and records[0].work_date == D


async def test_get_writes_filters_and_orders(storage: Storage) -> None:
    other = date(2026, 11, 3)
    await storage.record_correction(1, D, NewWrite(17, 1, "add", 0))
    await storage.record_correction(2, D, NewWrite(18, 2, "add", 0))
    await storage.record_correction(1, other, NewWrite(19, 3, "add", 0))
    assert [r.row for r in await storage.get_writes()] == [17, 18, 19]
    assert [r.row for r in await storage.get_writes(user_id=1)] == [17, 19]
    assert [r.row for r in await storage.get_writes(user_id=1, work_date=D)] == [17]
    assert [r.row for r in await storage.get_writes(work_date=D)] == [17, 18]


async def test_prev_value_may_be_unknown(storage: Storage) -> None:
    await storage.record_correction(1, D, NewWrite(17, 1, "set", None))
    assert (await storage.get_writes())[0].prev_value is None


# ─────────────── Транзакции ───────────────


async def test_confirmation_removes_draft_when_nothing_left(storage: Storage) -> None:
    await storage.save_draft(1, "текст", [item(17, 15)])
    await storage.record_confirmation(
        1, D, [NewWrite(17, 15, "add", 0)], outbox_payload={"x": 1}, leftover=[]
    )
    assert await storage.get_draft(1) is None


async def test_confirmation_keeps_unresolved_items_in_draft(storage: Storage) -> None:
    unresolved = item(None, None, MatchStatus.AMBIGUOUS)
    await storage.save_draft(1, "исходный текст", [item(17, 15), unresolved])
    await storage.record_confirmation(
        1, D, [NewWrite(17, 15, "add", 0)], outbox_payload=None, leftover=[unresolved]
    )
    draft = await storage.get_draft(1)
    assert draft is not None
    assert draft.items == [unresolved] and draft.source_text == "исходный текст"


async def test_failure_midway_rolls_everything_back(storage: Storage) -> None:
    """Вторая запись нарушает NOT NULL: не должно остаться ни первой записи, ни outbox,
    а черновик обязан уцелеть."""
    await storage.save_draft(1, "текст", [item(17, 15)])
    bad = NewWrite(44, None, "add", 0)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        await storage.record_confirmation(
            1,
            D,
            [NewWrite(17, 15, "add", 0), bad],
            outbox_payload={"x": 1},
            leftover=[],
        )
    assert await storage.get_writes() == []
    assert await storage.pending_outbox() == []
    assert await storage.get_draft(1) is not None


async def test_storage_works_after_a_failed_transaction(storage: Storage) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await storage.record_correction(
            1,
            D,
            NewWrite(17, None, "set", 0),  # type: ignore[arg-type]
        )
    await storage.record_correction(1, D, NewWrite(17, 5, "set", 0))
    assert len(await storage.get_writes()) == 1


async def test_concurrent_writers_do_not_interfere(storage: Storage) -> None:
    await asyncio.gather(
        *(
            storage.record_correction(
                u, D, NewWrite(17, float(u), "add", 0), outbox_payloads=[{"user_id": u}]
            )
            for u in range(1, 21)
        ),
        *(storage.save_draft(u, "t", [item(17, 1)]) for u in range(1, 21)),
    )
    assert sorted(r.user_id for r in await storage.get_writes()) == list(range(1, 21))
    assert len(await storage.pending_outbox()) == 20


# ─────────────── Outbox ───────────────


async def test_outbox_lifecycle(storage: Storage) -> None:
    await storage.record_correction(
        1, D, NewWrite(17, 1, "add", 0), outbox_payloads=[{"master": "Иван", "lines": ["а — 1 м"]}]
    )
    [pending] = await storage.pending_outbox()
    assert pending.payload == {"master": "Иван", "lines": ["а — 1 м"]}  # кириллица цела
    assert (pending.work_date, pending.attempts, pending.last_error) == (D, 0, None)

    await storage.mark_outbox_failed(pending.id, "ConnectError: нет связи")
    await storage.mark_outbox_failed(pending.id, "ConnectError: нет связи")
    [again] = await storage.pending_outbox()
    assert (again.attempts, again.last_error) == (2, "ConnectError: нет связи")

    await storage.mark_outbox_done([pending.id])
    assert await storage.pending_outbox() == []


async def test_outbox_done_with_note(storage: Storage) -> None:
    await storage.record_correction(1, D, NewWrite(17, 1, "add", 0), outbox_payloads=[{"a": 1}])
    [pending] = await storage.pending_outbox()
    await storage.mark_outbox_done([pending.id], note="заменено")
    cur = await storage.db.execute("SELECT done, last_error FROM bitrix_outbox")
    row = await cur.fetchone()
    assert (row["done"], row["last_error"]) == (1, "заменено")


async def test_outbox_in_creation_order(storage: Storage) -> None:
    for n in range(3):
        await storage.record_correction(1, D, NewWrite(17, 1, "add", 0), outbox_payloads=[{"n": n}])
    assert [i.payload["n"] for i in await storage.pending_outbox()] == [0, 1, 2]


# ─────────────── Отчёты ───────────────


async def test_confirmation_creates_report_and_tags_outbox(storage: Storage) -> None:
    writes = [NewWrite(17, 15, "add", 0), NewWrite(44, 10, "add", 2)]
    await storage.record_confirmation(
        1, D, writes, outbox_payload={"x": 1}, leftover=[], title="Отчет от 02.11.2026 15:00"
    )
    [task] = await storage.pending_outbox()
    report = await storage.get_report(task.payload["report_id"])
    assert report is not None
    assert (report.user_id, report.work_date, report.title, report.bitrix_id) == (
        1,
        D,
        "Отчет от 02.11.2026 15:00",
        None,
    )
    assert [(x.row, x.qty, x.reported) for x in report.lines] == [(17, 15, 15), (44, 10, 10)]
    assert task.payload["x"] == 1


async def test_reports_with_row_and_corrections(storage: Storage) -> None:
    for qty in (15, 5):
        await storage.record_confirmation(
            1, D, [NewWrite(17, qty, "add", 0)], outbox_payload=None, leftover=[], title="t"
        )
    await storage.record_confirmation(  # другой мастер и другая строка — не попадают
        2, D, [NewWrite(17, 1, "add", 0)], outbox_payload=None, leftover=[], title="t"
    )
    await storage.record_confirmation(
        1, D, [NewWrite(18, 1, "add", 0)], outbox_payload=None, leftover=[], title="t"
    )
    first, second = await storage.reports_with_row(1, D, 17)
    assert first.id < second.id

    await storage.record_correction(
        1,
        D,
        NewWrite(17, 12, "set", 20),
        line_updates=[(first.id, 12), (second.id, 0)],
        outbox_payloads=[{"report_id": first.id}, {"report_id": second.id}],
    )
    first, second = await storage.reports_with_row(1, D, 17)
    assert [(x.qty, x.reported) for x in first.lines + second.lines] == [(12, 15), (0, 5)]
    assert len(await storage.pending_outbox()) == 2


async def test_set_bitrix_id(storage: Storage) -> None:
    await storage.record_confirmation(
        1, D, [NewWrite(17, 1, "add", 0)], outbox_payload=None, leftover=[], title="t"
    )
    [report] = await storage.reports_with_row(1, D, 17)
    await storage.set_bitrix_id(report.id, 555)
    assert (await storage.get_report(report.id)).bitrix_id == 555  # type: ignore[union-attr]
    assert await storage.get_report(999) is None


# ─────────────── fold_writes ───────────────


def test_fold_add_accumulates() -> None:
    writes = [NewWrite(17, 15, "add", 0), NewWrite(17, 5, "add", 15), NewWrite(44, 10, "add", 0)]
    assert fold_writes(writes) == {17: 20, 44: 10}


def test_fold_set_replaces_earlier_adds() -> None:
    writes = [NewWrite(17, 15, "add", 0), NewWrite(17, 12, "set", 15), NewWrite(17, 3, "add", 12)]
    assert fold_writes(writes) == {17: 15}


def test_fold_zero_drops_the_row() -> None:
    assert fold_writes([NewWrite(17, 15, "add", 0), NewWrite(17, 0, "set", 15)]) == {}


def test_fold_rounds_float_noise() -> None:
    assert fold_writes([NewWrite(17, 0.1, "add", 0), NewWrite(17, 0.2, "add", 0.1)]) == {17: 0.3}


async def test_draft_token_follows_content(storage: Storage) -> None:
    await storage.save_draft(1, "текст", [item(17, 15)])
    first = await storage.get_draft(1)
    await storage.save_draft(1, "текст", [item(17, 15)])  # то же самое — тот же отпечаток
    same = await storage.get_draft(1)
    await storage.save_draft(1, "текст", [item(17, 16)])
    changed = await storage.get_draft(1)
    await storage.save_draft(1, "другой текст", [item(17, 15)])
    other_text = await storage.get_draft(1)

    assert first and same and changed and other_text
    assert first.token == same.token and len(first.token) == 8
    assert len({first.token, changed.token, other_text.token}) == 3
