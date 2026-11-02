"""Хэндлеры aiogram. Тонкие: логика — в services/report.py, тексты — в bot/render.py.

Состояние сценария живёт в черновике (SQLite), а не в памяти бота; FSM нужна только для
режимов «следующее сообщение — число / ещё позиции». Поэтому после перезапуска кнопки
карточки работают, а от старых карточек защищает отпечаток черновика в кнопках.
"""

from collections.abc import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from loguru import logger

from m100bot.bot import render
from m100bot.bot.keyboards import (
    Act,
    Today,
    back_keyboard,
    choice_keyboard,
    edit_pick_keyboard,
    item_edit_keyboard,
    qty_question_keyboard,
    review_keyboard,
    sheet_link_keyboard,
    today_item_keyboard,
    today_keyboard,
    today_pick_keyboard,
)
from m100bot.bot.states import ReportFlow
from m100bot.matching.llm import LlmError
from m100bot.services.report import ReportError, ReportService, StaleCardError, is_ready
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
    """Текст мастера → карточка. Прежний черновик заменяется, а если работ не нашлось — остаётся."""
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    old = await state.get_data()
    placeholder = await message.answer(render.THINKING)
    try:
        items = await service.recognize(uid, message.text)
    except USER_ERRORS as e:
        await edit_or_send(bot, chat_id, placeholder.message_id, str(e))
        return
    except Exception:
        logger.exception("Не удалось разобрать отчёт")
        await edit_or_send(bot, chat_id, placeholder.message_id, render.FAILED)
        return
    if not items:
        await edit_or_send(bot, chat_id, placeholder.message_id, render.NOTHING_FOUND)
        return
    # новый черновик: старая карточка и вопрос больше не действуют
    await strip_keyboard(bot, chat_id, old.get("card_id"))
    await delete_quietly(bot, chat_id, old.get("question_id"))
    await state.clear()
    await refresh(bot, chat_id, service, state, uid, card_id=placeholder.message_id)


async def on_report(message: Message, state: FSMContext, bot: Bot, service: ReportService) -> None:
    await process_report(message, state, bot, service)


# ─────────────── Ответы текстом в режимах ───────────────


