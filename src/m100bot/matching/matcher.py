"""Гибридное сопоставление: синонимы → LLM → проверка и уточнение кодом.

Публичный интерфейс — `Matcher.match(text, catalog, synonyms) -> list[MatchedItem]`.
Его вызывают сервис отчётов и эталонный тест tests/test_golden.py. Справочник и синонимы
передаются в каждый вызов: таблицу правят, и матчер не должен хранить устаревшую копию.

LLM предлагает кандидатов, код их проверяет и уточняет:
- отсеивает кандидатов, у которых признаки (диаметр, сечение, ток, размер, модель) не
  совпадают с фразой;
- добавляет другие варианты того же товара (`expand_siblings`): LLM часто находит товар,
  но ошибается в варианте («кабель 3х2,5» вместо названной «полторашки»);
- если материал не назван («девятку протянули»), добавляет все позиции с этим размером;
- из оставшихся предпочитает позиции, где признак или слово фразы есть явно.
Товар другого типа (другое первое слово названия) код сам не подставляет никогда.
Остался один кандидат → OK, несколько → спросить мастера, ноль → «нет в смете».
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from loguru import logger

from m100bot.matching.features import PhraseFeatures, extract_phrase_features
from m100bot.matching.llm import MAX_MENTIONS
from m100bot.matching.models import LlmMention, LlmResponse, MatchedItem, MatchStatus, Understood
from m100bot.matching.normalize import NUMBER_WORDS, normalize_text, normalize_unit
from m100bot.matching.phrase import (
    bare_numbers,
    find_quantities,
    jargon_words,
    split_by_features,
    synonym_key,
    words,
)
from m100bot.sheets.catalog import Catalog, Position

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Сколько первых букв слова сравниваем. Разные задачи — разная строгость:
TYPE_PREFIX = 3  # «называет ли фраза тип позиции»: «хом» — хомут, хомуты, хомутов
WORD_PREFIX = 4  # «есть ли слово фразы в названии»: строже, чтобы не путать похожие слова
GROUP_PREFIX = 5  # «варианты одного товара»: «кабел», «хомут» — по первому слову названия

# Жаргон мастера, который называет тип позиции другим словом (как «шестёрка» в features.py)
TYPE_SLANG = {"трасс": "фре"}  # «трасса» — фреоновая трасса, т.е. фреонопровод


class Extractor(Protocol):
    async def extract(self, text: str, catalog: Catalog) -> LlmResponse: ...


# ─────────────── Числа и слова названия ───────────────


@lru_cache(maxsize=4096)
def _name_words(short_name: str) -> tuple[str, ...]:
    return tuple(words(short_name))


def _name_numbers(pos: Position) -> set[float]:
    return {float(x) for x in _NUMBER.findall(pos.norm_name)}


def _numbers_in_name(spec: str, pos: Position) -> bool:
    """Все числа из «25х16» / «3х1.5» встречаются в названии позиции."""
    numbers = _name_numbers(pos)
    return all(float(x) in numbers for x in spec.split("х"))


# ─────────────── Сравнение признаков ───────────────


def _same_size(phrase_value: float, item_value: float) -> bool:
    """Мастер называет номинал: «девятка» (9.52) подходит и к Д9,52, и к Ф9.

    Точное совпадение или совпадение целой части, если одно из чисел дробное.
    """
    if math.isclose(phrase_value, item_value, abs_tol=0.01):
        return True
    fractional = not phrase_value.is_integer() or not item_value.is_integer()
    return fractional and math.floor(phrase_value) == math.floor(item_value)


def _section_ok(phrase_section: str, item_section: str) -> bool:
    cores, cross = phrase_section.split("х")
    item_cores, item_cross = item_section.split("х")
    if not math.isclose(float(cross), float(item_cross)):
        return False
    return cores == "?" or cores == item_cores


# Проверка одного вида признака: True — у позиции признак есть и совпал, False — есть и
# не совпал (конфликт), None — у позиции такого признака нет, проверка неприменима
# (диаметр «20» не мешает автомату, ток «16» не мешает трубе).
Check = Callable[[Position], bool | None]


def _any_or_none(values: list, test: Callable[[object], bool]) -> bool | None:
    return any(test(v) for v in values) if values else None


def feature_checks(pf: PhraseFeatures) -> list[tuple[str, Check]]:
    """Проверки по видам признаков, названным во фразе. Порядок важен для `prefer_explicit`.

    Если во фразе названо несколько значений одного вида, достаточно совпадения с любым.
    """
    checks: list[tuple[str, Check]] = []
    if pf.diameters:
        checks.append(
            (
                "диаметр",
                lambda p: _any_or_none(
                    p.features.diameters, lambda d: any(_same_size(x, d) for x in pf.diameters)
                ),
            )
        )
    if pf.dims_mm:
        # «9 мм» — это может быть и диаметр, и толщина стенки изоляции
        checks.append(
            (
                "мм",
                lambda p: _any_or_none(
                    p.features.diameters + p.features.thickness,
                    lambda d: any(_same_size(x, d) for x in pf.dims_mm),
                ),
            )
        )
    if pf.sections:
        full = [s for s in pf.sections if not s.startswith("?")]

        def section(p: Position) -> bool | None:
            if p.features.sections:
                return any(_section_ok(s, ps) for s in pf.sections for ps in p.features.sections)
            # Полное сечение «3х1.5» — это два числа. У позиции без сечения оба должны быть
            # в названии: «отвод 90° Д25» для «90 на 25» — да, «труба Д25» для «3х1.5» — нет.
            return any(_numbers_in_name(s, p) for s in full) if full else None

        checks.append(("сечение", section))
    if pf.sizes:
        # Размер «25х16» — тоже два числа: без признака размера — ищем оба в названии
        checks.append(
            (
                "размер",
                lambda p: (
                    bool(set(pf.sizes) & set(p.features.sizes))
                    if p.features.sizes
                    else any(_numbers_in_name(s, p) for s in pf.sizes)
                ),
            )
        )
    if pf.currents:
        checks.append(
            (
                "ток",
                lambda p: (
                    None if p.features.current_a is None else p.features.current_a in pf.currents
                ),
            )
        )
    if pf.models:
        checks.append(
            (
                "модель",
                lambda p: _any_or_none(
                    p.features.models, lambda m: any(m.startswith(x) for x in pf.models)
                ),
            )
        )
    return checks


def features_conflict(checks: list[tuple[str, Check]], pos: Position) -> str | None:
    """Какой признак фразы противоречит позиции; None — противоречий нет."""
    for kind, check in checks:
        if check(pos) is False:
            return kind
    return None


def prefer_explicit(
    checks: list[tuple[str, Check]], numbers: list[float], survivors: list[Position]
) -> list[Position]:
    """Если признак фразы явно есть у части позиций — оставляем только их.

    «гофра 20-ка»: у трубы ПВХ Д20 диаметр 20 есть, у кабелей диаметра нет вовсе —
    кабели уходят. Если явного совпадения нет ни у кого, признак ничего не меняет.
    Голые числа фразы («насос 1») — тоже признак: все они должны быть в названии.
    """
    tests: list[Callable[[Position], bool | None]] = [check for _, check in checks]
    if numbers:
        tests.append(
            lambda p: all(any(_same_size(n, x) for x in _name_numbers(p)) for n in numbers)
        )
    for test in tests:
        hit = [p for p in survivors if test(p) is True]
        if hit:
            survivors = hit
    return survivors


# ─────────────── Слова фразы ───────────────


def _type_prefix(word: str) -> str:
    for slang, prefix in TYPE_SLANG.items():
        if word.startswith(slang):
            return prefix
    return word[:TYPE_PREFIX]


def names_type(text_words: list[str], pos: Position) -> int:
    """Сколько слов текста называют тип позиции (первые три значимых слова названия)."""
    type_words = [w for w in _name_words(pos.short_name) if len(w) >= 4][:3]
    prefixes = {w[:TYPE_PREFIX] for w in type_words}
    return sum(1 for w in text_words if len(w) >= 4 and _type_prefix(w) in prefixes)


def _word_hits(text_words: list[str], pos: Position) -> int:
    """Сколько слов фразы встречается в названии (по началу слова; короткое — целиком)."""
    name = _name_words(pos.short_name)
    hits = 0
    for w in text_words:
        if len(w) >= WORD_PREFIX:
            hits += any(nw.startswith(w[:WORD_PREFIX]) for nw in name)
        elif len(w) >= 2:
            hits += w in name
    return hits


def _named_by(name_words: list[str], pos: Position) -> bool:
    """Название начинается с первого слова («Труба …») и содержит остальные («ПВХ»).
    Тройник «для ПВХ труб» — не труба ПВХ."""
    first = next(iter(_name_words(pos.short_name)), "")
    return first.startswith(name_words[0][:WORD_PREFIX]) and _word_hits(name_words, pos) == len(
        name_words
    )


def prefer_by_words(text_words: list[str], survivors: list[Position]) -> list[Position]:
    """«хомуты на дренаж» → хомут для дренажа, а не для фреонопровода.

    Сначала по типу позиции (первые слова названия), потом по всему названию. Если фраза
    ничем не различает позиции — оставляем все: бот спросит.
    """
    for score in (names_type, _word_hits):
        if len(survivors) < 2:
            break
        scores = {p.row: score(text_words, p) for p in survivors}
        best = max(scores.values())
        if best > 0:
            survivors = [p for p in survivors if scores[p.row] == best]
    return survivors


def _group_key(pos: Position) -> tuple[str, str] | None:
    """«Варианты одного товара»: одинаковое первое слово названия и ед. изм."""
    head = next((w for w in _name_words(pos.short_name) if len(w) >= 4), None)
    return (head[:GROUP_PREFIX], pos.unit) if head else None


# ─────────────── Итог по позиции ───────────────


def resolve_position(item: MatchedItem, pos: Position) -> MatchedItem:
    """Позиция определена (матчером или кнопкой мастера) — проверяем ед. изм. и объём.

    Ед. изм. не та → CLARIFY («в смете шт, а сказано м»); объём не назван → CLARIFY;
    иначе OK. Одно правило для матчера и для выбора кнопкой.
    """
    unit = normalize_unit(item.unit)
    if (unit is not None and unit != pos.unit) or item.qty is None or item.qty <= 0:
        status = MatchStatus.CLARIFY
    else:
        status = MatchStatus.OK
    return item.model_copy(
        update={
            "row": pos.row,
            "candidates": [],
            "status": status,
            "pos_number": pos.number,
            "pos_name": pos.norm_name,
        }
    )


# ─────────────── Ссылки «изоляцию на них столько же» ───────────────

_REFERENCE = re.compile(
    r"\b(?:на них|на обе|на оба|на нее|на него|на ту же|столько же|туда же|на них же)\b"
)


# ─────────────── Matcher ───────────────


@dataclass(frozen=True)
class _Index:
    """Справочник и синонимы одного вызова, подготовленные для поиска."""

    catalog: Catalog
    synonyms: dict[str, int]  # ключ синонима (`synonym_key`) → строка
    groups: dict[tuple[str, str], list[Position]]

    @classmethod
    def build(cls, catalog: Catalog, synonyms: Mapping[str, int] | None) -> "_Index":
        keys = {synonym_key(k): v for k, v in (synonyms or {}).items()}
        groups: dict[tuple[str, str], list[Position]] = {}
        for p in catalog.positions:
            if key := _group_key(p):
                groups.setdefault(key, []).append(p)
        return cls(catalog, {k: v for k, v in keys.items() if k}, groups)

    def synonym(self, phrase: str, qty: float | None) -> Position | None:
        """Фраза мастера совпала с листом «Синонимы» (без объёма и глаголов)."""
        row = self.synonyms.get(synonym_key(phrase, qty))
        return self.catalog.by_row(row) if row is not None else None


class Matcher:
    def __init__(self, llm: Extractor, *, expand_siblings: bool = True) -> None:
        self.llm = llm
        self.expand_siblings = expand_siblings

    # ── сообщение целиком ──

    async def understand(
        self, text: str, catalog: Catalog, synonyms: Mapping[str, int] | None = None
    ) -> Understood:
        """Сообщение мастера → отчёт (позиции на подтверждение) или вопрос об остатке
        (позиции, о которых спрашивают). Ошибки LLM (LlmError) — наверх."""
        if not normalize_text(text):
            return Understood("report", [])
        index = _Index.build(catalog, synonyms)
        response = await self._extract_complete(text, catalog)
        intent = response.intent
        if intent == "question" and find_quantities(text):
            # «проложили 15 м, сколько осталось?» — есть сделанная работа, значит это отчёт
            logger.info("LLM сочла вопросом, но в сообщении есть объёмы — это отчёт")
            intent = "report"
        mentions = _collapse_loops(response.mentions)
        truncated = len(mentions) >= MAX_MENTIONS
        if truncated:
            logger.warning(
                "LLM вернула {} упоминаний — предел, часть могла не влезть", MAX_MENTIONS
            )
        if intent == "report":
            mentions = [_drop_invented_qty(m, text) for m in mentions]
        if intent == "question":
            # о чём спрашивают — важна только позиция: объём не нужен и не должен влиять
            mentions = [m.model_copy(update={"qty": None, "unit": None}) for m in mentions]
            if not mentions:  # «сколько осталось?» без позиции
                mentions = [LlmMention(phrase=text, qty=None, unit=None, candidates=[])]
        message_words = words(text)

        items = [
            self._validate(part, index, message_words)
            for mention in mentions
            for part in _split(mention)
        ]
        items = self._resolve_references(items, catalog)
        if intent == "question":
            items = [_too_vague(i) for i in items]
        logger.info(
            "match ({}): {} позиций: {}",
            intent,
            len(items),
            ", ".join(f"{i.status}:{i.row or i.candidates}" for i in items),
        )
        return Understood(intent, items, truncated)

    async def match(
        self, text: str, catalog: Catalog, synonyms: Mapping[str, int] | None = None
    ) -> list[MatchedItem]:
        """Только позиции (без намерения) — для тестов и скриптов."""
        return (await self.understand(text, catalog, synonyms)).items

    async def _extract_complete(self, text: str, catalog: Catalog) -> LlmResponse:
        """Вызов LLM + проверка, что ни один «объём с единицей» из сообщения не потерян.

        Если потерян — один раз переспрашиваем с подсказкой и берём более полный ответ.
        """
        response = await self.llm.extract(text, catalog)
        missing = _missing_quantities(text, response.mentions)
        if not missing:
            return response
        logger.warning("LLM пропустила объёмы {} — переспрашиваю", missing)
        hint = ", ".join(f"«{q}»" for q in missing)
        retry_text = (
            f"{text}\n\n(Проверка: в прошлом ответе пропущены упоминания с объёмами {hint}. "
            "Верни ВСЕ упоминания сообщения заново, каждое отдельно.)"
        )
        retry = await self.llm.extract(retry_text, catalog)
        if len(_missing_quantities(text, retry.mentions)) < len(missing):
            return retry
        return response

    def _resolve_references(self, items: list[MatchedItem], catalog: Catalog) -> list[MatchedItem]:
        """«изоляцию на них столько же» → по позиции на каждый диаметр из предыдущих строк."""
        out: list[MatchedItem] = []
        for item in items:
            if item.status == MatchStatus.AMBIGUOUS and _REFERENCE.search(
                normalize_text(item.phrase)
            ):
                resolved = _by_previous_diameters(item, out, catalog)
                if resolved:
                    out.extend(resolved)
                    continue
            out.append(item)
        return out

    # ── одно упоминание ──

    def validate(
        self,
        mention: LlmMention,
        catalog: Catalog,
        synonyms: Mapping[str, int] | None = None,
    ) -> MatchedItem:
        """Одно упоминание → итог (используется в тестах; бот идёт через `match`)."""
        return self._validate(mention, _Index.build(catalog, synonyms), words(mention.phrase))

    def _validate(
        self, mention: LlmMention, index: _Index, message_words: list[str]
    ) -> MatchedItem:
        """0 кандидатов → NOT_FOUND; ед. изм. не та → CLARIFY; >1 → AMBIGUOUS;
        1 → `resolve_position` (OK или CLARIFY, если объём не назван)."""
        unit = normalize_unit(mention.unit)
        item = MatchedItem(
            phrase=mention.phrase, qty=mention.qty, unit=unit, status=MatchStatus.NOT_FOUND
        )
        # синониму мастера доверяем без проверки признаков
        synonym = index.synonym(mention.phrase, mention.qty)
        survivors = [synonym] if synonym else self._select(mention, index, message_words)
        if not survivors:
            return item

        if unit is not None:
            same_unit = [p for p in survivors if p.unit == unit]
            if not same_unit and len(survivors) > 1:
                rows = [p.row for p in survivors]
                return item.model_copy(update={"status": MatchStatus.CLARIFY, "candidates": rows})
            survivors = same_unit or survivors

        if len(survivors) > 1:
            return item.model_copy(
                update={
                    "status": MatchStatus.AMBIGUOUS,
                    "candidates": [p.row for p in survivors],
                }
            )
        return resolve_position(item, survivors[0])

    def _select(
        self, mention: LlmMention, index: _Index, message_words: list[str]
    ) -> list[Position]:
        """Кандидаты LLM → проверенные позиции.

        1. Кандидаты LLM + другие варианты того же товара (если expand_siblings).
        2. Отсев по признакам (диаметр, сечение, ток, размер, модель).
        3. Предпочтение позиций, где признак фразы есть явно.
        4. Размер без материала («шестёрку проложили») — все позиции с этим размером.
        5. Предпочтение позиций, которые фраза называет словами.
        """
        pf = extract_phrase_features(mention.phrase)
        checks = feature_checks(pf)
        exclude = {mention.qty} if mention.qty is not None else set()
        numbers = bare_numbers(mention.phrase, exclude)
        jargon = jargon_words(mention.phrase)
        phrase_words = words(mention.phrase) + jargon

        llm_rows: list[Position] = []
        for row in dict.fromkeys(mention.candidates):
            pos = index.catalog.by_row(row)
            if pos is None:
                logger.warning("LLM вернула строку {}, которой нет в справочнике", row)
                continue
            llm_rows.append(pos)
        if jargon:
            # жаргон однозначно называет материал: позиции — по названию, а не от модели
            named = [p for p in index.catalog.positions if _named_by(jargon, p)]
            if named:
                llm_rows = named

        pool = list(llm_rows)
        if self.expand_siblings:
            for pos in llm_rows:
                key = _group_key(pos)
                pool += index.groups.get(key, []) if key else []
        pool = list({p.row: p for p in pool}.values())

        survivors = []
        for pos in pool:
            if conflict := features_conflict(checks, pos):
                logger.debug("«{}»: строка {} отсеяна: {}", mention.phrase, pos.row, conflict)
                continue
            survivors.append(pos)
        survivors = prefer_explicit(checks, numbers, survivors)

        if (
            pf.diameters
            and survivors
            and not any(
                names_type(phrase_words, p) or names_type(message_words, p) for p in survivors
            )
        ):
            unit = normalize_unit(mention.unit)
            survivors = _same_size_positions(index.catalog, pf, survivors, unit)
            survivors = prefer_explicit(checks, numbers, survivors)

        survivors = prefer_by_words(phrase_words, survivors)
        return sorted(survivors, key=lambda p: p.row)


# ─────────────── Вспомогательное ───────────────

LOOP_REPEATS = 3  # столько одинаковых упоминаний подряд — это «зацикливание» модели


def _collapse_loops(mentions: list[LlmMention]) -> list[LlmMention]:
    """Модель иногда повторяет одно упоминание до лимита токенов. Серия из LOOP_REPEATS и
    более одинаковых подряд — одно упоминание; одиночный повтор («хомуты 10 … хомуты 10»)
    мог сказать и мастер, его не трогаем."""
    out: list[LlmMention] = []
    run: list[LlmMention] = []
    for m in [*mentions, None]:
        if (
            m is not None
            and run
            and m.model_dump(exclude={"note"}) == run[0].model_dump(exclude={"note"})
        ):
            run.append(m)
            continue
        if len(run) >= LOOP_REPEATS:
            logger.warning("LLM повторила «{}» {} раз — оставляю одно", run[0].phrase, len(run))
            out.append(run[0])
        else:
            out.extend(run)
        run = [m] if m is not None else []
    return out


_DIGITS = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")


def _qty_in_text(qty: float, text: str) -> bool:
    """Объём, который назвала LLM, есть в сообщении: цифрами, словами («двадцать пять»,
    «полтора») или это 1 (единственное число в «шт» — согласованное правило)."""
    if math.isclose(qty, 1):
        return True
    t = normalize_text(text)
    if any(math.isclose(float(x), qty) for x in _DIGITS.findall(t)):
        return True
    return any(
        w in NUMBER_WORDS or w.startswith(("полтор", "половин")) for w in re.findall(r"[а-я]+", t)
    )


def _drop_invented_qty(mention: LlmMention, text: str) -> LlmMention:
    """LLM назвала объём, которого мастер не говорил («медь 6-ку тянули» → 15 м) — не верим:
    бот спросит «сколько?». Выдуманный объём опаснее лишнего вопроса."""
    if mention.qty is None or _qty_in_text(mention.qty, text):
        return mention
    logger.warning("LLM назвала объём {}, которого нет в сообщении — спрошу у мастера", mention.qty)
    return mention.model_copy(update={"qty": None})


# Больше вариантов в вопросе об остатке — позиция по сути не названа («сколько осталось?»)
MAX_QUESTION_CHOICES = 8


def _too_vague(item: MatchedItem) -> MatchedItem:
    """Вопрос подходит к слишком многим позициям — попросить спросить точнее, а не
    показывать список из десятков строк."""
    if item.status == MatchStatus.AMBIGUOUS and len(item.candidates) > MAX_QUESTION_CHOICES:
        return item.model_copy(update={"status": MatchStatus.NOT_FOUND, "candidates": []})
    return item


def _same_size_positions(
    catalog: Catalog, pf: PhraseFeatures, survivors: list[Position], unit: str | None
) -> list[Position]:
    """Материал не назван ни во фразе, ни в сообщении: «девятку протянули» — это может
    быть медь, изоляция или капиллярная трубка. Все позиции с этим размером и той же ед.
    (ед. из фразы, а если не названа — ед. кандидатов LLM)."""
    units = {unit} if unit else {p.unit for p in survivors}
    extra = [
        p
        for p in catalog.positions
        if p.unit in units
        and any(_same_size(x, d) for x in pf.diameters for d in p.features.diameters)
    ]
    return list({p.row: p for p in survivors + extra}.values())


def _by_previous_diameters(
    item: MatchedItem, previous: list[MatchedItem], catalog: Catalog
) -> list[MatchedItem]:
    diameters: list[float] = []
    for prev in previous:
        pos = catalog.by_row(prev.row) if prev.row is not None else None
        if pos and len(pos.features.diameters) == 1:
            d = pos.features.diameters[0]
            if d not in diameters:
                diameters.append(d)
    picks: list[int] = []
    for d in diameters:
        rows = [
            r
            for r in item.candidates
            if (p := catalog.by_row(r)) and any(_same_size(d, x) for x in p.features.diameters)
        ]
        if len(rows) != 1:
            return []
        picks.append(rows[0])
    if not picks:
        return []
    resolved = []
    for r in picks:
        pos = catalog.by_row(r)
        assert pos is not None
        resolved.append(resolve_position(item, pos))
    return resolved


def _split(mention: LlmMention) -> list[LlmMention]:
    """Слитое упоминание («20а и 32а по одному») → по одному на значение признака.
    Плюс объём и единица из текста, если LLM их перепутала («2 системы» → 1 шт)."""
    parts = split_by_features(mention.phrase)
    if parts is None:
        return [_qty_from_text(mention)]
    logger.debug("«{}» разделено на {} части", mention.phrase, len(parts))
    out = []
    for part in parts:
        qty = part.qty if part.qty is not None else mention.qty
        sub = mention.model_copy(update={"phrase": part.phrase, "qty": qty})
        out.append(_qty_from_text(sub))
    return out


def _qty_from_text(mention: LlmMention) -> LlmMention:
    found = find_quantities(mention.phrase)
    if len(found) != 1:
        return mention
    q = found[0]
    if mention.qty != q.value or normalize_unit(mention.unit) != q.unit:
        logger.debug(
            "«{}»: объём из текста {} {} вместо {} {}",
            mention.phrase,
            q.value,
            q.unit,
            mention.qty,
            mention.unit,
        )
    return mention.model_copy(update={"qty": q.value, "unit": q.unit})


def _missing_quantities(text: str, mentions: list[LlmMention]) -> list[str]:
    """«Объём + единица» из сообщения, которых не покрывает ни одно упоминание.

    Упоминание покрывает объёмы из своей фразы. Если во фразе объёма нет, а в полях
    qty/unit он есть (LLM вырезала «медь 6-ку», а 15 м положила в поля), — объём из полей.
    Иначе это ложная тревога и лишний вызов LLM.
    """
    found = Counter((q.value, q.unit) for q in find_quantities(text))
    covered: Counter[tuple[float, str]] = Counter()
    for m in mentions:
        own = find_quantities(m.phrase)
        if not own and m.qty is not None and m.unit:
            # тот же разбор, что и для текста, — единицы нормализуются одинаково
            own = find_quantities(f"{m.qty:g} {m.unit}")
        covered.update((q.value, q.unit) for q in own)
    missing = found - covered
    return [f"{v:g} {u}" for (v, u), n in missing.items() for _ in range(n)]
