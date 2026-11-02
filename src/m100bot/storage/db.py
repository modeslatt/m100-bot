"""SQLite: черновики отчётов, журнал записей, outbox для Битрикса.

Одно соединение aiosqlite; все операции идут под одной блокировкой, потому что
на одном соединении читатель увидел бы незавершённую транзакцию соседа.
Составные изменения (журнал + outbox + черновик) делаются одной транзакцией:
если процесс упадёт посреди, не будет ни «записано, но Битрикс не узнает»,
ни «в журнале есть, а черновик остался».
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from loguru import logger
from pydantic import TypeAdapter, ValidationError

from m100bot.matching.models import MatchedItem

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

_ITEMS = TypeAdapter(list[MatchedItem])
WriteMode = Literal["add", "set"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Draft:
    user_id: int
    source_text: str
    items: list[MatchedItem]
    updated_at: str


@dataclass(frozen=True)
class NewWrite:
    """Что записали в ячейку; prev_value — значение до записи (для отката)."""

    row: int
    qty: float
    mode: WriteMode
    prev_value: float | None


@dataclass(frozen=True)
class WriteRecord:
    id: int
    user_id: int
    work_date: date
    row: int
    qty: float
    mode: WriteMode
    prev_value: float | None
    created_at: str


@dataclass(frozen=True)
class OutboxItem:
    id: int
    work_date: date
    payload: dict[str, Any]
    attempts: int
    last_error: str | None


def fold_writes(writes: Iterable[NewWrite | WriteRecord]) -> dict[int, float]:
    """Сколько мастер сообщил за день по каждой строке, по журналу в порядке записи.

    add прибавляет, set заменяет; строки с нулём не возвращаются. Это снимок «что мастер
    отчитал за день»: именно он уходит в Битрикс — заново целиком, а не дельтой, потому
    что элемент за дату обновляется, а не дополняется.
    """
    totals: dict[int, float] = {}
    for w in writes:
        if w.mode == "add":
            totals[w.row] = round(totals.get(w.row, 0.0) + w.qty, 6)
        else:
            totals[w.row] = round(w.qty, 6)
    return {row: qty for row, qty in totals.items() if qty > 0}


class Storage:
    def __init__(self, path: Path, *, now: Callable[[], datetime] = _utc_now) -> None:
        self.path = path
        self._now = now
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ─────────────── Жизненный цикл ───────────────

    async def init(self) -> None:
        """Открыть базу (создав папку и таблицы). Повторный вызов ничего не делает."""
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: транзакциями управляем сами (BEGIN/COMMIT в _tx)
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        logger.info("Хранилище готово: {}", self.path.name)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Storage":
        await self.init()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage не открыт: вызовите init()")
        return self._db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Блокировка + транзакция: всё или ничего."""
        async with self._lock:
            db = self.db
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.execute("ROLLBACK")
                raise
            else:
                await db.execute("COMMIT")

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ─────────────── Черновики ───────────────

    async def save_draft(self, user_id: int, source_text: str, items: list[MatchedItem]) -> None:
        """Один черновик на мастера: новый заменяет прежний."""
        async with self._tx() as db:
            await self._put_draft(db, user_id, source_text, items)

    async def _put_draft(
        self, db: aiosqlite.Connection, user_id: int, source_text: str, items: list[MatchedItem]
    ) -> None:
        await db.execute(
            "INSERT INTO drafts(user_id, source_text, items_json, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET source_text=excluded.source_text, "
            "items_json=excluded.items_json, updated_at=excluded.updated_at",
            (user_id, source_text, _ITEMS.dump_json(items).decode(), self._stamp()),
        )

    async def get_draft(self, user_id: int) -> Draft | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM drafts WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            items = _ITEMS.validate_json(row["items_json"])
        except ValidationError:
            # формат MatchedItem мог измениться после обновления бота — не падаем, не стираем
            logger.error("Черновик пользователя {} не читается (устарел формат?)", user_id)
            return None
        return Draft(user_id, row["source_text"], items, row["updated_at"])

    async def delete_draft(self, user_id: int) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))

    # ─────────────── Журнал и outbox ───────────────

    async def record_confirmation(
        self,
        user_id: int,
        work_date: date,
        writes: list[NewWrite],
        *,
        outbox_payload: dict[str, Any] | None,
        leftover: list[MatchedItem],
    ) -> list[int]:
        """Подтверждённый отчёт, уже записанный в таблицу: одна транзакция.

        Журнал `writes` + задание в outbox Битрикса (если задано) + черновик: остаток
        неразобранных позиций сохраняется, иначе черновик удаляется.
        Возвращает id строк журнала.
        """
        async with self._tx() as db:
            ids = await self._insert_writes(db, user_id, work_date, writes)
            await self._insert_outbox(db, work_date, outbox_payload)
            if leftover:
                cur = await db.execute("SELECT source_text FROM drafts WHERE user_id=?", (user_id,))
                row = await cur.fetchone()
                await self._put_draft(db, user_id, row["source_text"] if row else "", leftover)
            else:
                await db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
        return ids

    async def record_correction(
        self,
        user_id: int,
        work_date: date,
        write: NewWrite,
        *,
        outbox_payload: dict[str, Any] | None,
    ) -> int:
        """Исправление уже записанного за день: журнал + outbox, черновик не трогаем."""
        async with self._tx() as db:
            [write_id] = await self._insert_writes(db, user_id, work_date, [write])
            await self._insert_outbox(db, work_date, outbox_payload)
        return write_id

    async def _insert_writes(
        self, db: aiosqlite.Connection, user_id: int, work_date: date, writes: list[NewWrite]
    ) -> list[int]:
        ids = []
        for w in writes:
            cur = await db.execute(
                'INSERT INTO writes(user_id, work_date, "row", qty, mode, prev_value, created_at) '
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, work_date.isoformat(), w.row, w.qty, w.mode, w.prev_value, self._stamp()),
            )
            ids.append(cur.lastrowid)
        return ids

    async def _insert_outbox(
        self, db: aiosqlite.Connection, work_date: date, payload: dict[str, Any] | None
    ) -> None:
        if payload is not None:
            await db.execute(
                "INSERT INTO bitrix_outbox(work_date, payload) VALUES(?,?)",
                (work_date.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )

    async def get_writes(
        self, user_id: int | None = None, work_date: date | None = None
    ) -> list[WriteRecord]:
        """Журнал записей в порядке записи, с фильтром по мастеру и дате."""
        where, args = [], []
        if user_id is not None:
            where.append("user_id=?")
            args.append(user_id)
        if work_date is not None:
            where.append("work_date=?")
            args.append(work_date.isoformat())
        sql = "SELECT * FROM writes" + (" WHERE " + " AND ".join(where) if where else "")
        async with self._lock:
            cur = await self.db.execute(sql + " ORDER BY id", args)
            rows = await cur.fetchall()
        return [
            WriteRecord(
                r["id"],
                r["user_id"],
                date.fromisoformat(r["work_date"]),
                r["row"],
                r["qty"],
                r["mode"],
                r["prev_value"],
                r["created_at"],
            )
            for r in rows
        ]

    async def pending_outbox(self) -> list[OutboxItem]:
        """Неотправленные задания Битрикса, по порядку создания."""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, work_date, payload, attempts, last_error FROM bitrix_outbox "
                "WHERE done=0 ORDER BY id"
            )
            rows = await cur.fetchall()
        return [
            OutboxItem(
                r["id"],
                date.fromisoformat(r["work_date"]),
                json.loads(r["payload"]),
                r["attempts"],
                r["last_error"],
            )
            for r in rows
        ]

    async def mark_outbox_done(self, ids: Iterable[int], note: str | None = None) -> None:
        """Отправлено (или заменено более новым снимком — тогда note объясняет почему)."""
        async with self._tx() as db:
            for i in ids:
                await db.execute(
                    "UPDATE bitrix_outbox SET done=1, last_error=? WHERE id=?", (note, i)
                )

    async def mark_outbox_failed(self, item_id: int, error: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE bitrix_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                (error, item_id),
            )
