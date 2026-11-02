"""Настройки из .env (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(LlmSettings):
    # Telegram
    bot_token: str
    allowed_user_ids: list[int] = []

    # Speech-to-text
    stt_provider: str = "local"  # local (faster-whisper) | api (OpenAI-совместимый)
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_base_url: str = ""
    stt_api_key: str = ""

    # Google Sheets
    google_credentials_file: Path = Path("credentials.json")
    spreadsheet_id: str
    worksheet_name: str = "Лист1"
    synonyms_worksheet_name: str = "Синонимы"

    # Bitrix24
    bitrix_webhook_url: str = ""
    bitrix_entity_type_id: int | None = None

    # Misc
    timezone: str = "Europe/Minsk"
    db_path: Path = Path("data/bot.sqlite3")
    log_level: str = "INFO"

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("bitrix_entity_type_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def spreadsheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
