"""Сценарий отчёта: распознать → черновик → подтвердить → записать.

Хэндлеры бота тонкие и вызывают только этот сервис.

Правила, на которых всё держится:
- в таблицу пишем только из `confirm` / `correct_today`, то есть после явного «Записать»;
- сбой Sheets не стирает черновик — мастер получает понятный текст и жмёт кнопку ещё раз;
- Битрикс24 напрямую не трогаем: задание кладётся в outbox (в той же транзакции, что
  журнал) и уходит отдельно, `flush_outbox` (сразу в фоне и потом с повторами).
  Недоступный Битрикс не мешает мастеру закончить отчёт. Каждый подтверждённый отчёт —
  свой элемент CRM; исправление меняет элементы тех отчётов, где была позиция;
- каждая запись попадает в журнал `writes` с prev_value — значением ячейки до записи;
- перед записью в таблицу в SQLite кладётся «намерение» (что и куда пишем): если ответ
  Sheets потерялся или журнал не сохранился, повторное нажатие проверяет таблицу
  (`resume_write`) и не удваивает объём; пока намерение не закрыто, черновик не меняется;
- перед записью № и название строки сверяются с отпечатком, который видел мастер;
- «Исправить» меняет только долю мастера в общей ячейке (разница к его итогу);
- кнопки карточки несут отпечаток черновика (`Draft.token`): нажатие на старую карточку
  не должно ничего записать, поэтому правящие методы принимают `token` и сверяют его
  под блокировкой мастера.
"""

import asyncio
import math
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

from m100bot.bitrix.client import BitrixError, BitrixUnavailable
from m100bot.matching.llm import LlmError
from m100bot.matching.matcher import resolve_position
from m100bot.matching.models import MatchedItem, MatchStatus, Understood
from m100bot.sheets.addressing import CellBelowZeroError, CellChange, Mode, RowId
from m100bot.sheets.catalog import Catalog, Position
from m100bot.sheets.client import SheetChangedError, SheetsError, WriteConflictError
from m100bot.storage.db import (
    Draft,
    NewWrite,
    OutboxItem,
    PendingWrite,
    Report,
    Storage,
    fold_writes,
)

BACKOFF_BASE_S = 30.0  # пауза перед второй попыткой отправки в Битрикс
BACKOFF_MAX_S = 900.0  # дальше — раз в 15 минут, пока не получится
# Ошибка настройки Битрикса (нет поля, нет прав, неверный вебхук) сама не пройдёт:
# повторяем раз в час и громко пишем в лог, что нужно исправить
PERMANENT_RETRY_S = 3600.0
# Общий предел на распознавание одного сообщения: повторы LLM и переспрос модели вместе
# иначе могли бы держать мастера на «Разбираю отчёт…» до 8 минут
RECOGNIZE_BUDGET_S = 180.0

NO_DRAFT = "Нет отчёта для записи. Пришлите сообщение с работами."
STALE = "Эта карточка устарела. Пришлите отчёт заново."
SHIFTED = (
    "⚠️ Таблицу изменили, пока отчёт ждал подтверждения: строки сместились. Отправьте отчёт заново."
)


class ReportError(RuntimeError):
    """Отчёт не записан. Текст можно показать мастеру; черновик (если был) остаётся."""


class StaleCardError(ReportError):
    """Нажата кнопка со старой карточки: черновик с тех пор изменился."""


class SheetsLike(Protocol):
    async def load_catalog(self) -> Catalog: ...
    async def load_synonyms(self) -> dict[str, int]: ...
    async def read_done(self, rows: Iterable[int]) -> dict[int, float]: ...
    async def write_volumes(
        self,
        d: date,
        volumes: dict[int, float],
        *,
        mode: Mode = "add",
        expected: Mapping[int, RowId] | None = None,
        before_write: Callable[[list[CellChange]], Awaitable[None]] | None = None,
    ) -> list[CellChange]: ...
    async def resume_write(
        self, d: date, changes: list[CellChange], *, expected: Mapping[int, RowId] | None = None
    ) -> list[CellChange]: ...
    def row_link(self, row: int) -> str: ...


