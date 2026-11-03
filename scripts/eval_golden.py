"""Прогон эталона tests/golden/phrases.yaml через Matcher с реальной LLM.

    python scripts/eval_golden.py               # таблица id / ожидали / получили + точность
    python scripts/eval_golden.py -k cab        # только кейсы, где id содержит «cab»

Справочник — tests/fixtures/sample.xlsx, настройки LLM — из .env (m100bot.config.LlmSettings).
"""

import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

import openpyxl
import yaml
from loguru import logger

from m100bot.config import LlmSettings
from m100bot.matching.llm import LlmError, LlmExtractor
from m100bot.matching.matcher import Matcher
from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.sheets.catalog import parse_catalog

ROOT = Path(__file__).resolve().parent.parent


def got_key(i: MatchedItem) -> str:
    if i.status == MatchStatus.OK:
        return f"{i.row}×{i.qty:g}"
    if i.status == MatchStatus.AMBIGUOUS:
        return f"?{sorted(i.candidates)}"
    if i.status == MatchStatus.CLARIFY:
        return f"clarify {i.row if i.row is not None else sorted(i.candidates)}"
    return "not_found"


def exp_key(e: dict) -> str:
    status = e.get("status", "ok")
    if status == "ok":
        return f"{e['row']}×{float(e['qty']):g}"
    if status == "ambiguous":
        return f"?{sorted(e['candidates'])}"
    if status == "clarify":
        return f"clarify {e['row']}"
    return "not_found"


def fmt(keys: Counter[str]) -> str:
    return "; ".join(sorted(keys.elements())) or "—"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="фильтр по id")
    args = ap.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    s = LlmSettings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
    ws = openpyxl.load_workbook(ROOT / "tests/fixtures/sample.xlsx").active
    catalog = parse_catalog([list(r) for r in ws.iter_rows(values_only=True)])
    matcher = Matcher(LlmExtractor.from_settings(s))

    cases = yaml.safe_load((ROOT / "tests/golden/phrases.yaml").read_text(encoding="utf-8"))
    cases = [c for c in cases if args.k in c["id"]]
    passed = 0
    print(f"модель: {s.llm_model}\n")
    print("| id | ожидали | получили | с | |\n|---|---|---|---|---|")
    started_all = time.perf_counter()
    for c in cases:
        exp = Counter(exp_key(e) for e in c["expected"])
        exp_intent = c.get("intent", "report")
        t0 = time.perf_counter()
        try:
            understood = await matcher.understand(c["text"], catalog)
            got = Counter(got_key(i) for i in understood.items)
            got_intent = understood.intent
            got_s = fmt(got) + ("" if got_intent == exp_intent else f" [{got_intent}]")
        except LlmError as e:
            got, got_intent, got_s = Counter(), "", f"ошибка LLM: {e}"
        ok = got == exp and got_intent == exp_intent
        passed += ok
        dt = time.perf_counter() - t0
        print(f"| {c['id']} | {fmt(exp)} | {got_s} | {dt:.1f} | {'✅' if ok else '❌'} |")
    total = time.perf_counter() - started_all
    print(f"\nТочность: {passed}/{len(cases)} = {passed / max(len(cases), 1):.0%}")
    print(f"Время: всего {total:.0f} с, в среднем {total / max(len(cases), 1):.1f} с на фразу")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
