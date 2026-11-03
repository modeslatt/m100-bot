"""Валидатор и Matcher без сети: поддельные LlmMention и поддельный extractor."""

import pytest

from m100bot.matching.matcher import Matcher
from m100bot.matching.models import LlmMention, LlmResponse, MatchedItem, MatchStatus
from m100bot.sheets.catalog import Catalog


class FakeLlm:
    def __init__(self, *mentions: LlmMention) -> None:
        self.mentions = list(mentions)
        self.calls = 0

    async def extract(self, text: str, catalog: Catalog) -> LlmResponse:
        self.calls += 1
        return LlmResponse(mentions=self.mentions)


def m(phrase: str, candidates: list[int], qty: float | None = 1, unit: str | None = None):
    return LlmMention(phrase=phrase, qty=qty, unit=unit, candidates=candidates)


@pytest.fixture
def matcher() -> Matcher:
    return Matcher(FakeLlm())


@pytest.mark.parametrize(
    ("mention", "status", "row", "candidates"),
    [
        # признаки отсекают лишних кандидатов LLM
        (m("медь 6-ка 15 м", [17, 18, 19], 15, "м"), "ok", 17, []),
        (m("кабель 3 на 2,5 — 56 м", [55, 56, 57], 56, "м"), "ok", 55, []),
        (m("автомат на 16", [61, 62, 63], 1, "шт"), "ok", 61, []),
        (m("короб 150 на 60", [29, 30], 6, "м"), "ok", 29, []),
        (m("гофру 20-ку", [64, 65], 100, "м"), "ok", 64, []),
        (m("блок MDSAI2-07", [6, 10, 15], 1, "шт"), "ambiguous", None, [10, 15]),
        # размер без материала: «девятка» подходит и к Д9,52, и к Ф9
        (m("девятку протянули 12м", [18, 21, 33], 12, "м"), "ambiguous", None, [18, 21, 33]),
        # «9 мм» у изоляции — толщина стенки, подходят все три
        (m("кифлекс 9 мм", [20, 21, 22], 10, "м"), "ambiguous", None, [20, 21, 22]),
        (m("кабель полторашку", [55, 56, 57, 58, 60], 30, "м"), "ambiguous", None, [56, 57, 60]),
        # все кандидаты отсеяны → ничего не подставляем
        (m("кабель 3х6", [55, 58, 59], 15, "м"), "not_found", None, []),
        (m("автомат на 25 ампер", [61, 62, 63], 1, "шт"), "not_found", None, []),
        (m("покрасили стену", [], 20, "м2"), "not_found", None, []),
        (m("что-то", [999], 1, "шт"), "not_found", None, []),  # строки нет в справочнике
        # размер/сечение есть во фразе, а у позиции такого признака нет (дыра из box-04)
        (m("короб 25х16 6 метров", [65, 67], 6, "м"), "clarify", 67, []),
        (m("короб 25х16 6 метров", [65], 6, "м"), "not_found", None, []),
        (m("отводов 90 на 25-й", [35], 4, "шт"), "ok", 35, []),  # 90 и 25 есть в названии
        (m("тройку на полтора", [36, 37, 56], 16, "м"), "ok", 56, []),
        # ед. изм. не та
        (m("короб 25х16 6 метров", [67], 6, "метров"), "clarify", 67, []),
        # объём не назван
        (m("медь 6-ку тянули", [17], None, None), "clarify", 17, []),
        # единица не названа — не считаем ошибкой
        (m("фреонку 12.7 положили 30", [19], 30, None), "ok", 19, []),
    ],
)
def test_validate(catalog: Catalog, matcher: Matcher, mention, status, row, candidates) -> None:
    item = matcher.validate(mention, catalog)
    assert (item.status, item.row, item.candidates) == (MatchStatus(status), row, candidates)


def test_synonym_overrides_features(catalog: Catalog, matcher: Matcher) -> None:
    item = matcher.validate(m("гофра 20 32 м", [], 32, "м"), catalog, {"гофра 20": 64})
    assert (item.status, item.row) == (MatchStatus.OK, 64)


async def test_match_empty_text_skips_llm(catalog: Catalog) -> None:
    llm = FakeLlm()
    assert await Matcher(llm).match("   ", catalog) == []
    assert llm.calls == 0


