"""Хэндлеры aiogram. Тонкие: логика — в services/report.py, тексты — в bot/render.py.

Состояние сценария живёт в черновике (SQLite), а не в памяти бота; FSM нужна только для
режимов «следующее сообщение — число / ещё позиции». Поэтому после перезапуска кнопки
карточки работают, а от старых карточек защищает отпечаток черновика в кнопках.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from loguru import logger

from m100bot.bot import render
from m100bot.bot.keyboards import (
    Act,
    CardAction,
    Today,
    TodayAction,
    back_keyboard,
    choice_keyboard,
    edit_pick_keyboard,
    item_edit_keyboard,
    qty_question_keyboard,
    review_keyboard,
    sheet_link_keyboard,
    stale_draft_keyboard,
    today_item_keyboard,
    today_keyboard,
    today_pick_keyboard,
)
from m100bot.bot.states import ReportFlow
from m100bot.matching.llm import LlmError
from m100bot.services.report import ReportError, ReportService, StaleCardError
from m100bot.sheets.client import SheetsError
from m100bot.storage.db import Draft

# Ошибки, тексты которых уже написаны для мастера
USER_ERRORS = (ReportError, SheetsError, LlmError)


# ─────────────── Telegram-мелочи ───────────────


async def edit_or_send(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Поменять сообщение на месте; нельзя (удалено, слишком старое) — отправить новое."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
            return message_id
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return message_id
            logger.debug("Не удалось изменить сообщение {}: {}", message_id, e)
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    return sent.message_id


async def delete_quietly(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest as e:
        logger.debug("Не удалось удалить сообщение {}: {}", message_id, e)


async def strip_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Убрать кнопки у сообщения (старая карточка, на которую нажимать уже нельзя)."""
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest as e:
        logger.debug("Не удалось убрать кнопки у {}: {}", message_id, e)


# ─────────────── Экраны ───────────────


async def show_card(
    bot: Bot,
    chat_id: int,
    service: ReportService,
    state: FSMContext,
    user_id: int,
    *,
    edit_id: int | None = None,
) -> Draft | None:
    """Показать карточку отчёта (на месте edit_id, если можно)."""
    draft = await service.get_draft(user_id)
    if draft is None:
        return None
    catalog = await service.catalog()
    ready, unresolved = render.card_counts(draft.items)
    text = render.card_text(draft.items, service.today(), catalog)
    card_id = await edit_or_send(
        bot, chat_id, edit_id, text, review_keyboard(draft.token, ready, unresolved)
    )
    await state.update_data(card_id=card_id, token=draft.token)
    return draft


async def ask(
    bot: Bot,
    chat_id: int,
    service: ReportService,
    state: FSMContext,
    user_id: int,
    *,
    only_idx: int | None = None,
) -> None:
    """Задать мастеру следующий вопрос — по одной позиции за раз. Вопросов нет — режим сброшен."""
    data = await state.get_data()
    await delete_quietly(bot, chat_id, data.get("question_id"))
    await state.update_data(question_id=None)
    draft = await service.get_draft(user_id)
    if draft is None:
        await state.set_state(None)
        return
    catalog = await service.catalog()
    q = (
        render.next_question(draft.items, catalog)
        if only_idx is None
        else render.question_for(draft.items, only_idx, catalog)
    )
    if q is None:
        await state.set_state(None)
        return
    if q.kind == "choice":
        markup = choice_keyboard(draft.token, q.idx, q.options)
        await state.set_state(None)
    else:
        markup = qty_question_keyboard(draft.token, q.idx)
        await state.set_state(ReportFlow.editing_qty)
    sent = await bot.send_message(chat_id, q.text, reply_markup=markup)
    await state.update_data(question_id=sent.message_id, idx=q.idx, token=draft.token)


async def refresh(
    bot: Bot,
    chat_id: int,
    service: ReportService,
    state: FSMContext,
    user_id: int,
    *,
    card_id: int | None,
) -> None:
    """Перерисовать карточку и задать следующий вопрос."""
    if await show_card(bot, chat_id, service, state, user_id, edit_id=card_id) is None:
        await state.set_state(None)
        return
    await ask(bot, chat_id, service, state, user_id)


async def show_today(
    bot: Bot, chat_id: int, service: ReportService, user_id: int, *, edit_id: int | None = None
) -> None:
    work_date, lines = await service.today_report(user_id)
    url = service.table_link(lines[0].row if lines else None)
    await edit_or_send(
        bot, chat_id, edit_id, render.today_text(work_date, lines), today_keyboard(url, bool(lines))
    )