class BitrixLike(Protocol):
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
    ) -> int: ...


class MatcherLike(Protocol):
    async def understand(
        self, text: str, catalog: Catalog, synonyms: Mapping[str, int] | None = None
    ) -> Understood: ...


@dataclass(frozen=True)
class FlushResult:
    sent: int = 0  # ушло в Битрикс
    failed: int = 0  # не получилось, повторим позже
    waiting: int = 0  # ещё ждёт паузы перед повтором
    superseded: int = 0  # заменено более новым заданием того же отчёта


@dataclass(frozen=True)
class RemainingLine:
    """Ответ «сколько осталось» по одной позиции."""

    row: int
    name: str
    unit: str
    planned: float  # по смете (колонка D)
    done: float  # выполнено (колонка I)

    @property
    def left(self) -> float:
        return round(self.planned - self.done, 6)


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


def distribute_correction(values: list[float], target: float) -> list[float]:
    """Новые значения строки в отчётах дня, чтобы сумма стала `target`.

    values — значения в отчётах по порядку подтверждения. Прибавка уходит в последний
    отчёт; уменьшение снимается с последнего, потом с предыдущих (не ниже нуля).
    Так меняется как можно меньше отчётов — и скорее всего тот, что прислан последним.
    """
    new = list(values)
    if not new:
        return new
    diff = round(target - sum(values), 6)
    if diff >= 0:
        new[-1] = round(new[-1] + diff, 6)
        return new
    for i in range(len(new) - 1, -1, -1):
        take = min(new[i], -diff)
        new[i] = round(new[i] - take, 6)
        diff = round(diff + take, 6)
        if diff >= 0:
            break
    return new


def _safe_error(exc: BaseException) -> str:
    """Текст ошибки для журнала: без адресов — в URL вебхука Битрикса лежит секрет."""
    text = re.sub(r"https?://\S+", "<url>", str(exc))
    return f"{type(exc).__name__}: {text}"[:300]


