"""SQLite: черновики отчётов, журнал записей, outbox для Битрикса."""

from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    user_id     INTEGER PRIMARY KEY,
    source_text TEXT NOT NULL,
    items_json  TEXT NOT NULL,          -- list[MatchedItem]
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS writes (       -- что и когда записали в таблицу
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    work_date   TEXT NOT NULL,
    row         INTEGER NOT NULL,
    qty         REAL NOT NULL,
    mode        TEXT NOT NULL,            -- add | set
    prev_value  REAL,                     -- значение ячейки до записи (для отката)
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bitrix_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    done        INTEGER NOT NULL DEFAULT 0
);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        """TODO: aiosqlite.connect, executescript(SCHEMA)."""
        raise NotImplementedError
