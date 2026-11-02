"""Сценарий отчёта: распознать → черновик → подтвердить → записать.

Хэндлеры бота тонкие и вызывают только этот сервис.

Правила, на которых всё держится:
- в таблицу пишем только из `confirm` / `correct_today`, то есть после явного «Записать»;
- сбой Sheets не стирает черновик — мастер получает понятный текст и жмёт кнопку ещё раз;
- Битрикс24 напрямую не трогаем: задание кладётся в outbox (в той же транзакции, что
  журнал) и уходит отдельно, `flush_outbox`, с повторами. Недоступный Битрикс не мешает
  мастеру закончить отчёт;
- каждая запись попадает в журнал `writes` с prev_value — значением ячейки до записи;
- кнопки карточки несут отпечаток черновика (`Draft.token`): нажатие на старую карточку
  не должно ничего записать, поэтому правящие методы принимают `token` и сверяют его
  под блокировкой мастера.
"""

import asyncio
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

from m100bot.matching.llm import compact_name
from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.matching.normalize import normalize_unit
from m100bot.matching.phrase import synonym_key
from m100bot.sheets.addressing import CellChange, Mode
from m100bot.sheets.catalog import Catalog, Position
from m100bot.sheets.client import SheetChangedError, SheetsError
from m100bot.storage.db import Draft, NewWrite, OutboxItem, Storage, fold_writes

BACKOFF_BASE_S = 30.0  # пауза перед второй попыткой отправки в Битрикс
BACKOFF_MAX_S = 900.0  # дальше — раз в 15 минут, пока не получится

NO_DRAFT = "Нет отчёта для записи. Пришлите сообщение с работами."
STALE = "Эта карточка устарела. Пришлите отчёт заново."


class ReportError(RuntimeError):
    """Отчёт не записан. Текст можно показать мастеру; черновик (если был) остаётся."""


class StaleCardError(ReportError):
    """Нажата кнопка со старой карточки: черновик с тех пор изменился."""


class SheetsLike(Protocol):
    async def load_catalog(self) -> Catalog: ...
    async def load_synonyms(self) -> dict[str, int]: ...
    async def add_synonym(self, phrase: str, row: int) -> None: ...
    async def read_done(self, rows: Iterable[int]) -> dict[int, float]: ...
    async def write_volumes(
        self, d: date, volumes: dict[int, float], *, mode: Mode = "add"
    ) -> list[CellChange]: ...
    def row_link(self, row: int) -> str: ...


class BitrixLike(Protocol):
    async def upsert_daily_report(
        self, d: date, master: str, lines: list[str], sheet_url: str
    ) -> int: ...


class MatcherLike(Protocol):
    catalog: Catalog

    def refresh(self, catalog: Catalog, synonyms: dict[str, int] | None = None) -> None: ...
    async def match(
        self, text: str, remaining: Mapping[int, float] | None = None
    ) -> list[MatchedItem]: ...


@dataclass(frozen=True)
class FlushResult:
    sent: int = 0  # ушло в Битрикс
    failed: int = 0  # не получилось, повторим позже
    waiting: int = 0  # ещё ждёт паузы перед повтором
    superseded: int = 0  # заменено более новым снимком того же дня


@dataclass(frozen=True)
class TodayLine:
    """Что мастер отчитал сегодня по одной строке (по журналу бота)."""

    row: int
    name: str
    qty: float
    unit: str


def fmt_qty(qty: float) -> str:
    """Число по-русски: 15 → «15», 0.5 → «0,5»."""
    return f"{qty:g}".replace(".", ",")


def backoff_seconds(attempts: int) -> float:
    """Пауза после N-й неудачи: 30 с, 1 мин, 2 мин … не больше 15 мин."""
    return min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_MAX_S)


def _safe_error(exc: BaseException) -> str:
    """Текст ошибки для журнала: без адресов — в URL вебхука Битрикса лежит секрет."""
    text = re.sub(r"https?://\S+", "<url>", str(exc))
    return f"{type(exc).__name__}: {text}"[:300]


def is_ready(item: MatchedItem) -> bool:
    return (
        item.status == MatchStatus.OK
        and item.row is not None
        and item.qty is not None
        and item.qty > 0
    )


def _over(qty: float | None, left: float | None) -> bool:
    return qty is not None and left is not None and qty > left + 1e-9