class ReportService:
    def __init__(
        self,
        sheets: SheetsLike,
        storage: Storage,
        matcher: MatcherLike,
        bitrix: BitrixLike | None = None,
        *,
        timezone: str = "Europe/Minsk",
        recognize_budget_s: float = RECOGNIZE_BUDGET_S,
        today: Callable[[], date] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        send_now: bool = True,
    ) -> None:
        self.sheets = sheets
        self.storage = storage
        self.matcher = matcher
        self.bitrix = bitrix  # None — Битрикс не настроен: в outbox ничего не кладём
        self._tz = ZoneInfo(timezone)
        self._today = today or (lambda: datetime.now(self._tz).date())
        self._clock = clock
        self._now = now or (lambda: datetime.now(self._tz))  # время в названии отчёта
        self._recognize_budget_s = recognize_budget_s
        self._user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flush_lock = asyncio.Lock()
        self._next_try: dict[int, float] = {}  # id задания outbox → когда можно повторить
        # Отправлять в Битрикс сразу после записи (в фоне); False — только фоновой задачей
        self._send_now = send_now
        self._last_synonyms: dict[str, int] = {}  # если лист «Синонимы» не прочитался
        self._background: set[asyncio.Task[None]] = set()

    # ─────────────── Справочные методы для бота ───────────────

    def today(self) -> date:
        return self._today()

    def draft_day(self, draft: Draft) -> date:
        """День отчёта: когда пришло первое сообщение (по часовому поясу объекта).

        В этот день и пишем: отчёт от 23:40, подтверждённый в 00:05, — за вчера. Новое
        сообщение дописывается в сегодняшний черновик; про вчерашний бот спрашивает.
        Черновик старой версии без даты — день последнего изменения.
        """
        if draft.work_date is not None:
            return draft.work_date
        return datetime.fromisoformat(draft.updated_at).astimezone(self._tz).date()

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

    async def recognize(
        self, user_id: int, text: str, *, append: bool | None = False, token: str | None = None
    ) -> Understood:
        """Сообщение мастера → отчёт или вопрос об остатке.

        Отчёт: позиции сохраняются в черновик — новый (прежний заменяется) или, при append,
        дописываются в конец текущего (token — проверка, что карточка не устарела).
        append=None — решить под блокировкой мастера: есть черновик — дописать. Так два
        сообщения подряд не затирают друг друга (решение «до блокировки» устаревает).
        Вопрос: черновик не трогаем, позиции нужны только для ответа.
        Пустой отчёт черновик не трогает. Ошибки справочника (`SheetsError`) и
        LLM (`LlmError`) пробрасываются: их тексты уже понятны мастеру.
        """
        async with self._user_locks[user_id]:
            draft = None
            if append is None:
                draft = await self.storage.get_draft(user_id)
            elif append:
                draft = (
                    await self._load(user_id, token)
                    if token
                    else await self.storage.get_draft(user_id)
                )
            understood = await self._understand(text)
            if understood.intent == "question" or not understood.items:
                return understood
            await self._no_pending(user_id)
            items = (draft.items if draft else []) + understood.items
            source = f"{draft.source_text}\n{text}" if draft else text
            await self.storage.save_draft(
                user_id, source, items, work_date=None if draft else self._today()
            )
            return understood

    async def _understand(self, text: str) -> Understood:
        """Распознать по свежему справочнику; в отчёте отметить превышение остатка по смете."""
        catalog = await self.sheets.load_catalog()
        synonyms = await self._synonyms()
        try:
            async with asyncio.timeout(self._recognize_budget_s):
                understood = await self.matcher.understand(text, catalog, synonyms)
        except TimeoutError as e:
            logger.error("Распознавание не уложилось в {:.0f} с", self._recognize_budget_s)
            raise LlmError("Распознавание заняло слишком много времени. Попробуйте позже.") from e
        if understood.intent == "question":
            return understood
        remaining = await self._remaining_map(catalog)
        if remaining is None:
            return understood
        items = [
            i.with_remaining(remaining.get(i.row)) if i.row is not None else i
            for i in understood.items
        ]
        return Understood(understood.intent, items, understood.truncated)

    async def remaining(self, rows: Iterable[int]) -> list[RemainingLine]:
        """План, выполнено и остаток по строкам — для ответа «сколько осталось по …».

        «Выполнено» читается из таблицы заново (колонка I), без кэша: сегодняшние записи
        уже учтены. Сбой таблицы — ReportError с текстом для мастера.
        """
        catalog = await self._catalog()
        wanted = [r for r in dict.fromkeys(rows) if catalog.by_row(r) is not None]
        try:
            done = await self.sheets.read_done(wanted)
        except SheetsError as e:
            raise ReportError(f"⚠️ {e}") from e
        lines = []
        for row in wanted:
            pos = catalog.by_row(row)
            assert pos is not None
            done_qty = done.get(row, 0.0)
            lines.append(RemainingLine(row, pos.short_name, pos.unit, pos.planned, done_qty))
        return lines

    async def _synonyms(self) -> dict[str, int]:
        """Синонимы необязательны: без них бот работает, просто чаще переспрашивает."""
        try:
            self._last_synonyms = await self.sheets.load_synonyms()
        except SheetsError as e:
            logger.warning("Синонимы не прочитаны, работаю с последними известными: {}", e)
        return self._last_synonyms

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

    async def pick_variant(
        self, user_id: int, idx: int, row: int | None, *, token: str | None = None
    ) -> Draft:
        """Мастер выбрал вариант для неоднозначной позиции (row=None — «Нет нужного»)."""
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            item = self._item(draft, idx)
            if row is None:
                new = item.model_copy(
                    update={"status": MatchStatus.NOT_FOUND, "row": None, "candidates": []}
                )
            else:
                if row not in item.candidates:
                    raise StaleCardError(STALE)  # такого варианта в вопросе не было
                catalog = await self._catalog()
                pos = catalog.by_row(row)
                if pos is None:
                    raise ReportError("Этой позиции больше нет в таблице. Пришлите отчёт заново.")
                left = await self._remaining_for(catalog, row)
                new = resolve_position(item, pos).with_remaining(left)
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
                update={"qty": qty, "unit": pos.unit, "status": MatchStatus.OK}
            ).with_remaining(left)
            return await self._save(user_id, draft, idx, new)

    async def remove_item(
        self, user_id: int, idx: int, *, token: str | None = None
    ) -> Draft | None:
        """Убрать позицию. Не осталось ни одной — черновик удаляется (None)."""
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            self._item(draft, idx)
            await self._no_pending(user_id)
            items = [i for n, i in enumerate(draft.items) if n != idx]
            if not items:
                await self.storage.delete_draft(user_id)
                return None
            await self.storage.save_draft(user_id, draft.source_text, items)
            return await self.storage.get_draft(user_id)

    async def cancel(self, user_id: int) -> None:
        """Удалить черновик. Незакрытое намерение записи тоже снимается: мастер сам
        отказался от отчёта (в журнале останется только то, что дошло до журнала)."""
        async with self._user_locks[user_id]:
            if await self.storage.get_pending(user_id) is not None:
                logger.warning("Мастер {} удалил отчёт с прерванной записью", user_id)
                await self.storage.clear_pending(user_id)
            await self.storage.delete_draft(user_id)

    async def _save(self, user_id: int, draft: Draft, idx: int, new: MatchedItem) -> Draft:
        await self._no_pending(user_id)
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
        work_date: date | None = None,
        *,
        master: str | None = None,
        token: str | None = None,
    ) -> str:
        """Записать подтверждённые позиции (status=OK) в таблицу, журнал и outbox Битрикса.

        work_date=None — день отчёта (`draft_day`), а не день нажатия кнопки.
        Возвращает текст итога. Не записалось — `ReportError` с текстом для мастера,
        черновик цел. Неразобранные позиции остаются в черновике; «нет в смете» не
        блокирует запись и в черновике не остаётся (в итоге о них сказано один раз).
        """
        async with self._user_locks[user_id]:
            draft = await self._load(user_id, token)
            work_date = work_date or self.draft_day(draft)
            ready = [i for i in draft.items if i.is_ready]
            skipped = [i for i in draft.items if i.status == MatchStatus.NOT_FOUND]
            leftover = [
                i for i in draft.items if not i.is_ready and i.status != MatchStatus.NOT_FOUND
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
            expected = {
                i.row: (i.pos_number, i.pos_name)
                for i in ready
                if i.row is not None and i.pos_name is not None
            }
            key = f"report:{draft.token}"
            pending = await self._pending_for(user_id, key, work_date)
            if pending is not None:
                written = await self._resume_sheet(user_id, pending, expected, retry="Записать")
            else:
                written = await self._write_sheet(
                    user_id, key, work_date, volumes, "add", expected=expected, retry="Записать"
                )
            changes = {c.row: c for c in written}
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
            return self._confirmation_text(work_date, ready, leftover, skipped, changes, catalog)

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
        """Новый итог мастера за сегодня по строке (0 — очистить свою часть).

        Ячейка общая для всех мастеров, поэтому меняем только долю этого мастера: пишем
        разницу между новым итогом и его итогом по журналу. Чужие объёмы остаются.
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
            mine = fold_writes(await self.storage.get_writes(user_id, today)).get(row, 0.0)
            key = f"correct:{row}:{qty!r}"
            pending = await self._pending_for(user_id, key, today)
            if pending is not None:
                changes = await self._resume_sheet(user_id, pending, None, retry="Исправить")
            else:
                delta = round(qty - mine, 6)
                if delta == 0:
                    return f"Без изменений: {pos.short_name} — {fmt_qty(qty)} {pos.unit}."
                changes = await self._write_sheet(
                    user_id, key, today, {row: delta}, "delta", expected=None, retry="Исправить"
                )
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
            name = pos.short_name
            if qty == 0:
                return f"✅ Очищено за {today:%d.%m.%Y}: {name} (было {fmt_qty(mine)} {pos.unit})"
            return (
                f"✅ Исправлено за {today:%d.%m.%Y}: {name} — "
                f"было {fmt_qty(mine)}, стало {fmt_qty(qty)} {pos.unit}"
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

    # ─────────────── Внутреннее: таблица ───────────────

    async def _catalog(self) -> Catalog:
        try:
            return await self.sheets.load_catalog()
        except SheetsError as e:
            raise ReportError(f"⚠️ {e}") from e

    async def _no_pending(self, user_id: int) -> None:
        """Пока прерванная запись не закрыта, черновик не меняем: иначе повтор «Записать»
        не узнает, что уже лежит в таблице."""
        pending = await self.storage.get_pending(user_id)
        if pending is not None:
            raise ReportError(self._pending_text(pending))

    @staticmethod
    def _pending_text(pending: PendingWrite) -> str:
        if pending.key.startswith("correct:"):
            _, row, qty = pending.key.split(":", 2)
            action = f"повторите исправление строки {row} на {fmt_qty(float(qty))}"
        else:
            action = "нажмите «✅ Записать» на карточке отчёта ещё раз"
        return (
            "⚠️ Прошлая запись в таблицу прервалась. Чтобы не удвоить объёмы, сначала "
            f"{action} — бот проверит таблицу. Отказаться от отчёта — «✖️ Отмена» на карточке."
        )

    async def _pending_for(self, user_id: int, key: str, work_date: date) -> PendingWrite | None:
        """Незакрытая запись этого же действия (повтор после сбоя) или None.
        Незакрытая запись другого действия — ReportError: сначала её."""
        pending = await self.storage.get_pending(user_id)
        if pending is None:
            return None
        if pending.key != key or pending.work_date != work_date:
            raise ReportError(self._pending_text(pending))
        return pending

    async def _write_sheet(
        self,
        user_id: int,
        key: str,
        work_date: date,
        volumes: dict[int, float],
        mode: Mode,
        *,
        expected: Mapping[int, RowId] | None,
        retry: str,
    ) -> list[CellChange]:
        """Запись в Sheets с «намерением» в SQLite; любой сбой — ReportError для мастера."""

        async def remember(changes: list[CellChange]) -> None:
            await self.storage.save_pending(user_id, key, work_date, changes)

        try:
            return await self.sheets.write_volumes(
                work_date, volumes, mode=mode, expected=expected, before_write=remember
            )
        except SheetChangedError as e:
            raise ReportError(SHIFTED) from e
        except SheetsError as e:
            logger.error("Запись в таблицу не удалась ({}), черновик сохранён", e)
            raise ReportError(
                f"⚠️ {e} Отчёт сохранён — нажмите «{retry}» ещё раз чуть позже: бот проверит "
                "таблицу и не удвоит объём."
            ) from e
        except CellBelowZeroError as e:
            raise ReportError(
                f"⚠️ В таблице в строке {e.row} меньше, чем записано вами по журналу бота — "
                "ячейку правили вручную. Проверьте таблицу, бот ничего не менял."
            ) from e
        except ValueError as e:
            logger.exception("В таблицу ушли некорректные данные: {}", volumes)
            raise ReportError(
                "⚠️ Не удалось записать: в отчёте некорректные позиции. Отправьте отчёт заново."
            ) from e

    async def _resume_sheet(
        self,
        user_id: int,
        pending: PendingWrite,
        expected: Mapping[int, RowId] | None,
        *,
        retry: str,
    ) -> list[CellChange]:
        """Повтор после прерванной записи: таблица уже «после» — не пишем, «до» — пишем."""
        try:
            return await self.sheets.resume_write(
                pending.work_date, pending.changes, expected=expected
            )
        except (SheetChangedError, WriteConflictError) as e:
            # проверить нельзя — снимаем намерение и отдаём решение мастеру
            await self.storage.clear_pending(user_id)
            rows = ", ".join(str(c.row) for c in pending.changes)
            raise ReportError(
                f"⚠️ Прошлая запись прервалась, а таблицу с тех пор изменили ({e}). "
                f"Проверьте строки {rows} за {pending.work_date:%d.%m.%Y}: если объёмы уже "
                f"там — нажмите «✖️ Отмена», иначе нажмите «{retry}» ещё раз."
            ) from e
        except SheetsError as e:
            raise ReportError(
                f"⚠️ {e} Нажмите «{retry}» ещё раз чуть позже: бот проверит таблицу и не "
                "удвоит объём."
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

        Если сохранить не удалось — таблица уже изменена, но «намерение» записи осталось:
        повторное нажатие увидит, что объёмы уже в таблице, и только допишет журнал.
        """
        try:
            if leftover is None:
                await self._record_correction(user_id, work_date, writes[0], master, catalog)
            else:
                await self._record_report(user_id, work_date, writes, master, catalog, leftover)
        except Exception as e:
            logger.exception("{}, но журнал не сохранён: {}", what, writes)
            raise ReportError(
                "⚠️ В таблицу записано, но журнал бота не сохранился. Нажмите кнопку ещё раз "
                "чуть позже — бот увидит, что объёмы уже в таблице, и не удвоит их."
            ) from e
        if self.bitrix is not None and self._send_now:
            # Сразу, не дожидаясь фоновой задачи: мастер не ждёт, сбой останется в очереди.
            self._spawn(self._flush_now())

    async def _flush_now(self) -> None:
        try:
            await self.flush_outbox()
        except Exception:
            logger.exception("Отправка в Битрикс сразу после записи не удалась, повторит очередь")

    async def _record_report(
        self,
        user_id: int,
        work_date: date,
        writes: list[NewWrite],
        master: str,
        catalog: Catalog,
        leftover: list[MatchedItem],
    ) -> None:
        """Новый отчёт: журнал + отчёт + задание «создать элемент CRM» (если Битрикс есть)."""
        title = f"Отчет от {self._now():%d.%m.%Y %H:%M}"
        payload = None
        if self.bitrix is not None:
            lines = [self._describe(catalog, w.row, w.qty) for w in writes]
            payload = self._bitrix_payload(
                user_id, work_date, title, master, lines, [w.row for w in writes]
            )
        await self.storage.record_confirmation(
            user_id, work_date, writes, outbox_payload=payload, leftover=leftover, title=title
        )

    async def _record_correction(
        self, user_id: int, work_date: date, write: NewWrite, master: str, catalog: Catalog
    ) -> None:
        """Исправление: новое значение строки раскладывается по отчётам дня, где она была
        (`distribute_correction`); изменённые отчёты уходят в Битрикс заново целиком."""
        reports = await self.storage.reports_with_row(user_id, work_date, write.row)
        if not reports:
            logger.info("Исправление строки {}: отчётов за {} нет", write.row, work_date)
        old = [self._line_qty(r, write.row) for r in reports]
        new = distribute_correction(old, write.qty)
        changed = [(r, q) for r, o, q in zip(reports, old, new, strict=True) if q != o]
        payloads = []
        if self.bitrix is not None:
            for report, qty in changed:
                lines = [
                    self._describe_line(
                        catalog, line.row, qty if line.row == write.row else line.qty, line.reported
                    )
                    for line in report.lines
                ]
                rows = [line.row for line in report.lines]
                payload = self._bitrix_payload(
                    user_id, work_date, report.title, master, lines, rows
                )
                payloads.append({**payload, "report_id": report.id})
        await self.storage.record_correction(
            user_id,
            work_date,
            write,
            line_updates=[(r.id, q) for r, q in changed],
            outbox_payloads=payloads,
        )

    @staticmethod
    def _line_qty(report: Report, row: int) -> float:
        return next(line.qty for line in report.lines if line.row == row)

    def _describe_line(self, catalog: Catalog, row: int, qty: float, reported: float) -> str:
        """Строка отчёта для CRM; исправленная — с пометкой, сколько было в отчёте."""
        text = self._describe(catalog, row, qty)
        if qty != reported:
            pos = catalog.by_row(row)
            was = f"{fmt_qty(reported)} {pos.unit if pos else ''}".rstrip()
            text += f" (исправлено, было {was})"
        return text

    def _bitrix_payload(
        self,
        user_id: int,
        work_date: date,
        title: str,
        master: str,
        lines: list[str],
        rows: list[int],
    ) -> dict[str, Any]:
        """Задание outbox: элемент CRM одного отчёта целиком."""
        return {
            "user_id": user_id,
            "master": master,
            "work_date": work_date.isoformat(),
            "title": title,
            "lines": lines,
            "sheet_url": self.sheets.row_link(min(rows)),
        }

    @staticmethod
    def _name(pos: Position | None, row: int) -> str:
        return pos.short_name if pos else f"строка {row}"

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

        Задание = элемент CRM одного отчёта целиком. Для отчёта важно только самое свежее
        задание (после исправления) — старые помечаются выполненными без отправки.
        id созданного элемента хранится в отчёте: следующие задания обновляют его.
        Неудачи копятся с паузами 30 с … 15 мин и повторяются, пока не получится.
        """
        if self.bitrix is None:
            return FlushResult()
        async with self._flush_lock:
            pending = await self.storage.pending_outbox()
            legacy = [i.id for i in pending if "report_id" not in i.payload]
            if legacy:  # снимки дня из прошлой версии бота — у элементов была другая схема
                await self.storage.mark_outbox_done(legacy, note="старый формат, не отправлено")
            current = [i for i in pending if "report_id" in i.payload]
            latest: dict[int, OutboxItem] = {}
            for item in current:  # по возрастанию id: последний выигрывает
                latest[item.payload["report_id"]] = item
            stale = [i.id for i in current if latest[i.payload["report_id"]] is not i]
            if stale:
                await self.storage.mark_outbox_done(stale, note="заменено более новым заданием")

            sent = failed = waiting = 0
            now = self._clock()
            for report_id, item in latest.items():
                if self._next_try.get(item.id, 0.0) > now:
                    waiting += 1
                    continue
                try:
                    bitrix_id = await self._send_report(report_id, item)
                except Exception as e:
                    failed += 1
                    error = _safe_error(e)
                    await self.storage.mark_outbox_failed(item.id, error)
                    if isinstance(e, BitrixError) and not isinstance(e, BitrixUnavailable):
                        delay = PERMANENT_RETRY_S
                        logger.error(
                            "Битрикс: задание {} не отправлено — ошибка настройки ({}). "
                            "Проверьте вебхук и поля смарт-процесса (scripts/try_bitrix.py "
                            "check); повтор через час",
                            item.id,
                            error,
                        )
                    else:
                        delay = backoff_seconds(item.attempts + 1)
                        logger.warning(
                            "Битрикс: задание {} не отправлено ({}), повтор через {:.0f} с",
                            item.id,
                            error,
                            delay,
                        )
                    self._next_try[item.id] = now + delay
                else:
                    sent += 1
                    self._next_try.pop(item.id, None)
                    await self.storage.mark_outbox_done([item.id])
                    logger.info("Битрикс: «{}» → элемент {}", item.payload["title"], bitrix_id)
            return FlushResult(sent, failed, waiting, len(stale))

    async def _send_report(self, report_id: int, item: OutboxItem) -> int:
        assert self.bitrix is not None
        report = await self.storage.get_report(report_id)
        known_id = report.bitrix_id if report else None
        p = item.payload
        bitrix_id = await self.bitrix.save_report(
            known_id,
            title=p["title"],
            work_date=item.work_date,
            master_id=p["user_id"],
            master=p["master"],
            lines=p["lines"],
            sheet_url=p["sheet_url"],
        )
        if bitrix_id != known_id:
            await self.storage.set_bitrix_id(report_id, bitrix_id)
        return bitrix_id


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
