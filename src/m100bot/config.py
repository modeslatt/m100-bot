"""Настройки из .env (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LlmSettings(BaseSettings):
    """Только LLM — нужны скриптам и тесту точности без токена бота и таблицы."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (OpenAI-совместимый API; по умолчанию локальная Ollama)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3:8b"
    llm_timeout_s: float = 120
    llm_max_tokens: int = (
        2048  # ответ < 1000 токенов; меньше лимит — быстрее сбой при «зацикливании»
    )
    # qwen3 в Ollama: отключить режим рассуждений (reasoning_effort="none") — в ~2 раза быстрее
    llm_disable_thinking: bool = True
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""


class SheetsSettings(BaseSettings):
    """Только Google Sheets — нужны скриптам без токена бота и LLM."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_credentials_file: Path = Path("credentials.json")
    spreadsheet_id: str
    worksheet_name: str = "Лист1"
    synonyms_worksheet_name: str = "Синонимы"


class Settings(LlmSettings, SheetsSettings):
    # Telegram
    bot_token: str
    # NoDecode: иначе pydantic-settings читает пустое «ALLOWED_USER_IDS=» как JSON и падает
    allowed_user_ids: Annotated[list[int], NoDecode] = []

    # Голос → текст: Whisper через OpenAI-совместимый API (по умолчанию Groq).
    # Ключ пуст — голосовые не принимаются, бот просит прислать текст.
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_api_key: str = ""
    stt_model: str = "whisper-large-v3-turbo"
    stt_timeout_s: float = 60

    # Bitrix24
    bitrix_webhook_url: str = ""
    bitrix_entity_type_id: int | None = None

    # Misc
    timezone: str = "Europe/Minsk"
    db_path: Path = Path("data/bot.sqlite3")
    log_level: str = "INFO"
    # Лог ещё и в файл (UTF-8, ротация 10 МБ × 5). Пусто — только в консоль.
    log_file: Path | None = None

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("log_file", mode="before")
    @classmethod
    def _empty_path_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("bitrix_entity_type_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
