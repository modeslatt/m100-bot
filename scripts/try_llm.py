"""Ручная проверка LLM: фраза из аргумента → JSON-ответ модели.

    python scripts/try_llm.py "медь 6-ка 15 м"
    python scripts/try_llm.py --xlsx "пример таблица.xlsx" "кабель 3х2,5 56 м"

Справочник берётся из локального xlsx (по умолчанию tests/fixtures/sample.xlsx),
все настройки LLM — из .env через m100bot.config.LlmSettings.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import openpyxl

from m100bot.config import LlmSettings
from m100bot.matching.llm import LlmError, LlmExtractor
from m100bot.sheets.catalog import parse_catalog

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("text", help="фраза мастера")
    ap.add_argument("--xlsx", type=Path, default=ROOT / "tests/fixtures/sample.xlsx")
    args = ap.parse_args()

    s = LlmSettings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
    ws = openpyxl.load_workbook(args.xlsx).active
    catalog = parse_catalog([list(r) for r in ws.iter_rows(values_only=True)])
    ext = LlmExtractor.from_settings(s)

    started = time.perf_counter()
    try:
        res = await ext.extract(args.text, catalog)
    except LlmError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print(f"# модель: {s.llm_model}, {time.perf_counter() - started:.1f} с")
    for m in res.mentions:
        print(f"\n«{m.phrase}» → qty={m.qty} unit={m.unit}")
        for r in m.candidates:
            p = catalog.by_row(r)
            print(f"    {r}: {p.name[:60]}" if p else f"    {r}: ??? нет такой позиции")
        if m.note:
            print(f"    note: {m.note}")
    print("\n# raw JSON:")
    print(res.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