# ─────────────── Команды ───────────────


async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Здравствуйте! " + render.HELP)


async def on_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(render.HELP)


async def on_table(message: Message, state: FSMContext, service: ReportService) -> None:
    await state.clear()
    await message.answer(render.TABLE_TEXT, reply_markup=sheet_link_keyboard(service.table_link()))


async def on_today(message: Message, state: FSMContext, bot: Bot, service: ReportService) -> None:
    await state.clear()
    if message.from_user is None:
        return
    try:
        await show_today(bot, message.chat.id, service, message.from_user.id)
    except USER_ERRORS as e:
        await message.answer(str(e))


async def on_unknown_command(message: Message) -> None:
    await message.answer(render.UNKNOWN_COMMAND)


async def on_voice(message: Message) -> None:
    # TODO: скачать файл → Transcriber → тот же путь, что и текст (process_report)
    await message.answer(render.VOICE_STUB)


async def on_other(message: Message) -> None:
    await message.answer(render.OTHER_CONTENT)


# ─────────────── Новый отчёт ───────────────


async def process_report(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """Текст мастера → карточка.

    Незаписанного отчёта нет — начинаем новый. Есть сегодняшний — дописываем в него:
    мастер часто присылает работы по одной, и молча стирать прежние нельзя. Есть
    вчерашний (или старше) — спрашиваем, дописать в него или начать новый.
    """
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    draft = await service.get_draft(uid)
    if draft is not None and service.draft_day(draft) != service.today():
        await ask_about_stale_draft(message, state, service, draft)
        return
    placeholder = await message.answer(render.THINKING)
    await recognize_into_card(
        bot,
        chat_id,
        service,
        state,
        uid,
        message.text,
        placeholder.message_id,
        append=draft is not None,
    )


async def recognize_into_card(
    bot: Bot,
    chat_id: int,
    service: ReportService,
    state: FSMContext,
    uid: int,
    text: str,
    placeholder_id: int,
    *,
    append: bool,
    token: str | None = None,
) -> None:
    """Распознать текст (дописать в черновик или начать новый) и показать карточку на месте
    placeholder_id. Работ не нашлось — прежний черновик и карточка остаются как были."""
    old = await state.get_data()
    try:
        if append:
            _, found = await service.append(uid, text, token=token)
        else:
            found = len(await service.recognize(uid, text))
    except StaleCardError:
        raise  # кнопка со старой карточки — разберётся guarded()
    except USER_ERRORS as e:
        await edit_or_send(bot, chat_id, placeholder_id, str(e))
        return
    except Exception:
        logger.exception("Не удалось разобрать отчёт")
        await edit_or_send(bot, chat_id, placeholder_id, render.FAILED)
        return
    if not found:
        await edit_or_send(bot, chat_id, placeholder_id, render.NOTHING_FOUND)
        return
    # карточка будет новая: у старой убираем кнопки, открытый вопрос задаём заново
    await strip_keyboard(bot, chat_id, old.get("card_id"))
    await delete_quietly(bot, chat_id, old.get("question_id"))
    await state.clear()
    await refresh(bot, chat_id, service, state, uid, card_id=placeholder_id)


async def ask_about_stale_draft(
    message: Message, state: FSMContext, service: ReportService, draft: Draft
) -> None:
    """Незаписанный отчёт за прошлый день: текст откладываем, мастер решает кнопкой."""
    n = len(draft.items)
    text = render.STALE_DRAFT.format(
        day=service.draft_day(draft),
        n=n,
        word=render.plural(n, ("позиция", "позиции", "позиций")),
    )
    await state.set_state(None)
    await state.update_data(pending_text=message.text)
    await message.answer(text, reply_markup=stale_draft_keyboard(draft.token))


async def on_report(message: Message, state: FSMContext, bot: Bot, service: ReportService) -> None:
    await process_report(message, state, bot, service)


# ─────────────── Ответы текстом в режимах ───────────────


async def read_number(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> float | None:
    """Число из ответа мастера. Не число: длинный текст — это новый отчёт (разбираем его),
    короткий — просим ответить цифрами. В обоих случаях None."""
    assert message.text is not None
    qty = render.parse_qty(message.text)
    if qty is not None:
        return qty
    if render.looks_like_report(message.text):
        await process_report(message, state, bot, service)
    else:
        await message.answer(render.BAD_NUMBER)
    return None


async def on_qty_input(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """Ответ числом: на вопрос «сколько?» или новый объём при «Исправить»."""
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    qty = await read_number(message, state, bot, service)
    if qty is None:
        return
    data = await state.get_data()
    try:
        await service.set_item_qty(uid, data.get("idx", -1), qty, token=data.get("token"))
    except StaleCardError as e:
        await state.clear()
        await message.answer(str(e))
        return
    except USER_ERRORS as e:
        await message.answer(str(e))
        return
    await state.set_state(None)
    await refresh(bot, chat_id, service, state, uid, card_id=data.get("card_id"))


async def on_add_text(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """«Добавить»: ещё позиции текстом — дописываются в конец отчёта."""
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    data = await state.get_data()
    await bot.send_chat_action(chat_id, "typing")
    try:
        _, added = await service.append(uid, message.text, token=data.get("token"))
    except StaleCardError as e:
        await state.clear()
        await message.answer(str(e))
        return
    except USER_ERRORS as e:
        await message.answer(str(e))
        return
    if added == 0:
        await message.answer(render.NOTHING_ADDED)
        return
    await state.set_state(None)
    await refresh(bot, chat_id, service, state, uid, card_id=data.get("card_id"))


async def on_today_qty(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """Новый объём для строки, записанной сегодня."""
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    qty = await read_number(message, state, bot, service)
    if qty is None:
        return
    data = await state.get_data()
    try:
        result = await service.correct_today(
            uid, data.get("row", 0), qty, master=message.from_user.full_name
        )
    except USER_ERRORS as e:
        await message.answer(str(e))
        return
    await state.clear()
    try:
        await show_today(bot, chat_id, service, uid, edit_id=data.get("today_msg"))
    except USER_ERRORS as e:
        logger.warning("Список «сегодня» не обновлён: {}", e)
    await message.answer(result)


# ─────────────── Кнопки карточки ───────────────


@dataclass(frozen=True)
class Tap:
    """Нажатие кнопки: всё, что нужно обработчику действия."""

    bot: Bot
    service: ReportService
    state: FSMContext
    uid: int
    master: str  # имя мастера в Telegram — для журнала и Битрикса
    chat_id: int
    msg_id: int  # сообщение, под которым нажата кнопка
    data: dict[str, Any]  # данные режима (FSM) на момент нажатия

    async def refresh(self, card_id: int | None) -> None:
        await refresh(self.bot, self.chat_id, self.service, self.state, self.uid, card_id=card_id)


async def make_tap(call: CallbackQuery, state: FSMContext, bot: Bot, service: ReportService) -> Tap:
    assert call.message is not None
    return Tap(
        bot,
        service,
        state,
        call.from_user.id,
        call.from_user.full_name,
        call.message.chat.id,
        call.message.message_id,
        await state.get_data(),
    )


async def card_ok(t: Tap, cb: Act) -> None:
    """«Записать»: в таблицу, затем итог на месте карточки; неразобранное — новой карточкой."""
    draft = await t.service.get_draft(t.uid, token=cb.t)
    assert draft is not None
    rows = sorted({i.row for i in draft.items if i.is_ready and i.row is not None})
    summary = await t.service.confirm(t.uid, t.service.today(), master=t.master, token=cb.t)
    link = t.service.table_link(rows[0] if rows else None)
    await edit_or_send(t.bot, t.chat_id, t.msg_id, summary, sheet_link_keyboard(link))
    await delete_quietly(t.bot, t.chat_id, t.data.get("question_id"))
    await t.state.clear()
    if await t.service.get_draft(t.uid) is not None:  # остались позиции, требующие ответа
        await t.refresh(card_id=None)


async def card_edit(t: Tap, cb: Act) -> None:
    """«Исправить»: выбрать номер позиции."""
    draft = await t.service.get_draft(t.uid, token=cb.t)
    assert draft is not None
    keyboard = edit_pick_keyboard(draft.token, len(draft.items))
    await edit_or_send(t.bot, t.chat_id, t.msg_id, render.EDIT_PICK, keyboard)
    await t.state.update_data(card_id=t.msg_id)


async def card_item(t: Tap, cb: Act) -> None:
    """Выбрана позиция для исправления: неоднозначную — переспросить, остальные — ждать число."""
    draft = await t.service.get_draft(t.uid, token=cb.t)
    assert draft is not None
    if not 0 <= cb.i < len(draft.items):
        raise StaleCardError("Эта карточка устарела. Пришлите отчёт заново.")
    item = draft.items[cb.i]
    await t.state.update_data(card_id=t.msg_id)
    if render.needs_choice(item):
        await show_card(t.bot, t.chat_id, t.service, t.state, t.uid, edit_id=t.msg_id)
        await ask(t.bot, t.chat_id, t.service, t.state, t.uid, only_idx=cb.i)
        return
    catalog = await t.service.catalog()
    text = render.item_edit_text(cb.i + 1, item, catalog)
    await edit_or_send(t.bot, t.chat_id, t.msg_id, text, item_edit_keyboard(draft.token, cb.i))
    if item.row is not None:
        await t.state.set_state(ReportFlow.editing_qty)
        await t.state.update_data(idx=cb.i, token=draft.token)


async def card_delete(t: Tap, cb: Act) -> None:
    """Удалить позицию: из меню исправления (del) или под вопросом «сколько?» (qdel)."""
    # qdel нажата под вопросом, а карточка — отдельное сообщение
    card_id = t.msg_id if cb.a == "del" else t.data.get("card_id")
    draft = await t.service.remove_item(t.uid, cb.i, token=cb.t)
    await t.state.set_state(None)
    if cb.a == "qdel":
        await delete_quietly(t.bot, t.chat_id, t.msg_id)
        await t.state.update_data(question_id=None)
    if draft is None:
        await edit_or_send(t.bot, t.chat_id, card_id, render.EMPTY_REPORT)
        await delete_quietly(t.bot, t.chat_id, t.data.get("question_id"))
        await t.state.clear()
    else:
        await t.refresh(card_id=card_id)


async def card_pick(t: Tap, cb: Act) -> None:
    """Ответ на вопрос «какой вариант?» (r=0 — «Нет нужного»)."""
    await t.service.pick_variant(t.uid, cb.i, cb.r or None, token=cb.t)
    await t.state.set_state(None)
    await delete_quietly(t.bot, t.chat_id, t.msg_id)  # вопрос, на который ответили
    await t.state.update_data(question_id=None)
    await t.refresh(card_id=t.data.get("card_id"))


async def card_add(t: Tap, cb: Act) -> None:
    """«Добавить»: следующий текст мастера допишется к отчёту."""
    draft = await t.service.get_draft(t.uid, token=cb.t)
    assert draft is not None
    await edit_or_send(t.bot, t.chat_id, t.msg_id, render.ADD_PROMPT, back_keyboard(draft.token))
    await t.state.set_state(ReportFlow.adding)
    await t.state.update_data(card_id=t.msg_id, token=draft.token)


async def card_back(t: Tap, cb: Act) -> None:
    await t.service.get_draft(t.uid, token=cb.t)  # устаревшую карточку не оживляем
    await t.state.set_state(None)
    await t.refresh(card_id=t.msg_id)


async def card_cancel(t: Tap, cb: Act) -> None:
    await t.service.get_draft(t.uid, token=cb.t)
    await t.service.cancel(t.uid)
    await edit_or_send(t.bot, t.chat_id, t.msg_id, render.CANCELLED)
    await delete_quietly(t.bot, t.chat_id, t.data.get("question_id"))
    await t.state.clear()


async def _pending_into_card(t: Tap, cb: Act, *, append: bool) -> None:
    """Отложенный текст (см. ask_about_stale_draft) → в старый отчёт или в новый."""
    text = t.data.get("pending_text")
    if not text:
        raise StaleCardError("Эта карточка устарела. Пришлите отчёт заново.")
    await t.service.get_draft(t.uid, token=cb.t)  # черновик с тех пор не меняли
    await t.state.update_data(pending_text=None)
    await edit_or_send(t.bot, t.chat_id, t.msg_id, render.THINKING)
    await recognize_into_card(
        t.bot, t.chat_id, t.service, t.state, t.uid, text, t.msg_id, append=append, token=cb.t
    )


async def card_merge(t: Tap, cb: Act) -> None:
    """«Добавить к нему»: новое сообщение — во вчерашний незаписанный отчёт."""
    await _pending_into_card(t, cb, append=True)


async def card_fresh(t: Tap, cb: Act) -> None:
    """«Новый отчёт»: старый незаписанный отчёт заменяется новым."""
    await _pending_into_card(t, cb, append=False)


CARD_ACTIONS: dict[CardAction, Callable[[Tap, Act], Awaitable[None]]] = {
    "merge": card_merge,
    "fresh": card_fresh,
    "ok": card_ok,
    "edit": card_edit,
    "item": card_item,
    "del": card_delete,
    "qdel": card_delete,
    "pick": card_pick,
    "add": card_add,
    "back": card_back,
    "cancel": card_cancel,
}


# ─────────────── Кнопки /today ───────────────


async def today_edit(t: Tap, cb: Today) -> None:
    """«Исправить» в /today: выбрать строку."""
    _, lines = await t.service.today_report(t.uid)
    if not lines:
        await show_today(t.bot, t.chat_id, t.service, t.uid, edit_id=t.msg_id)
        return
    await edit_or_send(t.bot, t.chat_id, t.msg_id, render.EDIT_PICK, today_pick_keyboard(lines))


async def today_row(t: Tap, cb: Today) -> None:
    """Выбрана строка: ждём новый объём числом."""
    _, lines = await t.service.today_report(t.uid)
    line = next((ln for ln in lines if ln.row == cb.r), None)
    if line is None:  # строку уже очистили
        await t.state.clear()
        await show_today(t.bot, t.chat_id, t.service, t.uid, edit_id=t.msg_id)
        return
    await t.state.set_state(ReportFlow.correcting_today)
    await t.state.update_data(row=cb.r, today_msg=t.msg_id)
    text, keyboard = render.today_item_text(line), today_item_keyboard(cb.r)
    await edit_or_send(t.bot, t.chat_id, t.msg_id, text, keyboard)


async def today_clear(t: Tap, cb: Today) -> None:
    result = await t.service.correct_today(t.uid, cb.r, 0, master=t.master)
    await t.state.clear()
    await show_today(t.bot, t.chat_id, t.service, t.uid, edit_id=t.msg_id)
    await t.bot.send_message(t.chat_id, result)


async def today_back(t: Tap, cb: Today) -> None:
    await t.state.clear()
    await show_today(t.bot, t.chat_id, t.service, t.uid, edit_id=t.msg_id)


TODAY_ACTIONS: dict[TodayAction, Callable[[Tap, Today], Awaitable[None]]] = {
    "edit": today_edit,
    "row": today_row,
    "clear": today_clear,
    "back": today_back,
}


async def guarded(call: CallbackQuery, bot: Bot, action: Callable[[], Awaitable[None]]) -> None:
    """Выполнить действие кнопки; любую ошибку превратить в понятное сообщение."""
    if call.message is None:
        await call.answer()
        return
    chat_id = call.message.chat.id
    try:
        await action()
    except StaleCardError as e:
        await call.answer(str(e), show_alert=True)
        await strip_keyboard(bot, chat_id, call.message.message_id)
        return
    except USER_ERRORS as e:
        await call.answer()
        await bot.send_message(chat_id, str(e))
        return
    except Exception:
        logger.exception("Ошибка при обработке кнопки")
        await call.answer(render.FAILED, show_alert=True)
        return
    await call.answer()


async def on_act(
    call: CallbackQuery, callback_data: Act, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    async def run() -> None:
        tap = await make_tap(call, state, bot, service)
        await CARD_ACTIONS[callback_data.a](tap, callback_data)

    await guarded(call, bot, run)


async def on_today_act(
    call: CallbackQuery, callback_data: Today, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    async def run() -> None:
        tap = await make_tap(call, state, bot, service)
        await TODAY_ACTIONS[callback_data.a](tap, callback_data)

    await guarded(call, bot, run)


# ─────────────── Сборка роутера ───────────────


def build_router() -> Router:
    """Новый роутер на каждый вызов: aiogram не даёт подключить один и тот же дважды."""
    r = Router()
    not_command = ~F.text.startswith("/")

    # команды работают в любом режиме и сбрасывают его
    r.message.register(on_start, CommandStart())
    r.message.register(on_help, Command("help"))
    r.message.register(on_table, Command("table"))
    r.message.register(on_today, Command("today"))
    r.message.register(on_voice, F.voice)

    # ответы в режимах — раньше общего обработчика текста
    r.message.register(on_qty_input, StateFilter(ReportFlow.editing_qty), F.text, not_command)
    r.message.register(on_add_text, StateFilter(ReportFlow.adding), F.text, not_command)
    r.message.register(on_today_qty, StateFilter(ReportFlow.correcting_today), F.text, not_command)

    r.message.register(on_report, F.text, not_command)
    r.message.register(on_unknown_command, F.text)
    r.message.register(on_other)

    r.callback_query.register(on_act, Act.filter())
    r.callback_query.register(on_today_act, Today.filter())
    return r