class ReportService:
    def __init__(
        self,
        sheets: SheetsLike,
        storage: Storage,
        matcher: MatcherLike,
        bitrix: BitrixLike | None = None,
        *,
        timezone: str = "Europe/Minsk",
        today: Callable[[], date] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sheets = sheets
        self.storage = storage
        self.matcher = matcher
        self.bitrix = bitrix  # None — Битрикс не настроен: в outbox ничего не кладём
        self._today = today or (lambda: datetime.now(ZoneInfo(timezone)).date())
        self._clock = clock
        self._user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flush_lock = asyncio.Lock()
        self._next_try: dict[int, float] = {}  # id задания outbox → когда можно повторить
        self._synonyms_seen: dict[str, int] | None = None
        self._background: set[asyncio.Task[None]] = set()

    # ─────────────── Справочные методы для бота ───────────────

    def today(self) -> date:
        return self._today()

    async def catalog(self) -> Catalog:
        """Справочник для показа названий; сбой таблицы → ReportError с текстом для мастера."""
        return await self._catalog()

    def table_link(self, row: int | None = None) -> str:
        """Ссылка на таблицу (на строку позиции, если указана)."""
        return self.sheets.row_link(row or 1)

    async def get_draft(self, user_id: int, *, token: str | None = None) -> Draft | None:
        """Черновик мастера. С token — только если карточка актуальна (иначе StaleCardError,
        а если черновика нет вовсе — ReportError)."""
        if token is not None:
            return await self._load(user_id, token)
        return await self.storage.get_draft(user_id)

    # ─────────────── Распознавание ───────────────

    async def recognize(self, user_id: int, text: str) -> list[MatchedItem]:
        """Текст мастера → позиции на подтверждение; результат сохраняется как черновик.

        Пустой результат черновик не трогает. Ошибки справочника (`SheetsError`) и
        LLM (`LlmError`) пробрасываются: их тексты уже понятны мастеру.
        """
        async with self._user_locks[user_id]:
            items = await self._match(text)
            if items:
                await self.storage.save_draft(user_id, text, items)
            return items

    async def _match(self, text: str) -> list[MatchedItem]:
        catalog = await self.sheets.load_catalog()
        synonyms = await self._synonyms()
        if self.matcher.catalog is not catalog or self._synonyms_seen is not synonyms:
            self.matcher.refresh(catalog, synonyms)  # таблицу правят — подхватываем свежее
            self._synonyms_seen = synonyms
        remaining = await self._remaining_map(catalog)
        return await self.matcher.match(text, remaining=remaining)

    async def _synonyms(self) -> dict[str, int]:
        """Синонимы необязательны: без них бот работает, просто чаще переспрашивает."""
        try:
            return await self.sheets.load_synonyms()
        except SheetsError as e:
            logger.warning("Синонимы не прочитаны, работаю без них: {}", e)
            return self._synonyms_seen or {}

    async def _remaining_map(self, catalog: Catalog) -> dict[int, float] | None:
        """Остаток по смете (план − выполнено) для предупреждения ⚠️; нет данных — без него."""
        try:
            done = await self.sheets.read_done([p.row for p in catalog.positions])
        except SheetsError as e:
            logger.warning("Остатки не прочитаны, предупреждения о превышении не будет: {}", e)
            return None
        return {p.row: p.planned - done.get(p.row, 0.0) for p in catalog.positions}

    async def _remaining_for(self, catalog: Catalog, row: int) -> float | None:
        pos = catalog.by_row(row)
        if pos is None:
            return None
        try:
            done = (await self.sheets.read_done([row])).get(row, 0.0)
        except SheetsError as e:
            logger.warning("Остаток по строке {} не прочитан: {}", row, e)
            return None
        return pos.planned - done

    # ─────────────── Правка черновика ───────────────

    async def _load(self, user_id: int, token: str | None) -> Draft:
        """Черновик мастера; кнопка со старой карточки (токен не совпал) → StaleCardError."""
        draft = await self.storage.get_draft(user_id)
        if draft is None:
            raise ReportError(NO_DRAFT)
        if token is not None and draft.token != token:
            raise StaleCardError(STALE)
        return draft

    @staticmethod
    def _item(draft: Draft, idx: int) -> MatchedItem:
        if not 0 <= idx < len(draft.items):
            raise StaleCardError(STALE)
        return draft.items[idx]

    @staticmethod
    def _resolve(item: MatchedItem, pos: Position, left: float | None) -> MatchedItem:
        """Позиция выбрана: проверяем единицы и объём так же, как это делает матчер."""
        unit = normalize_unit(item.unit)
        update: dict[str, Any] = {
            "row": pos.row,
            "candidates": [],
            "remaining": left,
            "over_remaining": False,
        }
        if unit is not None and unit != pos.unit:
            update |= {
                "status": MatchStatus.CLARIFY,
                "message": f"В смете «{pos.unit}», а указано «{unit}».",
            }
        elif item.qty is None or item.qty <= 0:
            update |= {"status": MatchStatus.CLARIFY, "message": f"Сколько ({pos.unit})?"}
        else:
            update |= {
                "status": MatchStatus.OK,
                "message": None,
                "over_remaining": _over(item.qty, left),
            }
        return item.model_copy(update=update)

    async def pick_variant(
        self, user_id: int, idx: int, row: int | None, *, token: str | None = None
    ) -> Draft:
        """Мастер выбрал вариант для неоднозначной позиции (row=None — «Нет нужного»)."""
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            item = self._item(draft, idx)
            if row is None:
                new = item.model_copy(
                    update={
                        "status": MatchStatus.NOT_FOUND,
                        "row": None,
                        "candidates": [],
                        "message": "Нужной позиции нет.",
                    }
                )
            else:
                if row not in item.candidates:
                    raise StaleCardError(STALE)  # такого варианта в вопросе не было
                catalog = await self._catalog()
                pos = catalog.by_row(row)
                if pos is None:
                    raise ReportError("Этой позиции больше нет в таблице. Пришлите отчёт заново.")
                new = self._resolve(item, pos, await self._remaining_for(catalog, row))
                new.picked = True  # после записи запомним фразу как синоним
            return await self._save(user_id, draft, idx, new)

    async def set_item_qty(
        self, user_id: int, idx: int, qty: float, *, token: str | None = None
    ) -> Draft:
        """Новый объём позиции (в единицах сметы). Ноль и минус — через удаление позиции."""
        if isinstance(qty, bool) or not math.isfinite(qty) or qty <= 0:
            raise ReportError("Объём должен быть числом больше нуля.")
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            item = self._item(draft, idx)
            if item.row is None:
                raise ReportError("Сначала выберите позицию.")
            catalog = await self._catalog()
            pos = catalog.by_row(item.row)
            if pos is None:
                raise ReportError("Этой позиции больше нет в таблице. Пришлите отчёт заново.")
            left = await self._remaining_for(catalog, item.row)
            new = item.model_copy(
                update={
                    "qty": qty,
                    "unit": pos.unit,
                    "status": MatchStatus.OK,
                    "message": None,
                    "remaining": left,
                    "over_remaining": _over(qty, left),
                }
            )
            return await self._save(user_id, draft, idx, new)

    async def remove_item(
        self, user_id: int, idx: int, *, token: str | None = None
    ) -> Draft | None:
        """Убрать позицию. Не осталось ни одной — черновик удаляется (None)."""
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            self._item(draft, idx)
            items = [i for n, i in enumerate(draft.items) if n != idx]
            if not items:
                await self.storage.delete_draft(user_id)
                return None
            await self.storage.save_draft(user_id, draft.source_text, items)
            return await self.storage.get_draft(user_id)

    async def append(
        self, user_id: int, text: str, *, token: str | None = None
    ) -> tuple[Draft | None, int]:
        """«Добавить»: распознать ещё текст и дописать позиции в конец.

        Возвращает (черновик, сколько позиций добавлено).
        """
        async with self._user_locks[user_id]:
            draft = (
                await self._load(user_id, token) if token else await self.storage.get_draft(user_id)
            )
            new_items = await self._match(text)
            if not new_items:
                return draft, 0
            items = (draft.items if draft else []) + new_items
            source = f"{draft.source_text}\n{text}" if draft else text
            await self.storage.save_draft(user_id, source, items)
            return await self.storage.get_draft(user_id), len(new_items)

    async def cancel(self, user_id: int) -> None:
        async with self._user_locks[user_id]:
            await self.storage.delete_draft(user_id)

    async def _save(self, user_id: int, draft: Draft, idx: int, new: MatchedItem) -> Draft:
        items = list(draft.items)
        items[idx] = new
        await self.storage.save_draft(user_id, draft.source_text, items)
        saved = await self.storage.get_draft(user_id)
        assert saved is not None
        return saved

    # ─────────────── Подтверждение ───────────────

    async def confirm(
        self,
        user_id: int,
        work_date: date,
        *,
        master: str | None = None,
        token: str | None = None,
    ) -> str:
        """Записать подтверждённые позиции (status=OK) в таблицу, журнал и outbox Битрикса.

        Возвращает текст итога. Не записалось — `ReportError` с текстом для мастера,
        черновик цел. Неразобранные позиции остаются в черновике; «нет в смете» не
        блокирует запись и в черновике не остаётся (в итоге о них сказано один раз).
        """
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            ready = [i for i in draft.items if is_ready(i)]
            skipped = [i for i in draft.items if i.status == MatchStatus.NOT_FOUND]
            leftover = [
                i for i in draft.items if not is_ready(i) and i.status != MatchStatus.NOT_FOUND
            ]
            if not ready:
                raise ReportError(
                    "Нечего записывать: все позиции нужно уточнить."
                    if leftover
                    else "Нечего записывать: этих работ нет в смете."
                )

            volumes: dict[int, float] = {}
            for item in ready:  # две фразы про одну строку — одна запись на строку
                assert item.row is not None and item.qty is not None
                volumes[item.row] = round(volumes.get(item.row, 0.0) + item.qty, 6)

            # справочник — до записи: после неё остаются только локальные операции
            catalog = await self._catalog()
            changes = {
                c.row: c
                for c in await self._write_sheet(work_date, volumes, "add", retry="Записать")
            }
            writes = [
                NewWrite(row, qty, "add", changes[row].old if row in changes else None)
                for row, qty in sorted(volumes.items())
            ]
            await self._journal(
                user_id,
                work_date,
                writes,
                master=master or f"Мастер {user_id}",
                catalog=catalog,
                leftover=leftover,
                what="Отчёт записан в таблицу",
            )
            self._learn_synonyms(ready)
            return self._confirmation_text(work_date, ready, leftover, skipped, changes, catalog)

    # ─────────────── Синонимы «за кадром» ───────────────

    def _learn_synonyms(self, ready: list[MatchedItem]) -> None:
        """Варианты, выбранные мастером кнопкой и подтверждённые записью, запоминаем молча.

        Только после записи: неверно выбранный и отменённый вариант в синонимы не попадёт.
        Идёт в фоне — мастер не ждёт обращения к таблице; сбой только в лог.
        """
        pairs: dict[str, int] = {}
        for item in ready:
            if item.picked and item.row is not None:
                key = synonym_key(item.phrase, item.qty)
                if len(key) >= 3:
                    pairs[key] = item.row
        if pairs:
            self._spawn(self._save_synonyms(pairs))

    async def _save_synonyms(self, pairs: dict[str, int]) -> None:
        for key, row in pairs.items():
            try:
                await self.sheets.add_synonym(key, row)
                logger.info("Запомнил синоним «{}» → строка {}", key, row)
            except Exception as e:
                logger.warning("Синоним «{}» не сохранён: {}", key, e)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Дождаться фоновых задач (при остановке бота и в тестах)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    # ─────────────── Исправление записанного ───────────────

    async def correct_today(
        self, user_id: int, row: int, qty: float, *, master: str | None = None
    ) -> str:
        """Заменить (set) сегодняшнее значение строки; в журнал — с prev_value.

        Возвращает текст итога; не получилось — `ReportError`.
        """
        if isinstance(qty, bool) or not math.isfinite(qty) or qty < 0:
            raise ReportError("Объём должен быть числом, не меньше нуля.")
        async with self._user_locks[user_id]:
            catalog = await self._catalog()
            pos = catalog.by_row(row)
            if pos is None:
                raise ReportError("Эта строка не является позицией сметы.")
            today = self._today()
            changes = await self._write_sheet(today, {row: qty}, "set", retry="Исправить")
            old = next((c.old for c in changes if c.row == row), None)
            await self._journal(
                user_id,
                today,
                [NewWrite(row, qty, "set", old)],
                master=master or f"Мастер {user_id}",
                catalog=catalog,
                leftover=None,
                what="Исправление записано в таблицу",
            )
            name = compact_name(pos.name)
            if qty == 0:
                was = f"{fmt_qty(old or 0)} {pos.unit}"
                return f"✅ Очищено за {today:%d.%m.%Y}: {name} (было {was})"
            return (
                f"✅ Исправлено за {today:%d.%m.%Y}: {name} — "
                f"было {fmt_qty(old or 0)}, стало {fmt_qty(qty)} {pos.unit}"
            )

    async def today_report(self, user_id: int) -> tuple[date, list[TodayLine]]:
        """Что мастер записал сегодня (по журналу бота), по строкам."""
        today = self._today()
        catalog = await self._catalog()
        totals = fold_writes(await self.storage.get_writes(user_id, today))
        lines = []
        for row in sorted(totals):
            pos = catalog.by_row(row)
            lines.append(TodayLine(row, self._name(pos, row), totals[row], pos.unit if pos else ""))
        return today, lines

    # ─────────────── Остаток ───────────────

    async def remaining(self, row: int) -> tuple[float, float, str]:
        """(план, выполнено, ед.) для ответа «сколько осталось по …».

        Строка не позиция → ValueError; ошибки таблицы — `SheetsError`.
        """
        catalog = await self.sheets.load_catalog()
        pos = catalog.by_row(row)
        if pos is None:
            raise ValueError(f"строка {row} не является позицией сметы")
        done = (await self.sheets.read_done([row])).get(row, 0.0)
        return pos.planned, done, pos.unit

    # ─────────────── Внутреннее: таблица ───────────────

    async def _catalog(self) -> Catalog:
        try:
            return await self.sheets.load_catalog()
        except SheetsError as e:
            raise ReportError(f"⚠️ {e}") from e

    async def _write_sheet(
        self, work_date: date, volumes: dict[int, float], mode: Mode, *, retry: str
    ) -> list[CellChange]:
        """Запись в Sheets; любой сбой — ReportError с текстом для мастера."""
        try:
            return await self.sheets.write_volumes(work_date, volumes, mode=mode)
        except SheetChangedError as e:
            raise ReportError(
                "⚠️ Таблицу изменили, пока отчёт ждал подтверждения: строки сместились. "
                "Отправьте отчёт заново."
            ) from e
        except SheetsError as e:
            logger.error("Запись в таблицу не удалась ({}), черновик сохранён", e)
            raise ReportError(
                f"⚠️ {e} Отчёт сохранён — нажмите «{retry}» ещё раз чуть позже."
            ) from e
        except ValueError as e:
            logger.exception("В таблицу ушли некорректные данные: {}", volumes)
            raise ReportError(
                "⚠️ Не удалось записать: в отчёте некорректные позиции. Отправьте отчёт заново."
            ) from e

    # ─────────────── Внутреннее: журнал и Битрикс ───────────────

    async def _journal(
        self,
        user_id: int,
        work_date: date,
        writes: list[NewWrite],
        *,
        master: str,
        catalog: Catalog,
        leftover: list[MatchedItem] | None,
        what: str,
    ) -> None:
        """После успешной записи в таблицу: журнал + outbox + черновик одной транзакцией.

        Если сохранить не удалось — таблица уже изменена, и повторное «Записать» удвоило бы
        объёмы. Поэтому говорим об этом прямо и просим не повторять.
        """
        try:
            payload = await self._bitrix_payload(user_id, work_date, writes, master, catalog)
            if leftover is None:
                await self.storage.record_correction(
                    user_id, work_date, writes[0], outbox_payload=payload
                )
            else:
                await self.storage.record_confirmation(
                    user_id, work_date, writes, outbox_payload=payload, leftover=leftover
                )
        except Exception as e:
            logger.exception("{}, но журнал не сохранён: {}", what, writes)
            raise ReportError(
                "⚠️ В таблицу записано, но сохранить журнал не удалось. Повторно не нажимайте — "
                "объёмы удвоятся. Сообщите администратору."
            ) from e

    async def _bitrix_payload(
        self,
        user_id: int,
        work_date: date,
        new_writes: list[NewWrite],
        master: str,
        catalog: Catalog,
    ) -> dict | None:
        """Снимок всего дня мастера для Битрикса (None — Битрикс не настроен).

        Элемент за дату обновляется целиком, поэтому шлём все объёмы дня, а не только
        этот отчёт: иначе второй отчёт за день затёр бы первый.
        """
        if self.bitrix is None:
            return None
        existing = await self.storage.get_writes(user_id, work_date)
        totals = fold_writes([*existing, *new_writes])
        rows = sorted(totals)
        lines = [self._describe(catalog, row, totals[row]) for row in rows]
        link_row = rows[0] if rows else new_writes[0].row
        return {
            "user_id": user_id,
            "master": master,
            "work_date": work_date.isoformat(),
            "lines": lines,
            "sheet_url": self.sheets.row_link(link_row),
        }

    @staticmethod
    def _name(pos: Position | None, row: int) -> str:
        return compact_name(pos.name) if pos else f"строка {row}"

    def _describe(self, catalog: Catalog, row: int, qty: float) -> str:
        """«Фреонопровод медный, Д6,35 — 15 м»."""
        pos = catalog.by_row(row)
        return f"{self._name(pos, row)} — {fmt_qty(qty)} {pos.unit if pos else ''}".rstrip()

    def _confirmation_text(
        self,
        work_date: date,
        ready: list[MatchedItem],
        leftover: list[MatchedItem],
        skipped: list[MatchedItem],
        changes: dict[int, CellChange],
        catalog: Catalog,
    ) -> str:
        lines = [f"✅ Записано в таблицу за {work_date:%d.%m.%Y}:"]
        done_rows: set[int] = set()
        for item in ready:
            assert item.row is not None
            if item.row in done_rows:
                continue
            done_rows.add(item.row)
            qty = sum(i.qty or 0.0 for i in ready if i.row == item.row)
            line = "• " + self._describe(catalog, item.row, qty)
            change = changes.get(item.row)
            if change is not None and change.old > 0:
                line += f" (всего за день {fmt_qty(change.new)})"
            lines.append(line)
        over = [i for i in ready if i.over_remaining]
        if over:
            names = ", ".join(
                sorted({self._name(catalog.by_row(i.row or 0), i.row or 0) for i in over})
            )
            lines.append(f"⚠️ Больше остатка по смете: {names}")
        if leftover:
            phrases = ", ".join(f"«{i.phrase}»" for i in leftover[:5])
            lines.append(f"❓ Не записано, нужно уточнить: {phrases}")
        if skipped:
            lines.append("Нет в смете: " + ", ".join(f"«{i.phrase}»" for i in skipped[:5]))
        return "\n".join(lines)

    # ─────────────── Отправка в Битрикс (outbox) ───────────────

    async def flush_outbox(self) -> FlushResult:
        """Отправить накопившиеся задания в Битрикс; вызывать периодически.

        Для одного мастера и даты важен только самый свежий снимок — старые помечаются
        выполненными без отправки, чтобы повтор давнего задания не откатил Битрикс назад.
        Неудачи копятся с паузами 30 с … 15 мин и повторяются, пока не получится.
        """
        if self.bitrix is None:
            return FlushResult()
        async with self._flush_lock:
            pending = await self.storage.pending_outbox()
            latest: dict[tuple[date, object], OutboxItem] = {}
            for item in pending:  # по возрастанию id: последний выигрывает
                latest[(item.work_date, item.payload.get("user_id"))] = item
            stale = [
                i.id for i in pending if latest[(i.work_date, i.payload.get("user_id"))] is not i
            ]
            if stale:
                await self.storage.mark_outbox_done(stale, note="заменено более новым снимком")

            sent = failed = waiting = 0
            now = self._clock()
            for item in latest.values():
                if self._next_try.get(item.id, 0.0) > now:
                    waiting += 1
                    continue
                try:
                    p = item.payload
                    await self.bitrix.upsert_daily_report(
                        item.work_date, p["master"], p["lines"], p["sheet_url"]
                    )
                except Exception as e:
                    failed += 1
                    error = _safe_error(e)
                    await self.storage.mark_outbox_failed(item.id, error)
                    delay = backoff_seconds(item.attempts + 1)
                    self._next_try[item.id] = now + delay
                    logger.warning(
                        "Битрикс: задание {} не отправлено ({}), повтор через {:.0f} с",
                        item.id,
                        error,
                        delay,
                    )
                else:
                    sent += 1
                    self._next_try.pop(item.id, None)
                    await self.storage.mark_outbox_done([item.id])
                    logger.info("Битрикс: отчёт за {} отправлен", item.work_date)
            return FlushResult(sent, failed, waiting, len(stale))


async def run_outbox_worker(service: ReportService, interval_s: float = 60.0) -> None:
    """Фоновая задача бота: раз в interval_s отправлять накопленное в Битрикс.

    Останавливается отменой задачи. Любая ошибка проходки — в лог, цикл живёт дальше.
    """
    while True:
        try:
            await service.flush_outbox()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Разбор очереди Битрикса упал, продолжаю")
        await asyncio.sleep(interval_s)
