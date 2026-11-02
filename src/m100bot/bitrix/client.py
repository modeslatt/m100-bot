"""Битрикс24 через входящий вебхук.

План: смарт-процесс «Дневной отчёт», один элемент на отчёт (дата, мастер, текст сводки,
ссылка на таблицу). Метод crm.item.add / crm.item.update.
"""

from datetime import date

import httpx


class BitrixError(RuntimeError):
    pass


class BitrixClient:
    def __init__(self, webhook_url: str, entity_type_id: int | None) -> None:
        self._base = webhook_url.rstrip("/") + "/"
        self._entity_type_id = entity_type_id
        self._http = httpx.AsyncClient(timeout=15)

    async def call(self, method: str, params: dict) -> dict:
        """TODO: POST {base}{method}.json, разбор 'error' в ответе, tenacity-повторы
        на 5xx/таймаут/QUERY_LIMIT_EXCEEDED. Не логировать webhook_url."""
        raise NotImplementedError

    async def upsert_daily_report(
        self, d: date, master: str, lines: list[str], sheet_url: str
    ) -> int:
        """TODO: найти элемент за дату (crm.item.list с фильтром) → update, иначе add.
        Вернуть id элемента."""
        raise NotImplementedError
