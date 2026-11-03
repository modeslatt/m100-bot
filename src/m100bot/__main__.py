"""Точка входа: python -m m100bot"""

import asyncio
import sys
from contextlib import suppress

from aiogram import Bot
from aiogram.types import BotCommand
from loguru import logger

from m100bot.bitrix.client import BitrixClient
from m100bot.bot.app import build_dispatcher
from m100bot.config import Settings, get_settings
from m100bot.matching.llm import LlmExtractor
from m100bot.matching.matcher import Matcher
from m100bot.services.report import ReportError, ReportService, run_outbox_worker
from m100bot.sheets.client import SheetsClient
from m100bot.storage.db import Storage
from m100bot.voice.transcribe import Transcriber

COMMANDS = [
    BotCommand(command="today", description="Что записано сегодня"),
    BotCommand(command="table", description="Ссылка на таблицу"),
    BotCommand(command="help", description="Как пользоваться"),
]
OUTBOX_INTERVAL_S = 60.0  # как часто разбирать очередь Битрикса


def build_service(settings: Settings, storage: Storage) -> ReportService:
    """Собрать зависимости. Секреты читаются только из окружения (.env)."""
    sheets = SheetsClient.from_settings(settings)
    matcher = Matcher(LlmExtractor.from_settings(settings))
    bitrix = BitrixClient.from_settings(settings)
    if bitrix is None:
        logger.info("Битрикс24 не настроен — отправка отключена")
    return ReportService(sheets, storage, matcher, bitrix, timezone=settings.timezone)


async def main() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)

    storage = Storage(settings.db_path)
    await storage.init()
    service = build_service(settings, storage)
    try:
        catalog = await service.catalog()
        logger.info("Таблица доступна: {} позиций", len(catalog.positions))
    except ReportError as e:  # бот всё равно запускаем: таблица может ожить позже
        logger.error("Таблица недоступна при запуске: {}", e)

    bot = Bot(settings.bot_token)
    transcriber = Transcriber.from_settings(settings)
    if transcriber is None:
        logger.info("Голосовые не настроены (STT_API_KEY пуст) — бот просит присылать текст")
    dp = build_dispatcher(service, settings.allowed_user_ids, transcriber)
    worker = (
        asyncio.create_task(run_outbox_worker(service, OUTBOX_INTERVAL_S))
        if service.bitrix is not None
        else None
    )
    try:
        await bot.set_my_commands(COMMANDS)
        logger.info("Бот запущен (long polling)")
        await dp.start_polling(bot)
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await service.drain()  # фоновое сохранение синонимов
        if isinstance(service.bitrix, BitrixClient):
            await service.bitrix.aclose()
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
