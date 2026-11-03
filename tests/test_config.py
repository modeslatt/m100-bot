"""Настройки: разбор значений из окружения и .env."""

from zoneinfo import ZoneInfo

import pytest

from m100bot.config import Settings

BASE = {"bot_token": "x", "spreadsheet_id": "y"}


def load(**kw: object) -> Settings:
    return Settings(_env_file=None, **BASE, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", []), ("   ", []), ("123", [123]), ("1, 2,3", [1, 2, 3]), ("1,,2,", [1, 2])],
)
def test_allowed_user_ids_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[int]
) -> None:
    """Пустое «ALLOWED_USER_IDS=» (как в .env.example) раньше роняло запуск на json.loads('')."""
    monkeypatch.setenv("ALLOWED_USER_IDS", raw)
    assert load().allowed_user_ids == expected


def test_allowed_user_ids_default_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    assert load().allowed_user_ids == []


def test_allowed_user_ids_from_dotenv_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=x\nSPREADSHEET_ID=y\nALLOWED_USER_IDS=\n", encoding="utf-8")
    assert Settings(_env_file=env).allowed_user_ids == []  # type: ignore[call-arg]
    env.write_text("BOT_TOKEN=x\nSPREADSHEET_ID=y\nALLOWED_USER_IDS=7, 8\n", encoding="utf-8")
    assert Settings(_env_file=env).allowed_user_ids == [7, 8]  # type: ignore[call-arg]


def test_timezone_database_is_available() -> None:
    """На Windows базы часовых поясов нет без пакета tzdata — бот не смог бы узнать «сегодня»."""
    assert ZoneInfo(load().timezone).key == "Europe/Minsk"


def test_log_file_optional() -> None:
    assert load().log_file is None
    assert load(log_file="").log_file is None
    assert str(load(log_file="data/bot.log").log_file).replace("\\", "/") == "data/bot.log"
