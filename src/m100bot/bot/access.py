"""Доступ только для ALLOWED_USER_IDS."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from loguru import logger


class AccessMiddleware(BaseMiddleware):
    """Пускает только пользователей из списка. Пустой список — доступ открыт всем
    (так написано в .env.example; при запуске в лог идёт предупреждение)."""

    def __init__(self, allowed_user_ids: Iterable[int]) -> None:
        self.allowed = frozenset(allowed_user_ids)

    @property
    def open_to_all(self) -> bool:
        return not self.allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if self.open_to_all or (user is not None and user.id in self.allowed):
            return await handler(event, data)

        user_id = user.id if user is not None else None
        logger.warning("Доступ закрыт для пользователя {}", user_id)
        text = (
            f"Доступ закрыт. Передайте администратору ваш Telegram ID: {user_id}."
            if user_id is not None
            else "Доступ закрыт."
        )
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return None
