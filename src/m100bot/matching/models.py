"""Модели результата сопоставления."""

from enum import StrEnum

from pydantic import BaseModel, Field


class MatchStatus(StrEnum):
    OK = "ok"  # ровно одна позиция, ед. изм. совпадает
    AMBIGUOUS = "ambiguous"  # несколько кандидатов → спросить кнопками
    NOT_FOUND = "not_found"  # в смете нет → сообщить, предложить выбрать вручную
    CLARIFY = "clarify"  # позиция ясна, но ед. изм./объём требуют уточнения


class LlmMention(BaseModel):
    """Одно упоминание работы, как его вернула LLM (сырое, до валидации)."""

    phrase: str = Field(description="Фрагмент сообщения мастера")
    qty: float | None = Field(description="Объём числом; null, если не назван")
    unit: str | None = Field(description="Единица, как сказал мастер: м, шт, кг, компл, сист")
    candidates: list[int] = Field(
        description="Номера ВСЕХ подходящих строк справочника, лучший первым"
    )
    note: str | None = Field(
        default=None, description="Короткое пояснение (одна фраза), если есть сомнения"
    )


class LlmResponse(BaseModel):
    mentions: list[LlmMention]


class MatchedItem(BaseModel):
    """Итог по одному упоминанию — то, что видит мастер в карточке подтверждения."""

    phrase: str
    qty: float | None
    unit: str | None
    status: MatchStatus
    row: int | None = None  # выбранная строка, если status == OK
    candidates: list[int] = []  # варианты для кнопок при AMBIGUOUS
    message: str | None = None  # что сказать мастеру (почему уточняем)
    over_remaining: bool = False  # объём больше остатка по смете
