"""Сценарий отчёта: распознать → черновик → подтвердить → записать.

Хэндлеры бота тонкие и вызывают только этот сервис.
"""

from datetime import date

from m100bot.matching.models import MatchedItem


class ReportService:
    async def recognize(self, user_id: int, text: str) -> list[MatchedItem]:
        """TODO: matcher.match → проставить over_remaining → сохранить черновик."""
        raise NotImplementedError

    async def confirm(self, user_id: int, work_date: date) -> str:
        """TODO: только позиции со status=OK; Sheets (add) → журнал → outbox Битрикса.
        Вернуть текст итога. При ошибке Sheets черновик НЕ удалять."""
        raise NotImplementedError

    async def correct_today(self, user_id: int, row: int, qty: float) -> None:
        """TODO: mode='set' для сегодняшней даты, запись в журнал с prev_value."""
        raise NotImplementedError

    async def remaining(self, row: int) -> tuple[float, float, str]:
        """TODO: (план, выполнено, ед.) для ответа «сколько осталось по …»."""
        raise NotImplementedError
