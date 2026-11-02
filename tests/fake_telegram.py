"""Поддельный Telegram для тестов бота.

`FakeSession` подставляется в `Bot` вместо сети и запоминает всё, что бот отправил, изменил
и удалил. `Chat` играет мастера: пишет сообщения и нажимает кнопки через настоящий
`Dispatcher.feed_raw_update`, то есть проходят настоящие фильтры, middleware, FSM и хэндлеры.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat as TgChat
from aiogram.types import InlineKeyboardMarkup, Message

Button = tuple[str, str]  # (подпись, callback_data или url)


def flatten(markup: InlineKeyboardMarkup | None) -> list[Button]:
    if markup is None:
        return []
    return [(b.text, b.callback_data or b.url or "") for row in markup.inline_keyboard for b in row]


@dataclass
class Sent:
    """Сообщение бота в чате: текст, кнопки, удалено ли."""

    id: int
    chat_id: int
    text: str
    buttons: list[Button] = field(default_factory=list)
    deleted: bool = False

    @property
    def labels(self) -> list[str]:
        return [t for t, _ in self.buttons]


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.messages: dict[int, Sent] = {}
        self.methods: list[TelegramMethod[Any]] = []
        self.alerts: list[tuple[str | None, bool | None]] = []
        self.edit_errors: set[int] = set()  # сообщения, которые нельзя изменить
        self._next_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        if False:
            yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self._next_id += 1
            chat_id = int(method.chat_id)
            self.messages[self._next_id] = Sent(
                self._next_id, chat_id, method.text, flatten(_markup(method.reply_markup))
            )
            return self._message(bot, self._next_id, chat_id, method.text)
        if isinstance(method, EditMessageText):
            if method.message_id in self.edit_errors:
                raise TelegramBadRequest(
                    method=method, message="Bad Request: message to edit not found"
                )
            rec = self.messages.setdefault(
                method.message_id, Sent(method.message_id, int(method.chat_id), "")
            )
            if rec.deleted:
                raise TelegramBadRequest(
                    method=method, message="Bad Request: message to edit not found"
                )
            rec.text, rec.buttons = method.text, flatten(_markup(method.reply_markup))
            return self._message(bot, method.message_id, rec.chat_id, method.text)
        if isinstance(method, EditMessageReplyMarkup):
            if rec := self.messages.get(method.message_id):
                rec.buttons = flatten(_markup(method.reply_markup))
            return True
        if isinstance(method, DeleteMessage):
            if rec := self.messages.get(method.message_id):
                rec.deleted = True
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.alerts.append((method.text, method.show_alert))
            return True
        return True  # SendChatAction, SetMyCommands …

    @staticmethod
    def _message(bot: Bot, message_id: int, chat_id: int, text: str) -> Message:
        return Message(
            message_id=message_id,
            date=datetime.now(),
            chat=TgChat(id=chat_id, type="private"),
            text=text,
        ).as_(bot)


def _markup(markup: Any) -> InlineKeyboardMarkup | None:
    return markup if isinstance(markup, InlineKeyboardMarkup) else None


class Chat:
    """Мастер в личном чате с ботом."""

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        session: FakeSession,
        user_id: int = 1,
        first_name: str = "Иван",
        last_name: str = "Петров",
    ) -> None:
        self.dp, self.bot, self.session, self.user_id = dp, bot, session, user_id
        self._user = {
            "id": user_id,
            "is_bot": False,
            "first_name": first_name,
            "last_name": last_name,
        }
        self._update_id = 0
        self._message_id = 0

    def _base_message(self) -> dict[str, Any]:
        self._message_id += 1
        return {
            "message_id": self._message_id,
            "date": 1_700_000_000,
            "chat": {"id": self.user_id, "type": "private"},
            "from": self._user,
        }

    async def _feed(self, **update: Any) -> None:
        self._update_id += 1
        await self.dp.feed_raw_update(self.bot, {"update_id": self._update_id, **update})

    # ── действия мастера ──

    async def say(self, text: str) -> None:
        msg = self._base_message() | {"text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self._feed(message=msg)

    async def send_voice(self) -> None:
        msg = self._base_message() | {
            "voice": {"file_id": "f", "file_unique_id": "u", "duration": 3}
        }
        await self._feed(message=msg)

    async def send_photo(self) -> None:
        msg = self._base_message() | {
            "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
        }
        await self._feed(message=msg)

    async def press(self, message_id: int, label: str) -> None:
        """Нажать кнопку, подпись которой начинается с label."""
        rec = self.session.messages[message_id]
        matches = [data for text, data in rec.buttons if text.startswith(label)]
        assert matches, f"нет кнопки «{label}» у сообщения {message_id}: {rec.labels}"
        await self.press_data(message_id, matches[0])

    async def press_data(self, message_id: int, data: str) -> None:
        """Нажать кнопку по её callback_data (в том числе устаревшей)."""
        rec = self.session.messages.get(message_id)
        await self._feed(
            callback_query={
                "id": str(self._update_id + 1),
                "from": self._user,
                "chat_instance": "ci",
                "message": {
                    "message_id": message_id,
                    "date": 1_700_000_000,
                    "chat": {"id": self.user_id, "type": "private"},
                    "text": rec.text if rec else "",
                },
                "data": data,
            }
        )

    # ── что видит мастер ──

    def visible(self) -> list[Sent]:
        return [
            m for m in self.session.messages.values() if m.chat_id == self.user_id and not m.deleted
        ]

    def texts(self) -> list[str]:
        return [m.text for m in self.visible()]

    def find(self, prefix: str) -> Sent:
        """Последнее видимое сообщение, начинающееся с prefix."""
        found = [m for m in self.visible() if m.text.startswith(prefix)]
        assert found, f"нет сообщения «{prefix}…» среди: {self.texts()}"
        return found[-1]

    def card(self) -> Sent:
        return self.find("Отчёт за")

    def button_data(self, message_id: int, label: str) -> str:
        return next(d for t, d in self.session.messages[message_id].buttons if t.startswith(label))
