"""Эталонный набор: целостность (офлайн) и точность сопоставления (нужна запущенная LLM).

pytest tests/test_golden.py -v           # всё
pytest tests/test_golden.py -k integrity # только проверка самого набора
"""

import urllib.request
from collections import Counter
from pathlib import Path

import pytest
from conftest import load_golden

from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.sheets.catalog import Catalog

CASES = load_golden()
ROOT = Path(__file__).resolve().parent.parent


# ─────────────── Целостность набора (работает без сети) ───────────────


def test_integrity_unique_ids() -> None:
    dupes = [k for k, v in Counter(c["id"] for c in CASES).items() if v > 1]
    assert not dupes, f"повторяющиеся id: {dupes}"


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_integrity_rows_exist(case: dict, catalog: Catalog) -> None:
    for exp in case["expected"]:
        rows = ([exp["row"]] if "row" in exp else []) + exp.get("candidates", [])
        for r in rows:
            assert catalog.by_row(r) is not None, f"строка {r} не является позицией сметы"
        status = exp.get("status", "ok")
        assert status in {s.value for s in MatchStatus}
        if status == "ok":
            assert "row" in exp and "qty" in exp
        if status == "ambiguous":
            assert len(exp["candidates"]) >= 2


# ─────────────── Точность сопоставления (нужна Ollama или другой LLM) ───────────────


def _key(item: MatchedItem) -> tuple:
    if item.status == MatchStatus.OK:
        return ("ok", item.row, item.qty)
    if item.status == MatchStatus.AMBIGUOUS:
        return ("ambiguous", tuple(sorted(item.candidates)))
    if item.status == MatchStatus.CLARIFY:
        return ("clarify", item.row)
    return ("not_found",)


def _exp_key(exp: dict) -> tuple:
    status = exp.get("status", "ok")
    if status == "ok":
        return ("ok", exp["row"], float(exp["qty"]))
    if status == "ambiguous":
        return ("ambiguous", tuple(sorted(exp["candidates"])))
    if status == "clarify":
        return ("clarify", exp["row"])
    return ("not_found",)


@pytest.fixture(scope="module")
def matcher(catalog: Catalog):
    from m100bot.config import LlmSettings
    from m100bot.matching.llm import LlmExtractor
    from m100bot.matching.matcher import Matcher

    settings = LlmSettings(_env_file=ROOT / ".env")  # type: ignore[call-arg]
    base_url = settings.llm_base_url
    try:
        urllib.request.urlopen(f"{base_url}/models", timeout=3)
    except OSError:
        pytest.skip(f"LLM недоступна по {base_url} — запусти Ollama (тест точности пропущен)")

    return Matcher(LlmExtractor.from_settings(settings))


@pytest.mark.asyncio(loop_scope="module")  # один loop на модуль: HTTP-клиент matcher'а общий
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_match(case: dict, matcher, catalog: Catalog) -> None:
    try:
        items = await matcher.match(case["text"], catalog)
    except NotImplementedError:
        pytest.skip("Matcher.match ещё не реализован")
    got = Counter(_key(i) for i in items)
    expected = Counter(_exp_key(e) for e in case["expected"])
    assert got == expected, f"\nфраза: {case['text']}\nожидали: {expected}\nполучили: {got}"
