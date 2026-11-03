"""SheetsClient на имитации gspread (tests/fake_sheets.py): без сети и без реальной таблицы."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests
from fake_sheets import FakeGspread, make_gspread
from gspread.exceptions import APIError
from loguru import logger

from m100bot.sheets.addressing import col_index, date_to_serial
from m100bot.sheets.client import SheetChangedError, SheetsClient, SheetsError

NEW_DATE = date(2026, 11, 2)  # после последней даты образца (30.09)
AX = col_index("AX")  # первый столбец после AW — последней даты образца


def api_error(status: int, message: str = "boom") -> APIError:
    resp = requests.Response()
    resp.status_code = status
    body = {"error": {"code": status, "message": message, "status": "X"}}
    resp._content = json.dumps(body).encode()
    return APIError(resp)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_client(fake: FakeGspread, clock: Clock | None = None, **kw: Any) -> SheetsClient:
    return SheetsClient(
        Path("credentials.json"),
        fake.spreadsheet_id,
        "Лист1",
        retry_wait_s=0,
        clock=clock or Clock(),
        gspread_client=fake,
        **kw,
    )


@pytest.fixture
def fake(sample_rows: list[list[Any]]) -> FakeGspread:
    return make_gspread(sample_rows)


@pytest.fixture
def client(fake: FakeGspread) -> SheetsClient:
    return make_client(fake)


def ws_of(fake: FakeGspread):
    return fake.spreadsheet.sheets["Лист1"]


# ─────────────── load_catalog и кэш ───────────────


async def test_load_catalog_parses_sample(client: SheetsClient) -> None:
    catalog = await client.load_catalog()
    assert len(catalog.positions) == 62
    assert catalog.by_row(17).number == 13  # type: ignore[union-attr]
    assert catalog.last_col == col_index("AW")
    assert date(2026, 9, 30) in catalog.duplicate_dates()  # ловушка образца


async def test_catalog_cached_for_ttl(fake: FakeGspread) -> None:
    clock = Clock()
    client = make_client(fake, clock)
    first = await client.load_catalog()
    clock.now += 299
    assert await client.load_catalog() is first
    assert fake.spreadsheet.calls["get_values"] == 1

    clock.now += 2  # прошло 5 минут
    assert await client.load_catalog() is not first
    assert fake.spreadsheet.calls["get_values"] == 2


async def test_force_reloads_catalog(client: SheetsClient, fake: FakeGspread) -> None:
    await client.load_catalog()
    await client.load_catalog(force=True)
    assert fake.spreadsheet.calls["get_values"] == 2


# ─────────────── ensure_date_column ───────────────


async def test_existing_date_needs_no_changes(client: SheetsClient, fake: FakeGspread) -> None:
    col = await client.ensure_date_column(date(2026, 8, 25))
    assert col == col_index("N")  # даты идут с пропусками (выходные): 19.08 в J, 25.08 в N
    assert fake.spreadsheet.structure_updates == []


async def test_duplicate_date_goes_to_first_column_and_warns(client: SheetsClient) -> None:
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        col = await client.ensure_date_column(date(2026, 9, 30))
    finally:
        logger.remove(sink)
    assert col == col_index("AV")
    assert any("AV, AW" in w for w in warnings)


async def test_new_date_inserted_after_last_date(client: SheetsClient, fake: FakeGspread) -> None:
    ws = ws_of(fake)
    width_before = ws.col_count
    labels_before = [ws.get_cell(4, c) for c in range(col_index("BA"), ws.col_count + 1)]

    col = await client.ensure_date_column(NEW_DATE)

    assert col == AX
    assert ws.get_cell(3, AX) == date_to_serial(NEW_DATE)
    assert ws.get_cell(4, AX) == "объем"
    assert ws.get_cell(3, AX - 1) == date_to_serial(date(2026, 9, 30))  # AW не тронут
    assert ws.col_count == width_before + 1
    # заранее напечатанные «объем» правее сдвинулись на один столбец, а не затёрлись
    assert [ws.get_cell(4, c) for c in range(col_index("BB"), ws.col_count + 1)] == labels_before
    # одна структурная операция: вставка и запись заголовка вместе
    assert len(fake.spreadsheet.structure_updates) == 1


async def test_second_call_finds_the_new_column(client: SheetsClient, fake: FakeGspread) -> None:
    await client.ensure_date_column(NEW_DATE)
    assert await client.ensure_date_column(NEW_DATE) == AX
    assert len(fake.spreadsheet.structure_updates) == 1


async def test_catalog_sees_new_column_after_ensure(client: SheetsClient) -> None:
    await client.load_catalog()
    await client.ensure_date_column(NEW_DATE)
    catalog = await client.load_catalog()  # кэш сброшен — читаем заново
    assert catalog.column_for(NEW_DATE) == AX and catalog.last_col == AX


async def test_lost_response_does_not_insert_second_column(fake: FakeGspread) -> None:
    """Google применил вставку, но ответ потерялся (503): повтор не должен вставить ещё один."""
    client = make_client(fake)
    fake.spreadsheet.faults_after["batch_update"] = [api_error(503)]
    col = await client.ensure_date_column(NEW_DATE)
    assert col == AX
    assert len(fake.spreadsheet.structure_updates) == 1  # вставка выполнена один раз
    assert ws_of(fake).get_cell(3, AX + 1) is None  # соседнего столбца с датой нет


async def test_sum_picks_up_new_column(client: SheetsClient) -> None:
    """Главная ловушка образца: «Выполненный объем» (I) = SUM(J:CZ) должен видеть новый столбец."""
    before = (await client.read_done([17]))[17]
    await client.write_volumes(NEW_DATE, {17: 1})
    assert (await client.read_done([17]))[17] == before + 1


# ─────────────── Граница CZ ───────────────


async def test_column_beyond_cz_extends_sum_formulas(sample_rows: list[list[Any]]) -> None:
    """Даты уже заполнили J…CZ (104). Новый столбец — DA (105) — вне SUM(J:CZ)."""
    fake = make_gspread(sample_rows, dates_to_col=104)
    ws = ws_of(fake)
    assert ws.get_cell(5, 9) == "=SUM(J5:CZ5)"
    client = make_client(fake)

    col = await client.ensure_date_column(date(2026, 12, 31))

    assert col == 105
    assert ws.get_cell(3, 105) == date_to_serial(date(2026, 12, 31))
    assert ws.get_cell(5, 9) == "=SUM(J5:DA5)"  # растянуто
    assert ws.get_cell(52, 9) == "=SUM(J52:DA52)"  # и в итоговой строке
    assert ws.get_cell(6, 9) == "=SUM(J6:DA6)"

    await client.write_volumes(date(2026, 12, 31), {17: 7})
    assert (await client.read_done([17]))[17] == 7  # объём виден в итоге


async def test_last_column_inside_cz_needs_no_formula_rewrite(
    sample_rows: list[list[Any]],
) -> None:
    fake = make_gspread(sample_rows, dates_to_col=103)
    client = make_client(fake)
    col = await client.ensure_date_column(date(2026, 12, 31))
    assert col == 104  # CZ
    # формулы сами растянулись вставкой внутри диапазона; явных переписываний не было
    assert not any(
        "formulaValue" in json.dumps(req)
        for batch in fake.spreadsheet.structure_updates
        for req in batch
    )
    await client.write_volumes(date(2026, 12, 31), {17: 3})
    assert (await client.read_done([17]))[17] == 3


# ─────────────── write_volumes ───────────────


async def test_add_accumulates_in_one_batch(client: SheetsClient, fake: FakeGspread) -> None:
    changes = await client.write_volumes(NEW_DATE, {17: 15, 18: 12, 55: 56})
    assert [(c.row, c.old, c.new) for c in changes] == [(17, 0, 15), (18, 0, 12), (55, 0, 56)]
    assert len(fake.spreadsheet.value_writes) == 1  # один batch_update на весь отчёт

    again = await client.write_volumes(NEW_DATE, {17: 5})
    assert [(c.old, c.new) for c in again] == [(15, 20)]
    assert ws_of(fake).get_cell(17, AX) == 20
    assert ws_of(fake).get_cell(18, AX) == 12  # соседние строки не тронуты


async def test_set_replaces_value(client: SheetsClient, fake: FakeGspread) -> None:
    await client.write_volumes(NEW_DATE, {17: 15})
    [change] = await client.write_volumes(NEW_DATE, {17: 4}, mode="set")
    assert (change.old, change.new) == (15, 4)
    assert ws_of(fake).get_cell(17, AX) == 4


async def test_set_zero_clears_cell(client: SheetsClient, fake: FakeGspread) -> None:
    await client.write_volumes(NEW_DATE, {17: 15})
    await client.write_volumes(NEW_DATE, {17: 0}, mode="set")
    assert ws_of(fake).get_cell(17, AX) is None


async def test_writes_into_existing_date_column(client: SheetsClient, fake: FakeGspread) -> None:
    """25.08 уже есть; в строке 17 там стояло 15 — прибавляем, столбцов не создаём."""
    changes = await client.write_volumes(date(2026, 8, 25), {17: 5})
    assert (changes[0].old, changes[0].new) == (15, 20)
    assert fake.spreadsheet.structure_updates == []


async def test_decimal_volume(client: SheetsClient, fake: FakeGspread) -> None:
    await client.write_volumes(NEW_DATE, {28: 0.5})
    await client.write_volumes(NEW_DATE, {28: 0.25})
    assert ws_of(fake).get_cell(28, AX) == 0.75


async def test_empty_report_touches_nothing(client: SheetsClient, fake: FakeGspread) -> None:
    assert await client.write_volumes(NEW_DATE, {}) == []
    assert fake.spreadsheet.calls["get_values"] == 0
    assert fake.spreadsheet.structure_updates == [] and fake.spreadsheet.value_writes == []


async def test_zero_add_writes_nothing(client: SheetsClient, fake: FakeGspread) -> None:
    assert await client.write_volumes(NEW_DATE, {17: 0}) == []
    assert fake.spreadsheet.value_writes == []


async def test_rejects_unknown_row_before_any_write(
    client: SheetsClient, fake: FakeGspread
) -> None:
    for bad in ({4: 1}, {52: 1}, {999: 1}):  # раздел, «ИТОГО», за пределами
        with pytest.raises(ValueError, match="не являются позициями"):
            await client.write_volumes(NEW_DATE, bad)
    assert fake.spreadsheet.structure_updates == []  # столбец даты не создан зря


async def test_rejects_bad_quantity_without_network(
    client: SheetsClient, fake: FakeGspread
) -> None:
    with pytest.raises(ValueError):
        await client.write_volumes(NEW_DATE, {17: -5})
    assert fake.spreadsheet.calls["get_values"] == 0


async def test_refuses_to_overwrite_text_in_cell(client: SheetsClient, fake: FakeGspread) -> None:
    await client.ensure_date_column(NEW_DATE)
    ws_of(fake).set_cell(17, AX, "см. акт")
    with pytest.raises(SheetsError, match="строке 17"):
        await client.write_volumes(NEW_DATE, {17: 1})
    assert ws_of(fake).get_cell(17, AX) == "см. акт"


async def test_retry_of_write_does_not_double_add(client: SheetsClient, fake: FakeGspread) -> None:
    """Запись применилась, но ответ потерялся: повтор пишет те же абсолютные значения."""
    fake.spreadsheet.faults_after["ws.batch_update"] = [api_error(503)]
    changes = await client.write_volumes(NEW_DATE, {17: 15})
    assert (changes[0].old, changes[0].new) == (0, 15)
    assert ws_of(fake).get_cell(17, AX) == 15  # не 30


async def test_non_number_quantity_rejected(client: SheetsClient) -> None:
    with pytest.raises(ValueError):
        await client.write_volumes(NEW_DATE, {17: "=1+1"})  # type: ignore[dict-item]


# ─────────────── Сдвиг строк ───────────────


async def test_row_shift_is_detected_and_nothing_is_written(
    client: SheetsClient, fake: FakeGspread
) -> None:
    """Пока мастер подтверждал отчёт, кто-то вставил строку выше позиции 17."""
    await client.load_catalog()
    await client.ensure_date_column(NEW_DATE)
    ws_of(fake).insert_row(10)  # строки 17, 18, … съехали на одну вниз
    with pytest.raises(SheetChangedError, match="сместились"):
        await client.write_volumes(NEW_DATE, {17: 15})
    assert fake.spreadsheet.value_writes == []


# ─────────────── Ошибки Google ───────────────


async def test_permanent_503_gives_up_after_three_attempts(fake: FakeGspread) -> None:
    client = make_client(fake)
    fake.spreadsheet.faults["get_values"] = [api_error(503)] * 5
    with pytest.raises(SheetsError, match="Таблица недоступна"):
        await client.load_catalog()
    assert fake.spreadsheet.calls["get_values"] == 3


async def test_transient_then_ok(fake: FakeGspread) -> None:
    client = make_client(fake)
    fake.spreadsheet.faults["get_values"] = [api_error(429), requests.exceptions.ConnectionError()]
    assert len((await client.load_catalog()).positions) == 62


@pytest.mark.parametrize(
    ("status", "text"),
    [(403, "Нет доступа"), (404, "Таблица не найдена"), (400, "Не удалось")],
)
async def test_permanent_errors_not_retried(fake: FakeGspread, status: int, text: str) -> None:
    client = make_client(fake)
    fake.spreadsheet.faults["get_values"] = [api_error(status)] * 3
    with pytest.raises(SheetsError, match=text):
        await client.load_catalog()
    assert fake.spreadsheet.calls["get_values"] == 1


async def test_excel_file_instead_of_google_sheet(fake: FakeGspread) -> None:
    """Настоящий ответ Google, когда по ID лежит .xlsx, не конвертированный в Google Таблицу."""
    client = make_client(fake)
    office = "This operation is not supported for this document. The document must not be an Office file."  # noqa: E501
    fake.spreadsheet.faults["get_values"] = [api_error(400, office)]
    with pytest.raises(SheetsError, match="Excel-файл.*Сохранить как Google Таблицы"):
        await client.load_catalog()
    assert fake.spreadsheet.calls["get_values"] == 1  # повторять бессмысленно


async def test_wrong_spreadsheet_id(fake: FakeGspread) -> None:
    client = SheetsClient(
        Path("credentials.json"), "OTHER_ID", "Лист1", gspread_client=fake, retry_wait_s=0
    )
    with pytest.raises(SheetsError, match="SPREADSHEET_ID"):
        await client.load_catalog()


async def test_missing_worksheet(fake: FakeGspread) -> None:
    client = SheetsClient(
        Path("credentials.json"), fake.spreadsheet_id, "Нет такого", gspread_client=fake
    )
    with pytest.raises(SheetsError, match="WORKSHEET_NAME"):
        await client.load_catalog()


async def test_missing_credentials_file(tmp_path: Path) -> None:
    client = SheetsClient(tmp_path / "нет.json", "ID", "Лист1")
    with pytest.raises(SheetsError, match="файл ключа"):
        await client.load_catalog()


# ─────────────── Синонимы ───────────────


async def test_synonyms_sheet_is_created_when_missing(
    client: SheetsClient, fake: FakeGspread
) -> None:
    assert await client.load_synonyms() == {}
    sheet = fake.spreadsheet.sheets["Синонимы"]
    assert sheet.get_values()[0][0] == "Фраза мастера"
    await client.load_synonyms()
    assert fake.spreadsheet.calls["add_worksheet"] == 1  # второй раз не создаём


async def put_synonym(
    client: SheetsClient, fake: FakeGspread, phrase: str, row: int, title_row: int
) -> None:
    """Синоним, вписанный человеком: фраза, строка, название позиции из строки title_row."""
    await client.load_synonyms()  # создаёт лист с заголовком
    title = (await client.load_catalog()).by_row(title_row).name  # type: ignore[union-attr]
    sheet = fake.spreadsheet.sheets["Синонимы"]
    for col, value in enumerate((phrase, row, title), start=1):
        sheet.set_cell(2, col, value)
    client._synonyms = None


async def test_manual_synonym_is_loaded(client: SheetsClient, fake: FakeGspread) -> None:
    await put_synonym(client, fake, "  Гофра   20-ка ", 64, 64)
    assert await client.load_synonyms() == {"гофра 20-ка": 64}


async def test_stale_synonym_after_row_shift_is_skipped(
    client: SheetsClient, fake: FakeGspread
) -> None:
    await put_synonym(client, fake, "гофра", 65, 64)  # номер поправили, название осталось
    assert await client.load_synonyms() == {}  # название в C не совпало с позицией 65


# ─────────────── Прочее ───────────────


async def test_row_link_has_gid_after_connect(client: SheetsClient) -> None:
    assert client.row_link(17) == "https://docs.google.com/spreadsheets/d/SHEET_ID/edit#range=B17"
    await client.load_catalog()
    assert client.row_link(17).endswith("/edit#gid=1000&range=B17")


async def test_read_volumes(client: SheetsClient) -> None:
    got = await client.read_volumes(date(2026, 8, 25), [17, 19, 55])
    assert got == {17: 15, 19: 0, 55: 56}  # в строке 19 на 25.08 пусто
    assert await client.read_volumes(NEW_DATE, [17]) == {17: 0}  # такой даты ещё нет


def test_from_settings() -> None:
    from m100bot.config import SheetsSettings

    s = SheetsSettings(_env_file=None, spreadsheet_id="ABC", worksheet_name="W")  # type: ignore[call-arg]
    c = SheetsClient.from_settings(s)
    assert c.row_link(5) == "https://docs.google.com/spreadsheets/d/ABC/edit#range=B5"
