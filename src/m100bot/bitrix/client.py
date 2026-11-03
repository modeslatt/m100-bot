"""Битрикс24 через входящий вебхук: элемент смарт-процесса «Дневной отчёт».

Один элемент на каждый подтверждённый отчёт («Отчет от 03.11.2026 14:35»). Бот хранит id
созданного элемента у себя (SQLite) и при исправлении обновляет именно его — поиск по
полям не нужен.

Поля, созданные в интерфейсе, получают автоматические коды (`ufCrm5_1730612345`), поэтому
клиент находит их по названию через `crm.item.fields` (один раз, потом кэш).

`crm.item.add` не идемпотентен: если запрос дошёл, а ответ потерялся, повтор создаст второй
элемент. Поэтому создание повторяем только когда запрос точно не обработан (лимит запросов,
нет соединения); остальное — уже очередь outbox, позже.

В URL вебхука лежит секрет: ни URL, ни исключения httpx (они его содержат) наружу не отдаём.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from m100bot.config import Settings

# Названия пользовательских полей смарт-процесса (создаются вручную, см. README).
FIELD_TITLES: dict[str, str] = {
    "date": "Дата отчёта",
    "master": "Мастер",
    "master_id": "Telegram ID мастера",
    "lines": "Объёмы",
    "sheet_url": "Ссылка на таблицу",
}
# Где Битрикс хранит подпись поля: у системных — title, у пользовательских — *Label.
_LABEL_KEYS = ("title", "listLabel", "formLabel", "filterLabel", "editFormLabel")
# Запрос точно не выполнен — повтор безопасен даже для crm.item.add.
_NOT_PROCESSED_CODES = {"QUERY_LIMIT_EXCEEDED"}
_TRANSIENT_CODES = _NOT_PROCESSED_CODES | {"INTERNAL_SERVER_ERROR"}
_NOT_FOUND_CODES = {"NOT_FOUND"}


class BitrixError(RuntimeError):
    """Ошибка Битрикса; текст без URL вебхука. Повтор не поможет — нужна правка настроек."""

    def __init__(self, text: str, code: str = "") -> None:
        super().__init__(text)
        self.code = code


class BitrixUnavailable(BitrixError):
    """Временная недоступность (5xx, таймаут, лимит запросов) — имеет смысл повторить.

    maybe_done: запрос мог успеть выполниться (таймаут ответа, 5xx) — повторять можно
    только идемпотентные методы.
    """

    def __init__(self, text: str, code: str = "", *, maybe_done: bool = True) -> None:
        super().__init__(text, code)
        self.maybe_done = maybe_done


@dataclass(frozen=True)
class FieldMap:
    """Коды полей в формате crm.item (camelCase), найденные по названиям."""

    date: str
    master: str
    master_id: str
    lines: str
    sheet_url: str


def _norm(label: str) -> str:
    return " ".join(label.lower().replace("ё", "е").split())


def _hide_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "<url>", text)


def find_fields(fields: dict[str, dict[str, Any]]) -> tuple[FieldMap | None, list[str]]:
    """Сопоставить ответ crm.item.fields с FIELD_TITLES. Возвращает (карта, ненайденные)."""
    by_label: dict[str, str] = {}
    for code, meta in fields.items():
        for key in _LABEL_KEYS:
            label = meta.get(key)
            if isinstance(label, str) and label.strip():
                by_label.setdefault(_norm(label), code)
            elif isinstance(label, dict):  # подписи по языкам: {"ru": "...", "en": "..."}
                for text in label.values():
                    if isinstance(text, str) and text.strip():
                        by_label.setdefault(_norm(text), code)
    found = {k: by_label.get(_norm(t)) for k, t in FIELD_TITLES.items()}
    missing = [FIELD_TITLES[k] for k, code in found.items() if code is None]
    if missing:
        return None, missing
    return FieldMap(**found), []  # type: ignore[arg-type]


class BitrixClient:
    def __init__(
        self,
        webhook_url: str,
        entity_type_id: int,
        *,
        timeout: float = 15.0,
        attempts: int = 3,
        retry_wait: float = 1.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = webhook_url.rstrip("/") + "/"
        self.entity_type_id = entity_type_id
        self._attempts = attempts
        self._retry_wait = retry_wait
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._fields: FieldMap | None = None

    @classmethod
    def from_settings(cls, s: Settings) -> BitrixClient | None:
        """None — Битрикс не настроен (нет вебхука или ID смарт-процесса)."""
        if not s.bitrix_webhook_url:
            return None
        if s.bitrix_entity_type_id is None:
            logger.error(
                "BITRIX_WEBHOOK_URL задан, а BITRIX_ENTITY_TYPE_ID пуст — Битрикс выключен"
            )
            return None
        return cls(s.bitrix_webhook_url, s.bitrix_entity_type_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ─────────────── Низкий уровень ───────────────

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, idempotent: bool = True
    ) -> Any:
        """Вызвать метод REST и вернуть `result`.

        Временные сбои повторяются; для неидемпотентного метода — только те, где запрос
        точно не выполнен.
        """

        def retryable(e: BaseException) -> bool:
            return isinstance(e, BitrixUnavailable) and (idempotent or not e.maybe_done)

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential(multiplier=self._retry_wait, max=8),
            retry=retry_if_exception(retryable),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "Битрикс {}: попытка {} не удалась ({}), повторяю",
                method,
                rs.attempt_number,
                rs.outcome.exception() if rs.outcome else "?",
            ),
        )
        async for attempt in retrying:
            with attempt:
                return await self._call_once(method, params or {})
        raise AssertionError("unreachable")  # pragma: no cover

    async def _call_once(self, method: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._http.post(f"{self._base}{method}.json", json=params)
        except httpx.ConnectTimeout:
            raise BitrixUnavailable(f"{method}: таймаут соединения", maybe_done=False) from None
        except httpx.TimeoutException:
            raise BitrixUnavailable(f"{method}: таймаут") from None
        except httpx.ConnectError as e:  # сообщение httpx может содержать URL — не берём его
            raise BitrixUnavailable(
                f"{method}: нет соединения ({type(e).__name__})", maybe_done=False
            ) from None
        except httpx.TransportError as e:
            raise BitrixUnavailable(f"{method}: сбой связи ({type(e).__name__})") from None

        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            code = str(body["error"])
            desc = _hide_urls(str(body.get("error_description") or ""))
            text = f"{method}: {code}" + (f" — {desc}" if desc else "")
            if code in _TRANSIENT_CODES or resp.status_code >= 500 or resp.status_code == 429:
                raise BitrixUnavailable(
                    text,
                    code,
                    maybe_done=code not in _NOT_PROCESSED_CODES and resp.status_code != 429,
                )
            raise BitrixError(text, code)
        if resp.status_code == 429:
            raise BitrixUnavailable(f"{method}: HTTP 429", maybe_done=False)
        if resp.status_code >= 500:
            raise BitrixUnavailable(f"{method}: HTTP {resp.status_code}")
        if resp.status_code >= 400 or not isinstance(body, dict) or "result" not in body:
            raise BitrixError(f"{method}: неожиданный ответ, HTTP {resp.status_code}")
        return body["result"]

    # ─────────────── Смарт-процесс ───────────────

    async def fields(self) -> FieldMap:
        """Коды нужных полей; при первом вызове — запрос crm.item.fields."""
        if self._fields is None:
            result = await self.call("crm.item.fields", {"entityTypeId": self.entity_type_id})
            fields, missing = find_fields(result.get("fields", {}))
            if fields is None:
                raise BitrixError(
                    "в смарт-процессе нет полей: " + ", ".join(f"«{m}»" for m in missing)
                )
            self._fields = fields
        return self._fields

    async def save_report(
        self,
        item_id: int | None,
        *,
        title: str,
        work_date: date,
        master_id: int,
        master: str,
        lines: list[str],
        sheet_url: str,
    ) -> int:
        """Создать элемент отчёта (item_id=None) или обновить его целиком. Возвращает id.

        Если обновляемый элемент удалили в CRM — создаётся заново.
        """
        f = await self.fields()
        values = {
            "title": title,
            f.date: work_date.isoformat(),
            f.master: master,
            f.master_id: str(master_id),
            f.lines: "\n".join(lines),
            f.sheet_url: sheet_url,
        }
        if item_id is not None:
            try:
                await self.call(
                    "crm.item.update",
                    {"entityTypeId": self.entity_type_id, "id": item_id, "fields": values},
                )
                return item_id
            except BitrixError as e:
                if e.code not in _NOT_FOUND_CODES:
                    raise
                logger.warning("Битрикс: элемент {} удалён в CRM, создаю заново", item_id)
        result = await self.call(
            "crm.item.add",
            {"entityTypeId": self.entity_type_id, "fields": values},
            idempotent=False,
        )
        return int(result["item"]["id"])