# ─────────────── Варианты одного товара, слова, ссылки, пропуски ───────────────


@pytest.mark.parametrize(
    ("mention", "status", "row", "candidates"),
    [
        # LLM нашла товар, но не тот вариант: код выбирает вариант по признакам
        (m("кабель полторашку 30 м", [55], 30, "м"), "ambiguous", None, [56, 57, 60]),
        (m("гофры 20-ки 32 м", [65], 32, "м"), "ok", 64, []),
        # LLM угадала один вариант, а фраза их не различает — спрашиваем
        (m("поставили 12 хомутов", [43], 12, "шт"), "ambiguous", None, [43, 44]),
        (m("тройник 25 — 3 шт", [36], 3, "шт"), "ambiguous", None, [36, 37]),
        (m("внутренний блок MDSAI2-07 один", [10], 1, "шт"), "ambiguous", None, [10, 15]),
        # фраза различает словами и числами
        (m("10 хомутов под фреон", [43, 44], 10, "шт"), "ok", 44, []),
        (m("поставили дренажный насос 1", [23, 25], 1, "шт"), "ok", 23, []),
        (m("дренаж пп 12 метров", [33, 34], 12, "м"), "ok", 34, []),
        (m("кассету 12-ю смонтировали", [11, 12], 1, "шт"), "ok", 11, []),
        (m("гофру 20-ку", [55, 56, 64, 69], 100, "м"), "ok", 64, []),
        # LLM дала позицию без размера («Установка АВ»): ищем позиции с этим размером в той же ед.
        (m("гофру 20-ку 100 метров", [68], 100, "м"), "ok", 64, []),
        # размер без материала: все позиции с этим размером, с материалом — только он
        (m("проложили шестёрку 20 метров", [17], 20, "м"), "ambiguous", None, [17, 20]),
        (m("изоляцию на шестёрку 15 м", [17, 20], 15, "м"), "ok", 20, []),
        (m("трасса 1/4", [17, 20], 14, "м"), "ok", 17, []),
    ],
)
def test_select_variants(
    catalog: Catalog, matcher: Matcher, mention, status, row, candidates
) -> None:
    item = matcher.validate(mention, catalog)
    assert (item.status, item.row, item.candidates) == (MatchStatus(status), row, candidates)


def test_no_sibling_expansion_when_disabled(catalog: Catalog) -> None:
    strict = Matcher(FakeLlm(), expand_siblings=False)
    item = strict.validate(m("кабель полторашку 30 м", [55], 30, "м"), catalog)
    assert item.status == "not_found"


async def test_split_merged_mention(catalog: Catalog) -> None:
    llm = FakeLlm(m("автоматы 20А и 32А по одному", [61, 62, 63], 1, "шт"))
    items = await Matcher(llm).match("автоматы 20А и 32А по одному", catalog)
    assert [(i.status, i.row, i.qty) for i in items] == [("ok", 62, 1), ("ok", 63, 1)]


async def test_qty_from_text_beats_llm(catalog: Catalog) -> None:
    llm = FakeLlm(m("пнр мультисплита 2 системы", [51], 1, "шт"))
    [item] = await Matcher(llm).match("пнр мультисплита 2 системы", catalog)
    assert (item.status, item.row, item.qty, item.unit) == ("ok", 51, 2, "сист")


async def test_reference_to_previous_diameters(catalog: Catalog) -> None:
    llm = FakeLlm(
        m("медь 6-ку 15 м", [17], 15, "м"),
        m("9-ку 15 м", [18], 15, "м"),
        m("изоляцию на них столько же", [20, 21, 22], 15, "м"),
    )
    items = await Matcher(llm).match(
        "медь 6-ку 15 м и 9-ку 15 м, изоляцию на них столько же", catalog
    )
    assert [(i.row, i.qty) for i in items] == [(17, 15), (18, 15), (20, 15), (21, 15)]


class SequenceLlm:
    """Первый ответ теряет упоминание, второй — полный."""

    def __init__(self, *responses: list[LlmMention]) -> None:
        self.responses = list(responses)
        self.texts: list[str] = []

    async def extract(self, text: str, catalog: Catalog) -> LlmResponse:
        self.texts.append(text)
        return LlmResponse(mentions=self.responses.pop(0))


