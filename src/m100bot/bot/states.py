from aiogram.fsm.state import State, StatesGroup


class ReportFlow(StatesGroup):
    reviewing = State()  # показана карточка, ждём «Записать» / правки
    choosing = State()  # выбор варианта для неоднозначной позиции
    editing_qty = State()  # мастер вводит новый объём
    adding = State()  # мастер дописывает позицию текстом
    correcting_today = State()  # правка уже записанного за сегодня
