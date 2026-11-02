"""Хэндлеры aiogram. Тонкие: вся логика — в services/report.py."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router()

HELP = (
    "Пришлите отчёт за день текстом или голосом, например:\n"
    "«Проложили медь 6-ку 15 м и 9-ку 15 м, изоляцию столько же, кабель 3х2,5 — 56 м».\n\n"
    "Я покажу, что распознал, и запишу в таблицу только после подтверждения.\n"
    "/today — что записано сегодня и исправление\n"
    "/table — ссылка на таблицу"
)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Здравствуйте! " + HELP)


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(HELP)


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    # TODO: скачать файл → Transcriber → показать распознанный текст → тот же путь, что и текст
    await message.answer("Голос пока не поддерживается (TODO).")


@router.message(F.text)
async def on_text(message: Message) -> None:
    # TODO: вопрос «сколько осталось…» → ReportService.remaining
    # иначе ReportService.recognize → карточка + review_keyboard, состояние ReportFlow.reviewing
    await message.answer("Распознавание пока не подключено (TODO).")
