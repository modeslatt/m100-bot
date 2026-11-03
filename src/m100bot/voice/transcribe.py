"""Голосовое сообщение (OGG/Opus из Telegram) → текст через Whisper API (по умолчанию Groq).

У Groq API, совместимый с OpenAI, поэтому клиент — тот же `openai`, что и для LLM.
Ogg/Opus из Telegram API принимает как есть, конвертировать не нужно.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_fixed

if TYPE_CHECKING:
    from m100bot.config import Settings

# Whisper читает prompt как «начало разговора» и подстраивается под его стиль и слова:
# жаргон пишет правильно, числа — цифрами, сечение — как «3х2,5».
PROMPT = (
    "Отчёт мастера за день: проложили медь шестёрку 15 м и девятку 12 м, двенашку 8 м, "
    "изоляцию кифлекс 20 м, гофру 20 — 40 м, кабель 3х2,5 — 56 м, кабель 3х1,5 — 30 м, "
    "поставили 12 хомутов, 10 шпилек, автомат на 16 А, повесили внутряк и наружку, "
    "кассетник, дренаж, капилляр, помпа, сверление, гильзы, ПНР."
)
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


class TranscribeError(RuntimeError):
    """Голос не распознан. Текст можно показать мастеру."""


class Transcriber:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        attempts: int = 2,
        retry_wait: float = 1.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model = model
        self._attempts = attempts
        self._retry_wait = retry_wait
        # встроенные повторы SDK выключены — повторами управляет tenacity
        self._client = client or AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0
        )

    @classmethod
    def from_settings(cls, s: Settings) -> Transcriber | None:
        """None — ключ не задан: голос бот не принимает и просит прислать текст."""
        if not s.stt_api_key:
            return None
        return cls(s.stt_base_url, s.stt_api_key, s.stt_model, timeout=s.stt_timeout_s)

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Аудио → текст. Временные сбои повторяются; остальное — TranscribeError."""
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_fixed(self._retry_wait),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "Whisper: попытка {} не удалась ({}), повторяю",
                rs.attempt_number,
                type(rs.outcome.exception()).__name__ if rs.outcome else "?",
            ),
        )
        try:
            async for attempt in retrying:
                with attempt:
                    result = await self._client.audio.transcriptions.create(
                        model=self._model,
                        file=(filename, audio),
                        language="ru",
                        temperature=0,
                        prompt=PROMPT,
                    )
        except _RETRYABLE as e:
            logger.error("Whisper: сервис не отвечает ({})", type(e).__name__)
            raise TranscribeError(
                "Сервис распознавания голоса не отвечает. Попробуйте позже или пришлите текстом."
            ) from e
        except APIStatusError as e:
            # в лог — только код и тип: в ответе бывают куски запроса, ключ туда не попадает,
            # но текст отчёта мастера тоже не пишем
            logger.error("Whisper: HTTP {} ({})", e.status_code, type(e).__name__)
            raise TranscribeError("Распознавание голоса недоступно. Пришлите отчёт текстом.") from e
        text = (result if isinstance(result, str) else getattr(result, "text", "") or "").strip()
        # текст отчёта в лог не пишем — только объём
        logger.info("Whisper: {} КБ аудио → {} знаков", len(audio) // 1024, len(text))
        return text
