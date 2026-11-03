"""Модели результата сопоставления."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

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


# report — мастер сообщает о сделанной работе; question — спрашивает остаток по смете
Intent = Literal["report", "question"]


class LlmResponse(BaseModel):
    intent: Intent = Field(
        default="report",
        description="report — отчёт о сделанной работе; question — вопрос об остатке по смете",
    )
    mentions: list[LlmMention]


class MatchedItem(BaseModel):
    """Итог по одному упоминанию — то, что видит мастер в карточке подтверждения."""

    phrase: str
    qty: float | None
    unit: str | None
    status: MatchStatus
    row: int | None = None  # выбранная строка, если status == OK
    candidates: list[int] = []  # варианты для кнопок при AMBIGUOUS
    over_remaining: bool = False  # объём больше остатка по смете
    remaining: float | None = None  # сколько осталось по смете (для «осталось 12 м»)
    picked: bool = False  # вариант выбрал мастер кнопкой — после записи запомним как синоним

    @property
    def is_ready(self) -> bool:
        """Можно записывать: позиция определена, объём назван."""
        return (
            self.status == MatchStatus.OK
            and self.row is not None
            and self.qty is not None
            and self.qty > 0
        )

    def with_remaining(self, left: float | None) -> "MatchedItem":
        """Отметить остаток по смете и превышение его объёмом (⚠️ в карточке)."""
        over = self.qty is not None and left is not None and self.qty > left + 1e-9
        return self.model_copy(update={"remaining": left, "over_remaining": over})


@dataclass(frozen=True)
class Understood:
    """Что хотел мастер и о каких позициях речь."""

    intent: Intent
    items: list[MatchedItem]
    truncated: bool = False  # LLM вернула предельное число упоминаний — часть могла не влезть
