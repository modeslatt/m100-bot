"""Transcriber без сети: поддельный клиент вместо Groq."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError

from m100bot.config import Settings
from m100bot.voice.transcribe import PROMPT, TranscribeError, Transcriber

REQUEST = httpx.Request("POST", "https://api.groq.test/openai/v1/audio/transcriptions")


class FakeTranscriptions:
    """Отвечает по очереди: текст (str) или ошибка (Exception)."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(text=answer)


def make(*answers: str | Exception) -> tuple[Transcriber, FakeTranscriptions]:
    api = FakeTranscriptions(*answers)
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=api))
    t = Transcriber("https://x", "key", "whisper-large-v3-turbo", retry_wait=0, client=client)  # type: ignore[arg-type]
    return t, api


async def test_transcribe_sends_audio_with_russian_and_prompt() -> None:
    t, api = make("  Проложили медь 15 м  ")
    assert await t.transcribe(b"ogg") == "Проложили медь 15 м"
    [call] = api.calls
    assert call["file"] == ("voice.ogg", b"ogg")
    assert (call["model"], call["language"], call["temperature"]) == (
        "whisper-large-v3-turbo",
        "ru",
        0,
    )
    assert call["prompt"] == PROMPT


async def test_timeout_is_retried_once() -> None:
    t, api = make(APITimeoutError(REQUEST), "ок")
    assert await t.transcribe(b"ogg") == "ок"
    assert len(api.calls) == 2


async def test_persistent_timeout_becomes_message_for_master() -> None:
    t, _ = make(APITimeoutError(REQUEST), APITimeoutError(REQUEST))
    with pytest.raises(TranscribeError, match="не отвечает"):
        await t.transcribe(b"ogg")


async def test_bad_key_is_not_retried() -> None:
    error = AuthenticationError(
        "invalid key", response=httpx.Response(401, request=REQUEST), body=None
    )
    t, api = make(error, "не дойдёт")
    with pytest.raises(TranscribeError, match="Пришлите отчёт текстом"):
        await t.transcribe(b"ogg")
    assert len(api.calls) == 1


def test_no_key_means_no_voice() -> None:
    base = {"bot_token": "x", "spreadsheet_id": "y", "_env_file": None}
    assert Transcriber.from_settings(Settings(**base, stt_api_key="")) is None  # type: ignore[arg-type]
    assert isinstance(
        Transcriber.from_settings(Settings(**base, stt_api_key="k")),  # type: ignore[arg-type]
        Transcriber,
    )
