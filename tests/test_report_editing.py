"""ReportService: правка черновика, защита от старых карточек, синонимы, /today, воркер."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import date
from pathlib import Path

import pytest
from test_report_service import FakeBitrix, FakeMatcher, FakeSheets, ok

from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.services.report import (
    ReportError,
    ReportService,
    StaleCardError,
    run_outbox_worker,
)
from m100bot.sheets.catalog import Catalog
from m100bot.storage.db import Storage

D = date(2026, 11, 2)
USER = 111


def amb(phrase: str, qty: float | None, candidates: list[int], unit: str | None = "шт"):
    return MatchedItem(
        phrase=phrase,
        qty=qty,
        unit=unit,
        status=MatchStatus.AMBIGUOUS,
        candidates=candidates,
    )


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
def service(sheets: FakeSheets, storage: Storage, catalog: Catalog) -> ReportService:
    return ReportService(sheets, storage, FakeMatcher(), FakeBitrix(), today=lambda: D)


async def put(storage: Storage, *items: MatchedItem, text: str = "текст"):
    await storage.save_draft(USER, text, list(items))
    draft = await storage.get_draft(USER)
    assert draft is not None
    return draft


# ─────────────── Выбор варианта ───────────────


async def test_pick_resolves_item_and_marks_it_picked(
    service: ReportService, storage: Storage
) -> None:
    draft = await put(storage, amb("12 хомутов", 12, [43, 44]))
    after = await service.pick_variant(USER, 0, 44, token=draft.token)
    item = after.items[0]
    assert (item.status, item.row, item.candidates, item.picked) == (MatchStatus.OK, 44, [], True)


async def test_pick_with_other_unit_asks_quantity_again(
    service: ReportService, storage: Storage
) -> None:
    draft = await put(storage, amb("короб", 6, [67, 36], unit="м"))
    item = (await service.pick_variant(USER, 0, 67, token=draft.token)).items[0]
    assert (item.status, item.row) == (MatchStatus.CLARIFY, 67)  # в смете «шт», а сказано «м»


async def test_pick_without_quantity_asks_quantity(
    service: ReportService, storage: Storage
) -> None:
    draft = await put(storage, amb("хомуты", None, [43, 44], unit=None))
    item = (await service.pick_variant(USER, 0, 43, token=draft.token)).items[0]
    assert (item.status, item.row) == (MatchStatus.CLARIFY, 43)


async def test_pick_computes_remaining_and_over(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.done = {17: 140.0}
    draft = await put(storage, amb("медь", 300, [17, 20], unit="м"))
    item = (await service.pick_variant(USER, 0, 17, token=draft.token)).items[0]
    assert (item.remaining, item.over_remaining) == (125.0, True)  # план 265 − выполнено 140


async def test_none_of_these_marks_not_found(service: ReportService, storage: Storage) -> None:
    draft = await put(storage, amb("хомуты", 12, [43, 44]))
    item = (await service.pick_variant(USER, 0, None, token=draft.token)).items[0]
    assert (item.status, item.row, item.candidates) == (MatchStatus.NOT_FOUND, None, [])


async def test_pick_of_row_that_was_not_offered_is_refused(
    service: ReportService, storage: Storage
) -> None:
    draft = await put(storage, amb("хомуты", 12, [43, 44]))
    with pytest.raises(StaleCardError):
        await service.pick_variant(USER, 0, 17, token=draft.token)  # поддельная кнопка


# ─────────────── Старые карточки ───────────────


async def test_token_changes_with_every_edit(service: ReportService, storage: Storage) -> None:
    d0 = await put(storage, ok(17, 15), ok(18, 20))
    d1 = await service.set_item_qty(USER, 0, 12, token=d0.token)
    d2 = await service.remove_item(USER, 1, token=d1.token)
    assert d2 is not None
    assert len({d0.token, d1.token, d2.token}) == 3 and len(d0.token) == 8


async def test_old_token_is_rejected_by_every_operation(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    old = await put(storage, ok(17, 15), text="первый")
    await put(storage, ok(18, 7), text="второй")  # черновик заменили

    for call in (
        service.confirm(USER, D, token=old.token),
        service.set_item_qty(USER, 0, 5, token=old.token),
        service.remove_item(USER, 0, token=old.token),
        service.pick_variant(USER, 0, None, token=old.token),
        service.recognize(USER, "ещё", append=True, token=old.token),
        service.get_draft(USER, token=old.token),
    ):
        with pytest.raises(StaleCardError, match="устарела"):
            await call
    assert sheets.calls["write_volumes"] == 0
    assert await storage.get_draft(USER) is not None  # черновик не тронут


async def test_get_draft_without_token_is_plain_lookup(
    service: ReportService, storage: Storage
) -> None:
    assert await service.get_draft(USER) is None
    draft = await put(storage, ok(17, 1))
    assert (await service.get_draft(USER)) == draft
    assert (await service.get_draft(USER, token=draft.token)) == draft
    await storage.delete_draft(USER)
    with pytest.raises(ReportError, match="Нет отчёта"):
        await service.get_draft(USER, token=draft.token)


# ─────────────── Объём, удаление, добавление, отмена ───────────────


async def test_set_qty_uses_sheet_unit_and_recomputes_over(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.done = {67: 0.0}
    clarify = MatchedItem(phrase="к", qty=6, unit="м", status=MatchStatus.CLARIFY, row=67)
    draft = await put(storage, clarify)
    item = (await service.set_item_qty(USER, 0, 3, token=draft.token)).items[0]
    assert (item.status, item.qty, item.unit, item.over_remaining) == (
        MatchStatus.OK,
        3,
        "шт",
        False,
    )

    big = (await service.set_item_qty(USER, 0, 500)).items[0]
    assert big.over_remaining is True and big.remaining == 4  # в смете 4 шт


@pytest.mark.parametrize("qty", [0, -1, float("nan"), float("inf")])
async def test_set_qty_rejects_bad_numbers(
    service: ReportService, storage: Storage, qty: float
) -> None:
    await put(storage, ok(17, 15))
    with pytest.raises(ReportError, match="больше нуля"):
        await service.set_item_qty(USER, 0, qty)


async def test_set_qty_needs_a_chosen_position(service: ReportService, storage: Storage) -> None:
    await put(storage, amb("хомуты", 12, [43, 44]))
    with pytest.raises(ReportError, match="выберите позицию"):
        await service.set_item_qty(USER, 0, 5)


async def test_remove_item_and_last_item_deletes_draft(
    service: ReportService, storage: Storage
) -> None:
    await put(storage, ok(17, 15), ok(18, 20))
    left = await service.remove_item(USER, 0)
    assert left is not None and [i.row for i in left.items] == [18]
    assert await service.remove_item(USER, 0) is None
    assert await storage.get_draft(USER) is None


async def test_item_index_out_of_range_means_stale_card(
    service: ReportService, storage: Storage
) -> None:
    await put(storage, ok(17, 15))
    with pytest.raises(StaleCardError):
        await service.remove_item(USER, 5)


async def test_append_adds_to_the_end(
    service: ReportService, storage: Storage, catalog: Catalog
) -> None:
    await put(storage, ok(17, 15), text="первый")
    service.matcher = FakeMatcher([ok(44, 10)])
    understood = await service.recognize(USER, "второй", append=True)
    draft = await storage.get_draft(USER)
    assert len(understood.items) == 1 and draft is not None
    assert [i.row for i in draft.items] == [17, 44] and draft.source_text == "первый\nвторой"


async def test_append_with_nothing_found_changes_nothing(
    service: ReportService, storage: Storage, catalog: Catalog
) -> None:
    before = await put(storage, ok(17, 15))
    service.matcher = FakeMatcher([])
    understood = await service.recognize(USER, "ничего", append=True)
    assert understood.items == [] and await storage.get_draft(USER) == before


async def test_question_never_touches_draft(
    service: ReportService, storage: Storage, catalog: Catalog
) -> None:
    """«Сколько осталось по …» — только ответ: черновик не меняется ни в каком режиме."""
    before = await put(storage, ok(17, 15))
    matcher = FakeMatcher([ok(55, 1)])
    matcher.intent = "question"
    service.matcher = matcher
    for append in (False, True):
        understood = await service.recognize(USER, "сколько осталось кабеля", append=append)
        assert understood.intent == "question"
        assert await storage.get_draft(USER) == before


async def test_remaining_lines(service: ReportService, sheets: FakeSheets) -> None:
    sheets.done = {55: 112.0, 48: 20.0}
    lines = await service.remaining([55, 48, 4])  # 4 — не позиция: пропускается
    assert [(ln.row, ln.planned, ln.done, ln.left, ln.unit) for ln in lines] == [
        (55, 182.0, 112.0, 70.0, "м"),
        (48, 18.0, 20.0, -2.0, "шт"),
    ]


async def test_cancel_deletes_draft_only(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await put(storage, ok(17, 15))
    await service.cancel(USER)
    assert await storage.get_draft(USER) is None and sheets.writes == []


# ─────────────── Запись: «нет в смете» и сообщения ───────────────


async def test_confirm_drops_not_found_and_reports_it(
    service: ReportService, storage: Storage
) -> None:
    gone = MatchedItem(phrase="покрасили стену", qty=20, unit="м2", status=MatchStatus.NOT_FOUND)
    await put(storage, ok(17, 15), gone)
    text = await service.confirm(USER, D)
    assert "Нет в смете: «покрасили стену»" in text and "❓" not in text
    assert await storage.get_draft(USER) is None  # «нет в смете» черновик не держит


async def test_confirm_with_only_not_found(service: ReportService, storage: Storage) -> None:
    gone = MatchedItem(phrase="стена", qty=20, unit="м2", status=MatchStatus.NOT_FOUND)
    await put(storage, gone)
    with pytest.raises(ReportError, match="этих работ нет в смете"):
        await service.confirm(USER, D)


async def test_decimal_comma_in_texts(service: ReportService, storage: Storage) -> None:
    await put(storage, ok(28, 0.5))
    assert "— 0,5 кг" in await service.confirm(USER, D)


# ─────────────── Синонимы «за кадром» ───────────────


def picked(item: MatchedItem) -> MatchedItem:
    return item.model_copy(update={"picked": True})


async def test_only_picked_items_are_learned(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    chosen = picked(ok(44, 12).model_copy(update={"phrase": "поставили 12 хомутов"}))
    await put(storage, ok(17, 15), chosen)
    await service.confirm(USER, D)
    await service.drain()
    assert sheets.added_synonyms == [("хомутов", 44)]  # ни объёма, ни глагола


async def test_synonym_failure_does_not_break_confirm(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    sheets.synonym_error = RuntimeError("таблица не отвечает")
    await put(storage, picked(ok(44, 12).model_copy(update={"phrase": "хомутов 12"})))
    text = await service.confirm(USER, D)
    await service.drain()
    assert text.startswith("✅ Записано")  # мастер ничего не заметил
    assert sheets.cells[(D, 44)] == 12


async def test_too_short_key_is_not_learned(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    await put(storage, picked(ok(17, 15).model_copy(update={"phrase": "15 м"})))
    await service.confirm(USER, D)
    await service.drain()
    assert sheets.added_synonyms == []


async def test_synonyms_are_not_learned_when_writing_fails(
    service: ReportService, sheets: FakeSheets, storage: Storage
) -> None:
    from m100bot.sheets.client import SheetsError

    sheets.write_errors = [SheetsError("Таблица недоступна.")]
    await put(storage, picked(ok(44, 12).model_copy(update={"phrase": "хомутов 12"})))
    with pytest.raises(ReportError):
        await service.confirm(USER, D)
    await service.drain()
    assert sheets.added_synonyms == []


# ─────────────── /today ───────────────


async def test_today_report_follows_the_journal(service: ReportService, storage: Storage) -> None:
    await put(storage, ok(17, 15), ok(44, 10))
    await service.confirm(USER, D)
    await service.correct_today(USER, 17, 12)  # заменили 15 → 12

    day, lines = await service.today_report(USER)
    assert day == D
    assert [(ln.row, ln.qty, ln.unit) for ln in lines] == [(17, 12, "м"), (44, 10, "шт")]
    assert lines[0].name == "Фреонопровод медный, Д6,35 TEC TUBE cips"

    await service.correct_today(USER, 17, 0)  # очистили — строка пропала
    assert [ln.row for ln in (await service.today_report(USER))[1]] == [44]


async def test_today_report_ignores_other_users_and_days(
    service: ReportService, storage: Storage
) -> None:
    await storage.save_draft(1, "a", [ok(17, 5)])
    await storage.save_draft(2, "b", [ok(18, 7)])
    await service.confirm(1, D)
    await service.confirm(2, D)
    await storage.save_draft(1, "c", [ok(44, 3)])
    await service.confirm(1, date(2026, 11, 1))  # вчера
    assert [ln.row for ln in (await service.today_report(1))[1]] == [17]


async def test_today_report_empty(service: ReportService) -> None:
    assert await service.today_report(USER) == (D, [])


# ─────────────── Прочее ───────────────


async def test_table_link(service: ReportService) -> None:
    assert service.table_link() == "https://sheet.test/edit#range=B1"
    assert service.table_link(17) == "https://sheet.test/edit#range=B17"
    assert service.today() == D


async def test_outbox_worker_survives_errors_and_stops_on_cancel(service: ReportService) -> None:
    calls: list[int] = []
    enough = asyncio.Event()

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("упал")
        if len(calls) >= 3:
            enough.set()

    service.flush_outbox = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(run_outbox_worker(service, interval_s=0))
    await asyncio.wait_for(enough.wait(), timeout=2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert len(calls) >= 3 and task.cancelled()
