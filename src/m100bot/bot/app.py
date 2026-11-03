"""Сборка диспетчера aiogram: доступ, роутер, сервис."""

from collections.abc import Iterable

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from m100bot.bot.access import AccessMiddleware
from m100bot.bot.handlers import build_router
from m100bot.services.report import ReportService
from m100bot.voice.transcribe import Transcriber


def build_dispatcher(
    service: ReportService,
    allowed_user_ids: Iterable[int],
    transcriber: Transcriber | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    access = AccessMiddleware(allowed_user_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_router())
    dp["service"] = service  # хэндлеры получают сервис параметром `service`
    dp["transcriber"] = transcriber  # None — голосовые не настроены
    if access.open_to_all:
        logger.warning("ALLOWED_USER_IDS пуст — бот отвечает всем, кто ему напишет")
    return dp
