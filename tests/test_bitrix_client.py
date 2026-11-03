"""BitrixClient без сети: поддельный HTTP (httpx.MockTransport) вместо портала."""

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from m100bot.bitrix.client import (
    BitrixClient,
    BitrixError,
    BitrixUnavailable,
    find_fields,
)
from m100bot.config import Settings

WEBHOOK = "https://portal.bitrix24.by/rest/1/SECRETTOKEN/"
ETID = 1038
D = date(2026, 11, 3)

FIELDS = {
    "id": {"type": "integer", "title": "ID"},
    "title": {"type": "string", "title": "Название"},
    "ufCrm5_1": {"type": "date", "title": "UF_CRM_5_1", "listLabel": "Дата отчёта"},
    "ufCrm5_2": {"type": "string", "title": "UF_CRM_5_2", "formLabel": "Мастер"},
    "ufCrm5_3": {"type": "string", "title": "UF_CRM_5_3", "listLabel": "Telegram ID мастера"},
    "ufCrm5_4": {"type": "string", "title": "UF_CRM_5_4", "listLabel": "объёмы"},
    "ufCrm5_5": {"type": "url", "title": "UF_CRM_5_5", "listLabel": {"ru": "Ссылка на таблицу"}},
}

Handler = Callable[[str, dict[str, Any]], httpx.Response]


