"""Точка входа: python -m m100bot"""

import asyncio

from aiogram import Bot, Dispatcher
from loguru import logger

from m100bot.bot.handlers import router
from m100bot.config import get_settings


async def main() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(lambda m: print(m, end=""), level=settings.log_level)

    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    # TODO: собрать зависимости (SheetsClient, Matcher, ReportService, Storage) и передать
    # в dp["service"] = ...; запустить фоновую задачу разбора bitrix_outbox.
    logger.info("Бот запущен (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
