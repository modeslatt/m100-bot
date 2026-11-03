"""LlmExtractor без сети: фейковый клиент вместо OpenAI SDK."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from loguru import logger
from openai import APIConnectionError, APITimeoutError, AuthenticationError, InternalServerError

from m100bot.config import LlmSettings
from m100bot.matching.features import extract_features
from m100bot.matching.llm import (
    LlmError,
    LlmExtractor,
    LlmProvider,
    format_catalog,
    parse_response,
    strict_json_schema,
)
from m100bot.matching.models import LlmResponse
from m100bot.matching.normalize import compact_name
from m100bot.sheets.catalog import Catalog

GOOD = (
    '{"mentions": [{"phrase": "медь 6-ка 15 м", "qty": 15, "unit": "м",'
    ' "candidates": [17], "note": null}]}'
)
REQ = httpx.Request("POST", "https://llm.test/chat/completions")


class FakeCompletions:
    def __init__(self, outcomes: list[Any], prompt_tokens: int = 3000) -> None:
        self.outcomes = outcomes
        self.prompt_tokens = prompt_tokens
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        msg = SimpleNamespace(content=out)
        usage = SimpleNamespace(prompt_tokens=self.prompt_tokens, completion_tokens=100)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def fake_client(completions: FakeCompletions) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def make(outcomes: list[Any], **kw: Any) -> tuple[LlmExtractor, FakeCompletions]:
    completions = FakeCompletions(outcomes)
    ext = LlmExtractor("", "", "test-model", retry_wait=0, client=fake_client(completions), **kw)
    return ext, completions


def make_with_fallback(
    primary: list[Any], fallback: list[Any]
) -> tuple[LlmExtractor, FakeCompletions, FakeCompletions]:
    p, f = FakeCompletions(primary), FakeCompletions(fallback)
    ext = LlmExtractor(
        "",
        "",
        "local",
        retry_wait=0,
        disable_thinking=True,
        fallback=LlmProvider("https://cloud.test/v1", "key", "cloud"),
        client=fake_client(p),
        fallback_client=fake_client(f),
    )
    return ext, p, f


async def test_success_request_shape(catalog: Catalog) -> None:
    ext, comp = make([GOOD])
    res = await ext.extract("медь 6-ка 15 м", catalog)
    assert res.mentions[0].candidates == [17]
    call = comp.calls[0]
    assert call["temperature"] == 0
    assert call["seed"] == 42
    assert call["max_tokens"] == 2048
    assert call["response_format"]["type"] == "json_schema"
    user = call["messages"][1]["content"]
    assert "17|Фреонопровод медный" in user and "медь 6-ка 15 м" in user


async def test_retries_invalid_json_then_ok(catalog: Catalog) -> None:
    ext, comp = make(["не json", GOOD])
    res = await ext.extract("x", catalog)
    assert len(res.mentions) == 1 and len(comp.calls) == 2
    # повтор — с другим seed, иначе «зациклившаяся» модель ответит то же самое
    assert comp.calls[0]["seed"] != comp.calls[1]["seed"]


async def test_gives_up_after_one_retry(catalog: Catalog) -> None:
    ext, comp = make([APITimeoutError(REQ)] * 2 + [GOOD])
    with pytest.raises(LlmError, match="не отвечает"):
        await ext.extract("x", catalog)
    assert len(comp.calls) == 2


async def test_invalid_json_exhausted(catalog: Catalog) -> None:
    ext, comp = make(["", '{"mentions": [{"phrase": 1}]}', GOOD])
    with pytest.raises(LlmError, match="разобрать"):
        await ext.extract("x", catalog)
    assert len(comp.calls) == 2  # третьей попытки нет


async def test_auth_error_not_retried(catalog: Catalog) -> None:
    err = AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body=None)
    ext, comp = make([err, GOOD])
    with pytest.raises(LlmError, match="недоступен"):
        await ext.extract("x", catalog)
    assert len(comp.calls) == 1


def test_strict_schema() -> None:
    mention = strict_json_schema(LlmResponse)["$defs"]["LlmMention"]
    assert set(mention["required"]) == set(mention["properties"])  # note тоже обязателен
    assert mention["additionalProperties"] is False
    assert "default" not in mention["properties"]["note"]
    note_string = next(b for b in mention["properties"]["note"]["anyOf"] if b["type"] == "string")
    assert note_string["maxLength"] == 150  # защита от «зацикливания» в пояснении


def test_parse_response_strips_code_fence() -> None:
    assert parse_response(f"```json\n{GOOD}\n```").mentions[0].qty == 15


# ─────────────── Ollama: отключение рассуждений, токены ───────────────


async def test_reasoning_effort_only_when_thinking_disabled(catalog: Catalog) -> None:
    ext, comp = make([GOOD], disable_thinking=True)
    await ext.extract("x", catalog)
    assert comp.calls[0]["reasoning_effort"] == "none"
    ext, comp = make([GOOD])
    await ext.extract("x", catalog)
    assert "reasoning_effort" not in comp.calls[0]


async def test_warns_on_long_prompt(catalog: Catalog) -> None:
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        ext, comp = make([GOOD, GOOD])
        comp.prompt_tokens = 7500
        await ext.extract("x", catalog)
        comp.prompt_tokens = 3000
        await ext.extract("x", catalog)
    finally:
        logger.remove(sink)
    assert len([m for m in messages if "7500" in m]) == 1
    assert not [m for m in messages if "3000" in m]


# ─────────────── Резервный провайдер ───────────────


async def test_fallback_on_connection_error(catalog: Catalog) -> None:
    ext, p, f = make_with_fallback([APIConnectionError(request=REQ)], [GOOD])
    res = await ext.extract("x", catalog)
    assert res.mentions[0].candidates == [17]
    assert len(p.calls) == 1 and len(f.calls) == 1
    assert "reasoning_effort" not in f.calls[0]  # параметр Ollama в облако не шлём
    assert f.calls[0]["model"] == "cloud"


async def test_fallback_after_primary_timeouts(catalog: Catalog) -> None:
    ext, p, f = make_with_fallback([APITimeoutError(REQ)] * 2, [GOOD])
    await ext.extract("x", catalog)
    assert len(p.calls) == 2 and len(f.calls) == 1


async def test_fallback_on_5xx(catalog: Catalog) -> None:
    err = InternalServerError("boom", response=httpx.Response(500, request=REQ), body=None)
    ext, p, f = make_with_fallback([err], [GOOD])
    await ext.extract("x", catalog)
    assert len(f.calls) == 1


async def test_no_fallback_on_bad_json(catalog: Catalog) -> None:
    """Невалидный JSON — не «недоступность»: резерв не трогаем."""
    ext, p, f = make_with_fallback(["", ""], [GOOD])
    with pytest.raises(LlmError, match="разобрать"):
        await ext.extract("x", catalog)
    assert f.calls == []


async def test_both_unavailable(catalog: Catalog) -> None:
    conn = APIConnectionError(request=REQ)
    ext, _, f = make_with_fallback([conn], [APIConnectionError(request=REQ)])
    with pytest.raises(LlmError, match="недоступен"):
        await ext.extract("x", catalog)
    assert len(f.calls) == 1


def test_from_settings_fallback_only_when_configured() -> None:
    base = {"llm_base_url": "http://localhost:11434/v1", "llm_model": "qwen3:8b"}
    s = LlmSettings(_env_file=None, **base)  # type: ignore[call-arg]
    assert len(LlmExtractor.from_settings(s)._providers) == 1
    s = LlmSettings(  # type: ignore[call-arg]
        _env_file=None,
        llm_fallback_base_url="https://cloud.test/v1",
        llm_fallback_model="cloud",
        **base,
    )
    providers = [p for p, _ in LlmExtractor.from_settings(s)._providers]
    assert [p.model for p in providers] == ["qwen3:8b", "cloud"]
    assert [p.disable_thinking for p in providers] == [True, False]


# ─────────────── Компактный справочник ───────────────


def test_compact_names_keep_features(catalog: Catalog) -> None:
    """Сокращение не должно терять различающие признаки ни у одной позиции."""
    for p in catalog.positions:
        short = compact_name(p.name)
        assert extract_features(short) == extract_features(p.name), (p.row, short)
        assert len(short) <= len(p.name)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (17, "Фреонопровод медный, Д6,35 TEC TUBE cips"),
        (61, "Автоматический выключатель РН47, Iр=16А"),
        (55, "Кабель силовой в ПВХ изоляции и оболочке, негорючий, 3х2,5"),
        (23, "Дренажный насос, Aspen"),
        (27, "Элемент нагревательный (3м/120Вт/230В) CSC-3.0"),
    ],
)
def test_compact_name_examples(catalog: Catalog, row: int, expected: str) -> None:
    assert compact_name(catalog.by_row(row).name) == expected


def test_compact_name_fixes_ocr(catalog: Catalog) -> None:
    assert compact_name(catalog.by_row(33).name).startswith("трубопровод дренажн")
    assert compact_name(catalog.by_row(48).name).lower().startswith("сверление отверстий")


def test_format_catalog_is_shorter(catalog: Catalog) -> None:
    full = "\n".join(f"{p.row} | {p.name} | {p.unit}" for p in catalog.positions)
    assert len(format_catalog(catalog)) < 0.8 * len(full)
