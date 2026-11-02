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
from m100bot.sheets.catalog import Catalog
from m100bot.sheets.client import SheetsClient
from m100bot.storage.db import Storage

COMMANDS = [
    BotCommand(command="today", description="Что записано сегодня"),
    BotCommand(command="table", description="Ссылка на таблицу"),
    BotCommand(command="help", description="Как пользоваться"),
]
OUTBOX_INTERVAL_S = 60.0  # как часто разбирать очередь Битрикса


def build_service(settings: Settings, storage: Storage) -> ReportService:
    """Собрать зависимости. Секреты читаются только из окружения (.env)."""
    sheets = SheetsClient.from_settings(settings)
    llm = LlmExtractor.from_settings(settings)
    # справочник матчер получит при первом отчёте (ReportService обновляет его сам)
    matcher = Matcher(Catalog(positions=[], date_columns={}, last_col=9), llm)
    bitrix = (
        BitrixClient(settings.bitrix_webhook_url, settings.bitrix_entity_type_id)
        if settings.bitrix_webhook_url
        else None
    )
    if bitrix is None:
        logger.info("Битрикс24 не настроен (BITRIX_WEBHOOK_URL пуст) — отправка отключена")
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
    dp = build_dispatcher(service, settings.allowed_user_ids)
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
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