class Portal:
    """Записывает вызовы (метод, параметры) и отвечает через handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(WEBHOOK)
        method = str(request.url)[len(WEBHOOK) :].removesuffix(".json")
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        return self.handler(method, params)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "time": {}})


def make(handler: Handler, attempts: int = 3) -> tuple[BitrixClient, Portal]:
    portal = Portal(handler)
    http = httpx.AsyncClient(transport=httpx.MockTransport(portal))
    client = BitrixClient(WEBHOOK, ETID, attempts=attempts, retry_wait=0, http=http)
    return client, portal


def crm(*, update_error: httpx.Response | None = None) -> Handler:
    """Смарт-процесс с полями FIELDS: add отдаёт id 77, update — успех или update_error."""

    def handler(method: str, params: dict[str, Any]) -> httpx.Response:
        if method == "crm.item.fields":
            return ok({"fields": FIELDS})
        if method == "crm.item.add":
            return ok({"item": {"id": 77}})
        if method == "crm.item.update":
            return update_error or ok({"item": {"id": params["id"]}})
        raise AssertionError(method)

    return handler


REPORT: dict[str, Any] = {
    "title": "Отчет от 03.11.2026 14:35",
    "work_date": D,
    "master_id": 555,
    "master": "Иван",
    "lines": ["Кабель 3х2,5 — 56 м", "Хомут — 12 шт"],
    "sheet_url": "https://sheet.test/#gid=0",
}


# ─────────────── call: ответ и ошибки ───────────────


async def test_call_returns_result_and_posts_json() -> None:
    client, portal = make(lambda m, p: ok({"answer": 42}))
    assert await client.call("profile", {"a": 1}) == {"answer": 42}
    assert portal.calls == [("profile", {"a": 1})]


async def test_error_in_body_raises_without_retry() -> None:
    client, portal = make(
        lambda m, p: httpx.Response(
            401, json={"error": "INVALID_CREDENTIALS", "error_description": "Invalid request"}
        )
    )
    with pytest.raises(BitrixError, match="INVALID_CREDENTIALS") as e:
        await client.call("crm.item.list")
    assert not isinstance(e.value, BitrixUnavailable)
    assert len(portal.calls) == 1  # неверный вебхук повтором не исправить


async def test_error_with_http_200_is_still_an_error() -> None:
    client, _ = make(lambda m, p: httpx.Response(200, json={"error": "X"}))
    with pytest.raises(BitrixError, match="X"):
        await client.call("crm.item.add")


@pytest.mark.parametrize(
    "transient",
    [
        httpx.Response(
            503, json={"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many"}
        ),
        httpx.Response(502, text="<html>Bad gateway</html>"),
        httpx.Response(500, json={"error": "INTERNAL_SERVER_ERROR"}),
    ],
)
async def test_transient_errors_are_retried(transient: httpx.Response) -> None:
    answers = [transient, ok("done")]
    client, portal = make(lambda m, p: answers.pop(0))
    assert await client.call("crm.item.list") == "done"
    assert len(portal.calls) == 2


async def test_query_limit_with_http_200_is_retried() -> None:
    answers = [httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"}), ok(1)]
    client, portal = make(lambda m, p: answers.pop(0))
    assert await client.call("crm.item.list") == 1
    assert len(portal.calls) == 2


async def test_gives_up_after_attempts() -> None:
    client, portal = make(lambda m, p: httpx.Response(503, text=""), attempts=3)
    with pytest.raises(BitrixUnavailable, match="HTTP 503"):
        await client.call("crm.item.list")
    assert len(portal.calls) == 3


async def test_network_error_is_retried_and_hides_url() -> None:
    def handler(m: str, p: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {WEBHOOK}{m}.json")

    client, portal = make(handler, attempts=2)
    with pytest.raises(BitrixUnavailable) as e:
        await client.call("crm.item.list")
    assert len(portal.calls) == 2
    assert "SECRETTOKEN" not in str(e.value) and "ConnectError" in str(e.value)


async def test_timeout_is_retried() -> None:
    answers: list[Any] = [httpx.ReadTimeout("slow"), ok(1)]

    def handler(m: str, p: dict[str, Any]) -> httpx.Response:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    client, _ = make(handler)
    assert await client.call("x") == 1


async def test_error_description_hides_urls() -> None:
    client, _ = make(
        lambda m, p: httpx.Response(
            400, json={"error": "ERROR", "error_description": f"see {WEBHOOK}x"}
        )
    )
    with pytest.raises(BitrixError) as e:
        await client.call("x")
    assert "SECRETTOKEN" not in str(e.value)


async def test_unexpected_body_is_an_error() -> None:
    client, _ = make(lambda m, p: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(BitrixError, match="неожиданный ответ"):
        await client.call("x")


# ─────────────── Поля и запись ───────────────


def test_find_fields_by_labels() -> None:
    fields, missing = find_fields(FIELDS)
    assert missing == []
    assert fields is not None
    assert (fields.date, fields.master, fields.master_id, fields.lines, fields.sheet_url) == (
        "ufCrm5_1",
        "ufCrm5_2",
        "ufCrm5_3",
        "ufCrm5_4",  # «объёмы» == «Объёмы»: регистр и ё не важны
        "ufCrm5_5",
    )


def test_find_fields_reports_missing() -> None:
    partial = {k: v for k, v in FIELDS.items() if k != "ufCrm5_3"}
    fields, missing = find_fields(partial)
    assert fields is None and missing == ["Telegram ID мастера"]


async def test_save_report_creates_item() -> None:
    client, portal = make(crm())
    assert await client.save_report(None, **REPORT) == 77
    assert portal.methods() == ["crm.item.fields", "crm.item.add"]
    _, add = portal.calls[1]
    assert add["entityTypeId"] == ETID
    assert add["fields"] == {
        "title": "Отчет от 03.11.2026 14:35",
        "ufCrm5_1": "2026-11-03",
        "ufCrm5_2": "Иван",
        "ufCrm5_3": "555",
        "ufCrm5_4": "Кабель 3х2,5 — 56 м\nХомут — 12 шт",
        "ufCrm5_5": "https://sheet.test/#gid=0",
    }


async def test_save_report_updates_known_item_and_caches_fields() -> None:
    client, portal = make(crm())
    assert await client.save_report(12, **REPORT) == 12
    assert await client.save_report(12, **{**REPORT, "lines": ["a"]}) == 12
    assert portal.methods() == ["crm.item.fields", "crm.item.update", "crm.item.update"]
    _, upd = portal.calls[-1]
    assert upd["id"] == 12 and upd["fields"]["ufCrm5_4"] == "a"


async def test_deleted_item_is_created_again() -> None:
    gone = httpx.Response(400, json={"error": "NOT_FOUND", "error_description": "Not found"})
    client, portal = make(crm(update_error=gone))
    assert await client.save_report(12, **REPORT) == 77
    assert portal.methods() == ["crm.item.fields", "crm.item.update", "crm.item.add"]


async def test_other_update_error_is_raised() -> None:
    denied = httpx.Response(403, json={"error": "ACCESS_DENIED"})
    client, portal = make(crm(update_error=denied))
    with pytest.raises(BitrixError, match="ACCESS_DENIED"):
        await client.save_report(12, **REPORT)
    assert "crm.item.add" not in portal.methods()


async def test_add_is_not_retried_when_it_may_have_succeeded() -> None:
    """Таймаут ответа или 5xx: элемент мог создаться — повтор дал бы дубликат."""
    for failure in (httpx.ReadTimeout("slow"), httpx.Response(502, text="")):

        def handler(m: str, p: dict[str, Any], failure: Any = failure) -> httpx.Response:
            if m == "crm.item.fields":
                return ok({"fields": FIELDS})
            if isinstance(failure, Exception):
                raise failure
            return failure

        client, portal = make(handler)
        with pytest.raises(BitrixUnavailable):
            await client.save_report(None, **REPORT)
        assert portal.methods().count("crm.item.add") == 1


async def test_add_is_retried_when_surely_not_processed() -> None:
    """Лимит запросов или нет соединения — запрос не выполнен, повтор безопасен."""
    answers: list[Any] = [
        httpx.Response(503, json={"error": "QUERY_LIMIT_EXCEEDED"}),
        httpx.ConnectError("refused"),
        ok({"item": {"id": 5}}),
    ]

    def handler(m: str, p: dict[str, Any]) -> httpx.Response:
        if m == "crm.item.fields":
            return ok({"fields": FIELDS})
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    client, portal = make(handler)
    assert await client.save_report(None, **REPORT) == 5
    assert portal.methods().count("crm.item.add") == 3


async def test_missing_fields_named_in_error() -> None:
    def handler(m: str, p: dict[str, Any]) -> httpx.Response:
        return ok({"fields": {"title": {"title": "Название"}}})

    client, portal = make(handler)
    with pytest.raises(BitrixError, match="«Дата отчёта».*«Ссылка на таблицу»"):
        await client.save_report(None, **REPORT)
    assert portal.methods() == ["crm.item.fields"]  # ничего не записано


# ─────────────── Настройки ───────────────


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, bot_token="t", spreadsheet_id="s", **kw)  # type: ignore[arg-type]


def test_from_settings() -> None:
    assert BitrixClient.from_settings(_settings()) is None
    assert BitrixClient.from_settings(_settings(bitrix_webhook_url=WEBHOOK)) is None  # нет ID
    client = BitrixClient.from_settings(
        _settings(bitrix_webhook_url=WEBHOOK, bitrix_entity_type_id=ETID)
    )
    assert client is not None and client.entity_type_id == ETID