async def on_qty_input(
    message: Message, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """Ответ числом: на вопрос «сколько?» или новый объём при «Исправить»."""
    if message.from_user is None or not message.text:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    qty = render.parse_qty(message.text)
    if qty is None:
        if render.looks_like_report(message.text):  # это не число, а новый отчёт
            await process_report(message, state, bot, service)
        else:
            await message.answer(render.BAD_NUMBER)
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
    qty = render.parse_qty(message.text)
    if qty is None:
        if render.looks_like_report(message.text):
            await process_report(message, state, bot, service)
        else:
            await message.answer(render.BAD_NUMBER)
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


async def act(
    call: CallbackQuery, cb: Act, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    """Действие над карточкой. Ошибки (в т.ч. устаревшая карточка) ловит guarded()."""
    assert call.message is not None
    uid, chat_id, msg_id = call.from_user.id, call.message.chat.id, call.message.message_id
    data = await state.get_data()

    if cb.a == "ok":
        draft = await service.get_draft(uid, token=cb.t)
        assert draft is not None
        rows = sorted({i.row for i in draft.items if is_ready(i) and i.row is not None})
        summary = await service.confirm(
            uid, service.today(), master=call.from_user.full_name, token=cb.t
        )
        link = service.table_link(rows[0] if rows else None)
        await edit_or_send(bot, chat_id, msg_id, summary, sheet_link_keyboard(link))
        await delete_quietly(bot, chat_id, data.get("question_id"))
        await state.clear()
        if await service.get_draft(uid) is not None:  # остались позиции, требующие ответа
            await refresh(bot, chat_id, service, state, uid, card_id=None)

    elif cb.a == "edit":
        draft = await service.get_draft(uid, token=cb.t)
        assert draft is not None
        await edit_or_send(
            bot,
            chat_id,
            msg_id,
            render.EDIT_PICK,
            edit_pick_keyboard(draft.token, len(draft.items)),
        )
        await state.update_data(card_id=msg_id)

    elif cb.a == "item":
        draft = await service.get_draft(uid, token=cb.t)
        assert draft is not None
        if not 0 <= cb.i < len(draft.items):
            raise StaleCardError("Эта карточка устарела. Пришлите отчёт заново.")
        item = draft.items[cb.i]
        await state.update_data(card_id=msg_id)
        if render.needs_choice(item):  # неоднозначная — снова спрашиваем про неё
            await show_card(bot, chat_id, service, state, uid, edit_id=msg_id)
            await ask(bot, chat_id, service, state, uid, only_idx=cb.i)
            return
        catalog = await service.catalog()
        text = render.item_edit_text(cb.i + 1, item, catalog)
        await edit_or_send(bot, chat_id, msg_id, text, item_edit_keyboard(draft.token, cb.i))
        if item.row is not None:
            await state.set_state(ReportFlow.editing_qty)
            await state.update_data(idx=cb.i, token=draft.token)

    elif cb.a in ("del", "qdel"):
        # qdel нажата под вопросом, а карточка — отдельное сообщение
        card_id = msg_id if cb.a == "del" else data.get("card_id")
        draft = await service.remove_item(uid, cb.i, token=cb.t)
        await state.set_state(None)
        if cb.a == "qdel":
            await delete_quietly(bot, chat_id, msg_id)
            await state.update_data(question_id=None)
        if draft is None:
            await edit_or_send(bot, chat_id, card_id, render.EMPTY_REPORT)
            await delete_quietly(bot, chat_id, data.get("question_id"))
            await state.clear()
        else:
            await refresh(bot, chat_id, service, state, uid, card_id=card_id)

    elif cb.a == "pick":
        await service.pick_variant(uid, cb.i, cb.r or None, token=cb.t)
        await state.set_state(None)
        await delete_quietly(bot, chat_id, msg_id)  # вопрос, на который ответили
        await state.update_data(question_id=None)
        await refresh(bot, chat_id, service, state, uid, card_id=data.get("card_id"))

    elif cb.a == "add":
        draft = await service.get_draft(uid, token=cb.t)
        assert draft is not None
        await edit_or_send(bot, chat_id, msg_id, render.ADD_PROMPT, back_keyboard(draft.token))
        await state.set_state(ReportFlow.adding)
        await state.update_data(card_id=msg_id, token=draft.token)

    elif cb.a == "back":
        await service.get_draft(uid, token=cb.t)  # устаревшую карточку не оживляем
        await state.set_state(None)
        await refresh(bot, chat_id, service, state, uid, card_id=msg_id)

    elif cb.a == "cancel":
        await service.get_draft(uid, token=cb.t)
        await service.cancel(uid)
        await edit_or_send(bot, chat_id, msg_id, render.CANCELLED)
        await delete_quietly(bot, chat_id, data.get("question_id"))
        await state.clear()


async def act_today(
    call: CallbackQuery, cb: Today, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    assert call.message is not None
    uid, chat_id, msg_id = call.from_user.id, call.message.chat.id, call.message.message_id

    if cb.a == "edit":
        _, lines = await service.today_report(uid)
        if not lines:
            await show_today(bot, chat_id, service, uid, edit_id=msg_id)
            return
        await edit_or_send(bot, chat_id, msg_id, render.EDIT_PICK, today_pick_keyboard(lines))

    elif cb.a == "row":
        _, lines = await service.today_report(uid)
        line = next((ln for ln in lines if ln.row == cb.r), None)
        if line is None:  # строку уже очистили
            await state.clear()
            await show_today(bot, chat_id, service, uid, edit_id=msg_id)
            return
        await state.set_state(ReportFlow.correcting_today)
        await state.update_data(row=cb.r, today_msg=msg_id)
        await edit_or_send(
            bot, chat_id, msg_id, render.today_item_text(line), today_item_keyboard(cb.r)
        )

    elif cb.a == "clear":
        result = await service.correct_today(uid, cb.r, 0, master=call.from_user.full_name)
        await state.clear()
        await show_today(bot, chat_id, service, uid, edit_id=msg_id)
        await bot.send_message(chat_id, result)

    elif cb.a == "back":
        await state.clear()
        await show_today(bot, chat_id, service, uid, edit_id=msg_id)


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
    await guarded(call, bot, lambda: act(call, callback_data, state, bot, service))


async def on_today_act(
    call: CallbackQuery, callback_data: Today, state: FSMContext, bot: Bot, service: ReportService
) -> None:
    await guarded(call, bot, lambda: act_today(call, callback_data, state, bot, service))


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