async def test_reasks_when_quantity_lost(catalog: Catalog) -> None:
    text = "медь 6-ку 15 м и 9-ку 15 м"
    llm = SequenceLlm(
        [m("медь 6-ку 15 м", [17], 15, "м")],
        [m("медь 6-ку 15 м", [17], 15, "м"), m("9-ку 15 м", [18], 15, "м")],
    )
    items = await Matcher(llm).match(text, catalog)
    assert [i.row for i in items] == [17, 18]
    assert len(llm.texts) == 2 and "пропущены" in llm.texts[1]


async def test_no_reask_when_quantity_only_in_fields(catalog: Catalog) -> None:
    """LLM вырезала фразу без числа, но объём положила в qty/unit — это не потеря."""
    llm = SequenceLlm([m("медь 6-ку", [17], 15, "метров")])
    items = await Matcher(llm).match("медь 6-ку 15 м", catalog)
    assert len(llm.texts) == 1
    assert [(i.row, i.qty) for i in items] == [(17, 15)]


async def test_reasks_when_fields_cover_only_one_of_two(catalog: Catalog) -> None:
    """Два одинаковых объёма в тексте, упоминание одно — переспрос нужен."""
    llm = SequenceLlm(
        [m("медь 6-ку", [17], 15, "м")],
        [m("медь 6-ку", [17], 15, "м"), m("9-ку", [18], 15, "м")],
    )
    items = await Matcher(llm).match("медь 6-ку 15 м и 9-ку 15 м", catalog)
    assert len(llm.texts) == 2
    assert [i.row for i in items] == [17, 18]


async def test_no_reask_when_complete(catalog: Catalog) -> None:
    llm = SequenceLlm([m("кабель 3х2,5 — 56 м", [55], 56, "м")])
    await Matcher(llm).match("кабель 3х2,5 — 56 м", catalog)
    assert len(llm.texts) == 1


def test_matcher_uses_catalog_from_call(catalog: Catalog, matcher: Matcher) -> None:
    """Таблицу правят: матчер ничего не хранит и работает по справочнику из вызова."""
    smaller = Catalog(
        [p for p in catalog.positions if p.row != 56], catalog.date_columns, catalog.last_col
    )
    item = matcher.validate(m("кабель полторашку 30 м", [55], 30, "м"), smaller)
    assert item.candidates == [57, 60]  # варианты кабеля берутся уже без удалённой строки 56


@pytest.mark.parametrize(
    ("phrase", "qty", "row"),
    [
        ("12 хомутов", 12, 44),
        ("хомутов 5 штук", 5, 44),  # другая формулировка и объём
        ("поставили 3 хомутов", 3, 44),  # глагол не мешает
        ("тройник 25", 3, None),  # чужая фраза не совпадает
    ],
)
def test_learned_synonym_ignores_quantity(
    catalog: Catalog, matcher: Matcher, phrase: str, qty: float, row: int | None
) -> None:
    item = matcher.validate(m(phrase, [], qty, "шт"), catalog, {"Хомутов": 44})
    assert item.row == row


def test_sheet_synonym_with_quantity_still_matches(catalog: Catalog, matcher: Matcher) -> None:
    """На листе «Синонимы» фразу могли вписать вручную с объёмом и глаголом."""
    item = matcher.validate(m("гофра 20", [], 32, "м"), catalog, {"проложили гофра 20 32 м": 64})
    assert item.row == 64


async def test_match_resolves_ambiguous_phrase_by_learned_synonym(catalog: Catalog) -> None:
    llm = FakeLlm(m("поставили 12 хомутов", [43, 44], 12, "шт"))
    items = await Matcher(llm).match("поставили 12 хомутов", catalog, {"хомутов": 44})
    assert [(i.status, i.row, i.qty) for i in items] == [(MatchStatus.OK, 44, 12)]


@pytest.mark.parametrize(
    ("qty", "left", "over"),
    [(15, 10, True), (10, 10, False), (5, None, False), (None, 10, False)],
)
def test_with_remaining_marks_over(qty: float | None, left: float | None, over: bool) -> None:
    item = MatchedItem(phrase="…", qty=qty, unit="м", status=MatchStatus.OK, row=17)
    marked = item.with_remaining(left)
    assert (marked.over_remaining, marked.remaining) == (over, left)
