"""Ручная проверка SheetsClient на настоящей таблице.

    python scripts/try_sheets.py            # только чтение: справочник и план, без изменений
    python scripts/try_sheets.py --write    # + создать столбец даты, записать, прочитать обратно

Настройки — из .env (GOOGLE_CREDENTIALS_FILE, SPREADSHEET_ID, WORKSHEET_NAME).

--write ИЗМЕНЯЕТ таблицу: вставляет столбец с датой (по умолчанию сегодня) и ПРИБАВЛЯЕТ объём
к ячейке (по умолчанию 1 в строку 17). Запускайте на копии таблицы. Повторный запуск прибавит
ещё раз. Откатить вручную: удалить созданный столбец (или вернуть значение в ячейке).

Скрипт проверяет и главную ловушку образца: колонка I «Выполненный объем» = SUM(J:CZ)
должна вырасти на записанный объём, то есть формула «видит» новый столбец.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from loguru import logger

from m100bot.config import SheetsSettings
from m100bot.sheets.addressing import col_letter, plan_date_column
from m100bot.sheets.client import SheetsClient, SheetsError

ROOT = Path(__file__).resolve().parent.parent


async def run(
    client: SheetsClient,
    d: date,
    row: int,
    qty: float,
    *,
    write: bool,
    out: Callable[[str], None] = print,
) -> int:
    """Возвращает код выхода: 0 — всё в порядке, 1 — проверка не прошла, 2 — неверные данные."""
    catalog = await client.load_catalog()
    pos = catalog.by_row(row)
    if pos is None:
        out(f"Строка {row} не является позицией сметы (позиции: {catalog.positions[0].row}…).")
        return 2

    first_col = min(c for cols in catalog.date_columns.values() for c in cols)
    out(f"Справочник: {len(catalog.positions)} позиций, {len(catalog.date_columns)} дат "
        f"({col_letter(first_col)}…{col_letter(catalog.last_col)})")  # fmt: skip
    for dup, cols in catalog.duplicate_dates().items():
        out(f"  ⚠️ дата {dup:%d.%m.%Y} в столбцах {', '.join(col_letter(c) for c in cols)}")
    out(f"Позиция: строка {row}, № {pos.number}, {pos.name[:60]!r}, ед. «{pos.unit}»")

    plan = plan_date_column(catalog.date_columns, catalog.last_col, d)
    if plan.exists:
        out(f"Дата {d:%d.%m.%Y}: столбец {col_letter(plan.col)} уже есть")
    else:
        out(f"Дата {d:%d.%m.%Y}: будет создан столбец {col_letter(plan.col)}")
        if plan.extend_sum_to:
            out("  ⚠️ он правее границы SUM(J:CZ) — формулы в колонке I будут растянуты")

    if not write:
        out("\nРежим чтения: таблица не изменена. Добавьте --write, чтобы выполнить запись.")
        return 0

    volume_before = (await client.read_volumes(d, [row]))[row]
    done_before = (await client.read_done([row]))[row]
    out(f"\nДо записи: за {d:%d.%m} = {volume_before:g}, «Выполненный объем» = {done_before:g}")

    [change] = await client.write_volumes(d, {row: qty}, mode="add")
    out(f"Записано: строка {row}: {change.old:g} → {change.new:g}")

    volume_after = (await client.read_volumes(d, [row]))[row]
    done_after = (await client.read_done([row]))[row]
    created = (await client.load_catalog(force=True)).column_for(d)
    out(f"После записи: столбец {col_letter(created) if created else '?'}, "
        f"за {d:%d.%m} = {volume_after:g}, «Выполненный объем» = {done_after:g}")  # fmt: skip

    ok_cell = volume_after == volume_before + qty
    ok_sum = done_after == done_before + qty
    out(f"{'✅' if ok_cell else '❌'} значение в ячейке прочиталось обратно")
    out(f"{'✅' if ok_sum else '❌'} формула «Выполненный объем» учла новый столбец")
    out(f"Строка в таблице: {client.row_link(row)}")
    return 0 if ok_cell and ok_sum else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--write", action="store_true", help="выполнить запись (по умолчанию только чтение)"
    )
    ap.add_argument(
        "--date", type=date.fromisoformat, default=None, help="YYYY-MM-DD, по умолчанию сегодня"
    )
    ap.add_argument("--row", type=int, default=17)
    ap.add_argument("--qty", type=float, default=1.0)
    args = ap.parse_args()

    settings = SheetsSettings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
    if not settings.google_credentials_file.is_absolute():
        settings = settings.model_copy(
            update={"google_credentials_file": ROOT / settings.google_credentials_file}
        )
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

    client = SheetsClient.from_settings(settings)
    try:
        return asyncio.run(
            run(client, args.date or date.today(), args.row, args.qty, write=args.write)
        )
    except SheetsError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
