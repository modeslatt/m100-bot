"""Google Sheets через gspread (сервисный аккаунт).

gspread синхронный — вызовы оборачиваем в asyncio.to_thread, чтобы не блокировать бота.
"""

from datetime import date
from pathlib import Path

from m100bot.sheets.catalog import Catalog


class SheetsClient:
    def __init__(self, credentials_file: Path, spreadsheet_id: str, worksheet: str) -> None:
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._worksheet = worksheet

    async def load_catalog(self) -> Catalog:
        """TODO: get_values(value_render_option='UNFORMATTED_VALUE') → parse_catalog().
        Кэшировать на N минут, сбрасывать после записи."""
        raise NotImplementedError

    async def load_synonyms(self) -> dict[str, int]:
        """TODO: лист «Синонимы»: A — фраза мастера, B — строка позиции. Создать лист, если нет."""
        raise NotImplementedError

    async def ensure_date_column(self, d: date) -> int:
        """TODO: вернуть колонку даты; если нет — вставить после последней даты,
        записать дату в строку 3 и «объем» в строку 4. Проверить, что колонка ≤ CZ
        (иначе формула SUM(J:CZ) её не увидит — расширить формулы в I)."""
        raise NotImplementedError

    async def write_volumes(self, d: date, volumes: dict[int, float], *, mode: str = "add") -> None:
        """TODO: mode='add' — прибавить к текущему значению ячейки, 'set' — заменить.
        Одна batch_update на весь отчёт."""
        raise NotImplementedError

    async def add_synonym(self, phrase: str, row: int) -> None:
        raise NotImplementedError

    def row_link(self, row: int) -> str:
        """TODO: ссылка вида .../edit#gid=<gid>&range=B<row>."""
        raise NotImplementedError
