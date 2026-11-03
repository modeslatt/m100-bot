"""Бот целиком: настоящие хэндлеры, доступ, FSM и ReportService.

Подделаны только Telegram (tests/fake_telegram.py), таблица, Битрикс и LLM-матчер.
Тексты в проверках — те, что мастер увидит в чате (макеты согласованы).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from fake_telegram import Chat, FakeSession
from test_report_service import FakeBitrix, FakeMatcher, FakeSheets, ok

from m100bot.bot.app import build_dispatcher
from m100bot.matching.llm import LlmError
from m100bot.matching.models import MatchedItem, MatchStatus
from m100bot.services.report import ReportService
from m100bot.sheets.catalog import Catalog
from m100bot.sheets.client import SheetsError
from m100bot.storage.db import Storage
from m100bot.voice.transcribe import TranscribeError

D = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 9, tzinfo=UTC)  # 12:00 по Минску
HTTPS = "https://sheet.test/edit#range=B"


def amb(phrase: str, qty: float, candidates: list[int], unit: str = "шт") -> MatchedItem:
    return MatchedItem(
        phrase=phrase,
        qty=qty,
        unit=unit,
        status=MatchStatus.AMBIGUOUS,
        candidates=candidates,
    )


def clarify_qty(row: int, qty: float | None = None, unit: str | None = None) -> MatchedItem:
    return MatchedItem(
        phrase=f"позиция {row}", qty=qty, unit=unit, status=MatchStatus.CLARIFY, row=row
    )


def missing(phrase: str) -> MatchedItem:
    return MatchedItem(phrase=phrase, qty=20, unit="м2", status=MatchStatus.NOT_FOUND)


@dataclass
class Env:
    chat: Chat
    dp: Dispatcher
    bot: Bot
    session: FakeSession
    sheets: FakeSheets
    matcher: FakeMatcher
    bitrix: FakeBitrix
    service: ReportService
    storage: Storage
    transcriber: "FakeTranscriber | None" = None


class FakeTranscriber:
    """Whisper без сети: возвращает заданный текст или бросает заданную ошибку."""

    def __init__(self) -> None:
        self.text = ""
        self.error: Exception | None = None
        self.audio: list[bytes] = []

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        self.audio.append(audio)
        if self.error:
            raise self.error
        return self.text


async def make_env(
    tmp_path: Path,
    catalog: Catalog,
    allowed: tuple[int, ...] = (),
    transcriber: FakeTranscriber | None = None,
) -> Env:
    # черновики «создаются» в тот же день, что и сегодняшняя дата бота
    storage = Storage(tmp_path / "bot.sqlite3", now=lambda: NOW)
    await storage.init()
    sheets, matcher, bitrix = FakeSheets(catalog), FakeMatcher(), FakeBitrix()
    service = ReportService(sheets, storage, matcher, bitrix, today=lambda: D)
    dp = build_dispatcher(service, allowed, transcriber)  # type: ignore[arg-type]
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    chat = Chat(dp, bot, session)
    return Env(chat, dp, bot, session, sheets, matcher, bitrix, service, storage, transcriber)


@pytest.fixture
async def env(tmp_path: Path, catalog: Catalog) -> AsyncIterator[Env]:
    e = await make_env(tmp_path, catalog)
    yield e
    await e.service.drain()
    await e.storage.close()


async def report(env: Env, *items: MatchedItem, text: str = "отчёт мастера"):
    """Мастер присылает отчёт; матчер «распознаёт» заданные позиции. Возвращает карточку."""
    env.matcher.items = list(items)
    await env.chat.say(text)
    return env.chat.card()


# ─────────────── Карточка ───────────────


async def test_text_becomes_card(env: Env) -> None:
    card = await report(env, ok(17, 15), ok(18, 15))

    assert card.text.startswith("Отчёт за 02.11.2026. Распознано 2 позиции.")
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in card.text
    assert "2. ✅ Фреонопровод медный, Д9,52 TEC TUBE cips — 15 м" in card.text
    assert card.labels == ["✅ Записать", "✏️ Исправить", "➕ Добавить", "✖️ Отмена"]
    assert len(env.chat.visible()) == 1  # «Разбираю отчёт…» превратилось в карточку


async def test_over_remaining_warning(env: Env) -> None:
    env.sheets.done = {55: 170.0}  # по смете 182 м — осталось 12
    card = await report(env, ok(55, 56))
    assert (
        "1. ⚠️ Кабель силовой в ПВХ изоляции и оболочке, негорючий, 3х2,5 — 56 м\n"
        "   превышает остаток (осталось 12 м)"
    ) in card.text
    assert "✅ Записать" in card.labels  # предупреждение запись не блокирует


async def test_nothing_recognized_keeps_old_draft(env: Env) -> None:
    card = await report(env, ok(17, 15))
    env.matcher.items = []
    await env.chat.say("привет")

    assert env.chat.find("Не нашёл в сообщении работ")
    assert card.labels[0] == "✅ Записать"  # прежняя карточка жива
    assert await env.storage.get_draft(1) is not None


async def test_recognition_error_is_shown(env: Env) -> None:
    class Boom(FakeMatcher):
        async def understand(self, text, catalog, synonyms=None):
            raise LlmError("Сервис распознавания сейчас недоступен. Попробуйте позже.")

    env.service.matcher = Boom()
    await env.chat.say("медь")
    assert env.chat.texts() == ["Сервис распознавания сейчас недоступен. Попробуйте позже."]
    assert await env.storage.get_draft(1) is None


async def test_long_report_fits_telegram_limit(env: Env) -> None:
    card = await report(env, *[ok(17, 15) for _ in range(80)])
    assert len(card.text) <= 4096
    assert "…и ещё" in card.text
    assert card.labels[0] == "✅ Записать"


# ─────────────── Запись ───────────────


async def test_confirm_writes_and_shows_summary_with_link(env: Env) -> None:
    card = await report(env, ok(17, 15), ok(18, 15))
    await env.chat.press(card.id, "✅ Записать")

    assert card.text.startswith("✅ Записано в таблицу за 02.11.2026:")
    assert "• Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in card.text
    assert card.labels == ["📊 Открыть таблицу"]
    assert card.buttons[0][1] == HTTPS + "17"  # ссылка на первую записанную строку
    assert env.sheets.writes == [(D, {17: 15.0, 18: 15.0}, "add")]
    assert await env.storage.get_draft(1) is None
    assert env.bitrix.calls == []  # Битрикс — только через outbox
    assert len(await env.storage.pending_outbox()) == 1


async def test_master_name_goes_to_bitrix_snapshot(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✅ Записать")
    [queued] = await env.storage.pending_outbox()
    assert queued.payload["master"] == "Иван Петров"


async def test_pressing_confirm_twice_writes_once(env: Env) -> None:
    card = await report(env, ok(17, 15))
    data = env.chat.button_data(card.id, "✅ Записать")
    await env.chat.press_data(card.id, data)
    await env.chat.press_data(card.id, data)  # повторное нажатие
    assert env.chat.find("Нет отчёта для записи")
    assert env.sheets.cells[(D, 17)] == 15 and len(env.sheets.writes) == 1


async def test_sheets_failure_keeps_card_and_retry_works(env: Env) -> None:
    env.sheets.write_errors = [SheetsError("Таблица недоступна. Попробуйте позже.")]
    card = await report(env, ok(17, 15))

    await env.chat.press(card.id, "✅ Записать")

    assert env.chat.find("⚠️").text == (
        "⚠️ Таблица недоступна. Попробуйте позже. "
        "Отчёт сохранён — нажмите «Записать» ещё раз чуть позже."
    )
    assert card.text.startswith("Отчёт за") and "✅ Записать" in card.labels  # карточка цела
    assert await env.storage.get_draft(1) is not None

    await env.chat.press(card.id, "✅ Записать")
    assert card.text.startswith("✅ Записано")
    assert env.sheets.cells[(D, 17)] == 15


async def test_stale_card_button_writes_nothing(env: Env) -> None:
    """Кнопка со старой карточки (например, после перезапуска) не должна ничего записать."""
    old = await report(env, ok(17, 15), text="первый")
    old_data = env.chat.button_data(old.id, "✅ Записать")
    new = await report(env, ok(18, 7), text="второй")

    assert new.id != old.id and old.buttons == []  # у старой кнопки убраны
    await env.chat.press_data(old.id, old_data)

    assert env.session.alerts[-1] == ("Эта карточка устарела. Пришлите отчёт заново.", True)
    assert env.sheets.writes == [] and await env.storage.get_draft(1) is not None


async def test_buttons_survive_bot_restart(env: Env) -> None:
    """FSM в памяти и пропадает при перезапуске, а черновик — в базе: кнопки продолжают работать."""
    card = await report(env, ok(17, 15))
    data = env.chat.button_data(card.id, "✅ Записать")
    restarted = Chat(build_dispatcher(env.service, ()), env.bot, env.session)

    await restarted.press_data(card.id, data)

    assert card.text.startswith("✅ Записано")
    assert env.sheets.cells[(D, 17)] == 15


# ─────────────── Уточнения по одной позиции ───────────────


async def test_ambiguous_items_are_asked_one_at_a_time(env: Env) -> None:
    card = await report(
        env, ok(17, 15), amb("12 хомутов", 12, [43, 44]), amb("насверлили 4 отверстия", 4, [48, 49])
    )

    assert "Распознано 3 позиции, 2 нужно уточнить." in card.text
    assert "2. ❓ «12 хомутов» — какой вариант?" in card.text
    assert card.labels[0] == "✅ Записать готовое (1)"
    questions = [m for m in env.chat.visible() if m.text.startswith("Позиция")]
    assert len(questions) == 1  # вопрос только один
    q1 = questions[0]
    assert q1.text == (
        "Позиция 2: «12 хомутов». Какой вариант?\n\n"
        "1. Хомут для крепления дренажа М8 25мм\n"
        "2. Хомут для крепления фреонопровода М8 32 мм"
    )
    assert q1.labels == ["1", "2", "Нет нужного"]  # полные названия — в тексте

    await env.chat.press(q1.id, "2")

    assert q1.deleted
    assert "2. ✅ Хомут для крепления фреонопровода М8 32 мм — 12 шт" in card.text
    q2 = env.chat.find("Позиция 3:")
    # у строк 48 и 49 одинаковые названия: различаются № и количеством по смете
    assert "1. сверление отверстий Д80 L=0,5м · № 44, по смете 18 шт" in q2.text
    assert "2. сверление отверстий Д80 L=0,5м · № 45, по смете 26 шт" in q2.text

    await env.chat.press(q2.id, "Нет нужного")

    assert "3. ❓ «насверлили 4 отверстия» — нет в смете, не записываю" in card.text
    assert card.labels[0] == "✅ Записать"  # вопросов не осталось
    assert not [m for m in env.chat.visible() if m.text.startswith("Позиция")]


async def test_picked_variant_is_learned_as_synonym_only_after_writing(env: Env) -> None:
    card = await report(env, amb("12 хомутов", 12, [43, 44]))
    q = env.chat.find("Позиция 1:")
    await env.chat.press(q.id, "2")  # хомут для фреонопровода
    await env.service.drain()
    assert env.sheets.added_synonyms == []  # пока не записано — не запоминаем

    await env.chat.press(card.id, "✅ Записать")
    await env.service.drain()

    assert env.sheets.added_synonyms == [("хомутов", 44)]  # без объёма, молча
    assert not [t for t in env.chat.texts() if "синоним" in t.lower() or "Запомн" in t]


async def test_rejected_variant_is_not_learned_and_reported(env: Env) -> None:
    card = await report(env, ok(17, 15), amb("хомуты", 12, [43, 44]))
    await env.chat.press(env.chat.find("Позиция 2:").id, "Нет нужного")
    await env.chat.press(card.id, "✅ Записать")
    await env.service.drain()

    assert "Нет в смете: «хомуты»" in card.text
    assert env.sheets.added_synonyms == []
    assert await env.storage.get_draft(1) is None  # «нет в смете» черновик не держит


async def test_not_found_item_is_shown_and_does_not_block(env: Env) -> None:
    card = await report(env, ok(17, 15), missing("покрасили стену 20 м2"))
    assert "2. ❓ «покрасили стену 20 м2» — нет в смете, не записываю" in card.text
    assert card.labels[0] == "✅ Записать"
    assert not [m for m in env.chat.visible() if m.text.startswith("Позиция")]

    await env.chat.press(card.id, "✅ Записать")
    assert "Нет в смете: «покрасили стену 20 м2»" in card.text


async def test_confirm_with_unresolved_keeps_them_and_asks_again(env: Env) -> None:
    card = await report(env, ok(17, 15), amb("12 хомутов", 12, [43, 44]))
    await env.chat.press(card.id, "✅ Записать готовое")

    assert card.text.startswith("✅ Записано в таблицу")
    assert "❓ Не записано, нужно уточнить: «12 хомутов»" in card.text
    new_card = env.chat.card()
    assert new_card.id != card.id
    assert "Распознано 1 позиция, 1 нужно уточнить." in new_card.text
    assert "1. Хомут для крепления дренажа" in env.chat.find("Позиция 1:").text
    draft = await env.storage.get_draft(1)
    assert draft is not None and [i.phrase for i in draft.items] == ["12 хомутов"]


async def test_missing_quantity_is_asked_and_answered(env: Env) -> None:
    card = await report(env, clarify_qty(17))
    assert "1. ❓ Фреонопровод медный, Д6,35 TEC TUBE cips — сколько?" in card.text
    assert card.labels == ["✏️ Исправить", "➕ Добавить", "✖️ Отмена"]  # записывать пока нечего
    q = env.chat.find("Позиция 1:")
    assert q.text == (
        "Позиция 1: Фреонопровод медный, Д6,35 TEC TUBE cips. Сколько метров?\n"
        "Ответьте числом, например: 15"
    )
    assert q.labels == ["Удалить позицию"]

    await env.chat.say("много")
    assert env.chat.find("Не понял число")
    await env.chat.say("0")
    assert env.chat.find("Объём должен быть числом больше нуля")

    await env.chat.say("15 м")

    assert q.deleted
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in card.text
    assert card.labels[0] == "✅ Записать"


async def test_other_units_question(env: Env) -> None:
    card = await report(env, clarify_qty(67, qty=6, unit="м"))
    assert "1. ❓ Короб ПВХ размером 25х16мм, длиной 2м — в смете «шт», вы указали 6 м" in card.text
    q = env.chat.find("Позиция 1:")
    assert q.text.startswith(
        "Позиция 1: Короб ПВХ размером 25х16мм, длиной 2м. "
        "В смете он в «шт», вы указали «м». Сколько штук?"
    )
    await env.chat.say("3")
    assert "1. ✅ Короб ПВХ размером 25х16мм, длиной 2м — 3 шт" in card.text


async def test_long_text_instead_of_number_is_added_to_report(env: Env) -> None:
    """Бот ждёт «сколько?», а мастер присылает следующую работу: дописываем, не стираем."""
    old_card = await report(env, clarify_qty(17))
    env.matcher.items = [ok(18, 7)]
    await env.chat.say("проложили девятку семь метров")
    card = env.chat.card()
    assert "1. ❓ Фреонопровод медный, Д6,35 TEC TUBE cips — сколько?" in card.text
    assert "2. ✅ Фреонопровод медный, Д9,52 TEC TUBE cips — 7 м" in card.text
    assert old_card.buttons == []  # у прежней карточки кнопки убраны
    # вопрос по первой позиции задан заново и по-прежнему ждёт число
    assert env.chat.find("Позиция 1:")
    await env.chat.say("15")
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in card.text


async def test_messages_one_by_one_are_collected_in_one_report(env: Env) -> None:
    """Мастер шлёт работы по одной без кнопки «Добавить» — ни одна не теряется."""
    await report(env, ok(17, 15))
    for item in (ok(18, 10), amb("хомуты", 12, [43, 44]), ok(64, 40), ok(62, 1)):
        env.matcher.items = [item]
        await env.chat.say("ещё работа")
    draft = await env.storage.get_draft(1)
    assert draft is not None
    assert [i.row or i.candidates for i in draft.items] == [17, 18, [43, 44], 64, 62]
    assert "Распознано 5 позиций, 1 нужно уточнить." in env.chat.card().text


async def test_yesterdays_draft_is_not_mixed_in_silently(env: Env) -> None:
    await report(env, ok(17, 15))
    env.service._today = lambda: date(2026, 11, 3)  # наступил следующий день
    env.matcher.items = [ok(64, 40)]
    await env.chat.say("гофра 20 — 40 м")

    q = env.chat.find("Есть незаписанный отчёт за 02.11.2026 (1 позиция).")
    assert q.labels == ["➕ Добавить к нему", "🆕 Новый отчёт"]
    assert env.matcher.seen[-1][0] != "гофра 20 — 40 м"  # пока не распознавали

    await env.chat.press(q.id, "🆕 Новый отчёт")
    draft = await env.storage.get_draft(1)
    assert draft is not None and [i.row for i in draft.items] == [64]
    assert q.text.startswith("Отчёт за 03.11.2026. Распознано 1 позиция.")


async def test_yesterdays_draft_can_be_continued(env: Env) -> None:
    await report(env, ok(17, 15))
    env.service._today = lambda: date(2026, 11, 3)
    env.matcher.items = [ok(64, 40)]
    await env.chat.say("гофра 20 — 40 м")
    await env.chat.press(env.chat.find("Есть незаписанный отчёт").id, "➕ Добавить к нему")
    draft = await env.storage.get_draft(1)
    assert draft is not None and [i.row for i in draft.items] == [17, 64]


# ─────────────── Исправить / Добавить / Отмена ───────────────


async def test_edit_item_quantity(env: Env) -> None:
    card = await report(env, ok(17, 15), ok(18, 20))

    await env.chat.press(card.id, "✏️ Исправить")
    assert card.text == "Какую позицию исправить?"
    assert card.labels == ["1", "2", "Назад"]

    await env.chat.press(card.id, "2")
    assert card.text == (
        "Позиция 2: Фреонопровод медный, Д9,52 TEC TUBE cips — 20 м.\n"
        "Пришлите новый объём числом (м) или удалите позицию."
    )
    assert card.labels == ["Удалить", "Назад"]

    await env.chat.say("12,5")

    assert "2. ✅ Фреонопровод медный, Д9,52 TEC TUBE cips — 12,5 м" in card.text
    assert card.labels[0] == "✅ Записать"
    assert env.sheets.writes == []  # исправление карточки ничего не пишет


async def test_edit_delete_item_and_renumber(env: Env) -> None:
    card = await report(env, ok(17, 15), ok(18, 20))
    await env.chat.press(card.id, "✏️ Исправить")
    await env.chat.press(card.id, "1")
    await env.chat.press(card.id, "Удалить")

    assert "Распознано 1 позиция." in card.text
    assert "1. ✅ Фреонопровод медный, Д9,52 TEC TUBE cips — 20 м" in card.text


async def test_deleting_last_item_clears_report(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✏️ Исправить")
    await env.chat.press(card.id, "1")
    await env.chat.press(card.id, "Удалить")

    assert card.text == "Отчёт пуст. Ничего не записано."
    assert await env.storage.get_draft(1) is None


async def test_delete_from_quantity_question(env: Env) -> None:
    card = await report(env, ok(17, 15), clarify_qty(18))
    q = env.chat.find("Позиция 2:")
    await env.chat.press(q.id, "Удалить позицию")
    assert q.deleted
    assert "Распознано 1 позиция." in card.text
    assert not [m for m in env.chat.visible() if m.text.startswith("Позиция")]


async def test_edit_back_returns_to_card(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✏️ Исправить")
    await env.chat.press(card.id, "Назад")
    assert card.text.startswith("Отчёт за") and "✅ Записать" in card.labels


async def test_editing_ambiguous_item_reopens_its_question(env: Env) -> None:
    card = await report(env, ok(17, 15), amb("12 хомутов", 12, [43, 44]))
    first_question = env.chat.find("Позиция 2:")
    await env.chat.press(card.id, "✏️ Исправить")
    await env.chat.press(card.id, "2")
    assert card.text.startswith("Отчёт за")  # вернулись к карточке
    assert first_question.deleted
    assert env.chat.find("Позиция 2:").labels[-1] == "Нет нужного"


async def test_add_more_items(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "➕ Добавить")
    assert card.text == "Пришлите ещё позиции текстом, я добавлю их к отчёту."
    assert card.labels == ["Назад"]

    env.matcher.items = []
    await env.chat.say("ничего нового")
    assert env.chat.find("Не нашёл новых работ")

    env.matcher.items = [ok(44, 10)]
    await env.chat.say("хомутов 10")

    assert "Распознано 2 позиции." in card.text
    assert "2. ✅ Хомут для крепления фреонопровода М8 32 мм — 10 шт" in card.text
    assert card.labels[0] == "✅ Записать"


async def test_cancel(env: Env) -> None:
    card = await report(env, ok(17, 15), amb("12 хомутов", 12, [43, 44]))
    question = env.chat.find("Позиция 2:")
    await env.chat.press(card.id, "✖️ Отмена")

    assert card.text == "Отчёт отменён. Ничего не записано." and card.buttons == []
    assert question.deleted
    assert await env.storage.get_draft(1) is None and env.sheets.writes == []


async def test_card_is_resent_when_it_cannot_be_edited(env: Env) -> None:
    card = await report(env, ok(17, 15))
    env.session.edit_errors.add(card.id)  # сообщение удалили или оно слишком старое

    await env.chat.press(card.id, "✏️ Исправить")

    pick = env.chat.find("Какую позицию исправить?")
    assert pick.id != card.id  # бот отправил новое, а не упал


# ─────────────── /today ───────────────


async def test_today_shows_written_and_can_correct(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✅ Записать")

    await env.chat.say("/today")
    today = env.chat.find("Сегодня, 02.11.2026, записано:")
    assert today.text == (
        "Сегодня, 02.11.2026, записано:\n\n1. Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м"
    )
    assert today.labels == ["✏️ Исправить", "📊 Открыть таблицу"]

    await env.chat.press(today.id, "✏️ Исправить")
    assert today.text == "Какую позицию исправить?" and today.labels == ["1", "Назад"]
    await env.chat.press(today.id, "1")
    assert today.text == (
        "Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м.\n"
        "Пришлите новый объём числом (м) или очистите."
    )
    assert today.labels == ["Очистить", "Назад"]

    await env.chat.say("12")

    assert today.text.endswith("— 12 м")
    assert env.chat.find("✅ Исправлено за 02.11.2026:").text == (
        "✅ Исправлено за 02.11.2026: Фреонопровод медный, Д6,35 TEC TUBE cips — "
        "было 15, стало 12 м"
    )
    assert env.sheets.cells[(D, 17)] == 12
    modes = [w.mode for w in await env.storage.get_writes(1, D)]
    assert modes == ["add", "set"]


async def test_today_clear_row(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✅ Записать")
    await env.chat.say("/today")
    today = env.chat.find("Сегодня")
    await env.chat.press(today.id, "✏️ Исправить")
    await env.chat.press(today.id, "1")
    await env.chat.press(today.id, "Очистить")

    assert today.text == "Сегодня, 02.11.2026, ничего не записано."
    assert env.chat.find("✅ Очищено за 02.11.2026:").text.endswith("(было 15 м)")
    assert env.sheets.cells[(D, 17)] == 0


async def test_today_when_nothing_written(env: Env) -> None:
    await env.chat.say("/today")
    today = env.chat.find("Сегодня")
    assert today.text == "Сегодня, 02.11.2026, ничего не записано."
    assert today.labels == ["📊 Открыть таблицу"]


async def test_today_back_button(env: Env) -> None:
    card = await report(env, ok(17, 15))
    await env.chat.press(card.id, "✅ Записать")
    await env.chat.say("/today")
    today = env.chat.find("Сегодня")
    await env.chat.press(today.id, "✏️ Исправить")
    await env.chat.press(today.id, "Назад")
    assert today.text.startswith("Сегодня, 02.11.2026, записано:")


# ─────────────── Команды и прочее ───────────────


async def test_start_help_table(env: Env) -> None:
    await env.chat.say("/start")
    assert env.chat.find("Здравствуйте!").text.endswith("/table — ссылка на таблицу")
    await env.chat.say("/help")
    assert env.chat.find("Пришлите отчёт за день текстом")
    await env.chat.say("/table")
    table = env.chat.find("Таблица с объёмами.")
    assert table.labels == ["📊 Открыть таблицу"] and table.buttons[0][1].startswith(HTTPS)


async def test_command_leaves_input_mode(env: Env) -> None:
    await report(env, clarify_qty(17))  # бот ждёт число
    await env.chat.say("/help")
    env.matcher.items = [ok(18, 7)]
    await env.chat.say("15")  # теперь это уже новый отчёт, а не ответ на вопрос
    assert env.matcher.seen[-1][0] == "15"


async def test_unknown_command_voice_and_other_content(env: Env) -> None:
    await env.chat.say("/foo")
    assert env.chat.find("Не знаю такой команды. Список: /help")
    await env.chat.send_voice()
    assert env.chat.find("Голосовые не настроены. Пришлите отчёт текстом.")
    await env.chat.send_photo()
    assert env.chat.find("Пришлите отчёт текстом.")


# ─────────────── Доступ ───────────────


async def test_stranger_is_refused(tmp_path: Path, catalog: Catalog) -> None:
    e = await make_env(tmp_path, catalog, allowed=(5,))
    try:
        stranger = Chat(e.dp, e.bot, e.session, user_id=7)
        e.matcher.items = [ok(17, 15)]
        await stranger.say("медь 6-ка 15 м")
        assert stranger.texts() == ["Доступ закрыт. Передайте администратору ваш Telegram ID: 7."]
        assert e.matcher.seen == [] and await e.storage.get_draft(7) is None

        member = Chat(e.dp, e.bot, e.session, user_id=5)
        await member.say("медь 6-ка 15 м")
        assert member.card().text.startswith("Отчёт за")
    finally:
        await e.storage.close()


async def test_stranger_cannot_press_buttons(tmp_path: Path, catalog: Catalog) -> None:
    e = await make_env(tmp_path, catalog, allowed=(5,))
    try:
        member = Chat(e.dp, e.bot, e.session, user_id=5)
        e.matcher.items = [ok(17, 15)]
        await member.say("медь")
        card = member.card()
        data = member.button_data(card.id, "✅ Записать")

        stranger = Chat(e.dp, e.bot, e.session, user_id=7)
        await stranger.press_data(card.id, data)

        assert e.session.alerts[-1][1] is True and "Доступ закрыт" in (
            e.session.alerts[-1][0] or ""
        )
        assert e.sheets.writes == []
    finally:
        await e.storage.close()


async def test_empty_allowed_list_means_open_for_everyone(env: Env) -> None:
    other = Chat(env.dp, env.bot, env.session, user_id=99)
    env.matcher.items = [ok(17, 15)]
    await other.say("медь")
    assert other.card().text.startswith("Отчёт за")


# ─────────────── Голос ───────────────


@pytest.fixture
async def voice(tmp_path: Path, catalog: Catalog) -> AsyncIterator[Env]:
    e = await make_env(tmp_path, catalog, transcriber=FakeTranscriber())
    yield e
    await e.service.drain()
    await e.storage.close()


async def test_voice_report_shows_transcript_then_card(voice: Env) -> None:
    assert voice.transcriber is not None
    voice.transcriber.text = "Проложили медь шестёрку 15 метров"
    voice.matcher.items = [ok(17, 15)]

    await voice.chat.send_voice()

    assert voice.transcriber.audio == [voice.session.file_bytes]  # скачан файл из Telegram
    assert voice.chat.find("Распознал: «Проложили медь шестёрку 15 метров»")
    assert voice.matcher.seen[-1][0] == "Проложили медь шестёрку 15 метров"
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in voice.chat.card().text


async def test_voice_answers_how_many(voice: Env) -> None:
    """На вопрос «сколько?» можно ответить голосом, и числом словами тоже."""
    assert voice.transcriber is not None
    await report(voice, clarify_qty(17))
    voice.transcriber.text = "Пятнадцать метров."

    await voice.chat.send_voice()

    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in voice.chat.card().text


async def test_voice_adds_to_todays_report(voice: Env) -> None:
    assert voice.transcriber is not None
    await report(voice, ok(17, 15))
    voice.transcriber.text = "и гофру двадцатку сорок метров"
    voice.matcher.items = [ok(64, 40)]

    await voice.chat.send_voice()

    draft = await voice.storage.get_draft(1)
    assert draft is not None and [i.row for i in draft.items] == [17, 64]


async def test_too_long_voice_is_refused(voice: Env) -> None:
    assert voice.transcriber is not None
    await voice.chat.send_voice(duration=301)
    assert voice.chat.find("Голосовое длиннее 5 минут.")
    assert voice.transcriber.audio == []  # даже не скачивали


async def test_transcription_error_is_shown(voice: Env) -> None:
    assert voice.transcriber is not None
    voice.transcriber.error = TranscribeError("Сервис распознавания голоса не отвечает.")
    await voice.chat.send_voice()
    assert voice.chat.texts() == ["Сервис распознавания голоса не отвечает."]
    assert voice.matcher.seen == []


async def test_silence_is_not_a_report(voice: Env) -> None:
    await voice.chat.send_voice()  # Whisper вернул пустой текст
    assert voice.chat.texts() == ["Не расслышал. Повторите голосовое или пришлите текстом."]
    assert voice.matcher.seen == []


# ─────────────── «Сколько осталось по …» ───────────────


async def ask_left(env: Env, *items: MatchedItem, text: str = "сколько осталось?") -> None:
    env.matcher.intent = "question"
    env.matcher.items = list(items)
    await env.chat.say(text)
    env.matcher.intent = "report"


async def test_question_is_answered_with_plan_done_left(env: Env) -> None:
    env.sheets.done = {55: 112.0}
    await ask_left(env, clarify_qty(55), text="сколько осталось по кабелю 3х2,5?")
    answer = env.chat.find("Кабель силовой")
    assert answer.text.endswith("По смете 182 м, выполнено 112 м, осталось 70 м.")
    assert answer.labels == ["📊 Открыть таблицу"]
    assert await env.storage.get_draft(1) is None  # вопрос — не отчёт


async def test_question_does_not_disturb_open_report(env: Env) -> None:
    card = await report(env, clarify_qty(17))  # бот ждёт «сколько?» по меди
    await ask_left(env, clarify_qty(55), text="а сколько осталось по кабелю 3х2,5?")
    assert env.chat.find("Кабель силовой")
    assert card.labels[0] == "✏️ Исправить"  # карточка жива, кнопки на месте
    await env.chat.say("15")  # режим «жду число» не сбит
    assert "1. ✅ Фреонопровод медный, Д6,35 TEC TUBE cips — 15 м" in card.text


async def test_ambiguous_question_asks_which_position(env: Env) -> None:
    env.sheets.done = {44: 300.0}
    await ask_left(env, amb("хомутам", 0, [43, 44]), text="остаток по хомутам")
    q = env.chat.find("«хомутам» — по какой позиции?")
    assert "2. Хомут для крепления фреонопровода М8 32 мм" in q.text
    assert q.labels == ["1", "2"]
    await env.chat.press(q.id, "2")
    assert q.text.endswith("По смете 527 шт, выполнено 300 шт, осталось 227 шт.")


async def test_question_without_position(env: Env) -> None:
    await ask_left(env, missing("сколько осталось?"))
    assert env.chat.find("Не нашёл позицию. Спросите точнее")


async def test_truncated_report_warns_master(env: Env) -> None:
    env.matcher.truncated = True
    await report(env, ok(17, 15))
    assert env.chat.find("⚠️ Сообщение длинное — проверьте, все ли работы попали")
