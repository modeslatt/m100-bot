"""Гибридное сопоставление: синонимы → LLM → валидатор признаков и единиц.

Публичный интерфейс — `Matcher.match(text) -> list[MatchedItem]`. Его вызывает и бот,
и эталонный тест tests/test_golden.py.

LLM только предлагает кандидатов. Валидатор их не добавляет, а только отсеивает:
если после проверки признаков не осталось ни одного — NOT_FOUND, сами ничего не подставляем.
"""

import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any, Protocol

from loguru import logger

from m100bot.matching.features import PhraseFeatures, extract_phrase_features
from m100bot.matching.llm import compact_name
from m100bot.matching.models import LlmMention, LlmResponse, MatchedItem, MatchStatus
from m100bot.matching.normalize import normalize_text, normalize_unit
from m100bot.matching.phrase import (
    bare_numbers,
    find_quantities,
    split_by_features,
    words,
)
from m100bot.sheets.catalog import Catalog, Position

_TRAILING_QTY = re.compile(r"[\s—–-]*\d+(?:\.\d+)?\s*[а-я.]*\s*$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _name_numbers(pos: Position) -> set[float]:
    return {float(x) for x in _NUMBER.findall(pos.norm_name)}


def _numbers_in_name(spec: str, pos: Position) -> bool:
    """Все числа из «25х16» / «3х1.5» встречаются в названии позиции."""
    numbers = _name_numbers(pos)
    return all(float(x) in numbers for x in spec.split("х"))


class Extractor(Protocol):
    async def extract(self, text: str, catalog: Catalog) -> LlmResponse: ...


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


def features_conflict(pf: PhraseFeatures, pos: Position) -> str | None:
    """Почему позиция не подходит к признакам фразы; None — подходит.

    Признак фразы ограничивает только те позиции, у которых признак того же вида есть
    (диаметр «20» не мешает автомату, ток «16» не мешает трубе). Если во фразе названо
    несколько значений одного вида, достаточно совпадения с любым из них.
    """
    f = pos.features
    if (
        pf.diameters
        and f.diameters
        and not any(_same_size(p, d) for p in pf.diameters for d in f.diameters)
    ):
        return f"диаметр {pf.diameters} ≠ {f.diameters}"
    # «9 мм» — это может быть и диаметр, и толщина стенки изоляции
    dims = f.diameters + f.thickness
    if pf.dims_mm and dims and not any(_same_size(p, d) for p in pf.dims_mm for d in dims):
        return f"размер {pf.dims_mm} мм ≠ {dims}"
    if (
        pf.sections
        and f.sections
        and not any(_section_ok(p, s) for p in pf.sections for s in f.sections)
    ):
        return f"сечение {pf.sections} ≠ {f.sections}"
    if pf.currents and f.current_a is not None and f.current_a not in pf.currents:
        return f"ток {pf.currents} ≠ {f.current_a}"
    if pf.sizes and f.sizes and not set(pf.sizes) & set(f.sizes):
        return f"размер {pf.sizes} ≠ {f.sizes}"
    # Размер «25х16» и полное сечение «3х1.5» — это два числа сразу. Если у позиции такого
    # признака нет, она подходит, только если оба числа есть в её названии: «отвод 90° Д25»
    # для «90 на 25» — да, «труба Д25» для «короб 25х16» — нет.
    if pf.sizes and not f.sizes and not any(_numbers_in_name(s, pos) for s in pf.sizes):
        return f"размер {pf.sizes} не встречается в названии"
    full_sections = [s for s in pf.sections if not s.startswith("?")]
    if (
        full_sections
        and not f.sections
        and not any(_numbers_in_name(s, pos) for s in full_sections)
    ):
        return f"сечение {full_sections} не встречается в названии"
    if pf.models and f.models and not any(m.startswith(p) for p in pf.models for m in f.models):
        return f"модель {pf.models} ≠ {f.models}"
    return None


# ─────────────── Явные совпадения и слова фразы ───────────────


def _positive_tests(pf: PhraseFeatures, numbers: list[float]) -> list[tuple[str, Any]]:
    """Проверки «позиция явно подтверждает признак фразы» — по видам признаков."""
    tests: list[tuple[str, Any]] = []
    if pf.diameters:
        tests.append(
            (
                "диаметр",
                lambda p: any(_same_size(x, d) for x in pf.diameters for d in p.features.diameters),
            )
        )
    if pf.dims_mm:
        tests.append(
            (
                "мм",
                lambda p: any(
                    _same_size(x, d)
                    for x in pf.dims_mm
                    for d in p.features.diameters + p.features.thickness
                ),
            )
        )
    if pf.sections:
        tests.append(
            (
                "сечение",
                lambda p: any(
                    _section_ok(s, ps) for s in pf.sections for ps in p.features.sections
                ),
            )
        )
    if pf.sizes:
        tests.append(
            (
                "размер",
                lambda p: (
                    bool(set(pf.sizes) & set(p.features.sizes))
                    or any(_numbers_in_name(s, p) for s in pf.sizes)
                ),
            )
        )
    if pf.currents:
        tests.append(("ток", lambda p: p.features.current_a in pf.currents))
    if pf.models:
        tests.append(
            (
                "модель",
                lambda p: any(m.startswith(x) for x in pf.models for m in p.features.models),
            )
        )
    if numbers:
        tests.append(
            (
                "числа",
                lambda p: all(any(_same_size(n, x) for x in _name_numbers(p)) for n in numbers),
            )
        )
    return tests


def prefer_positive(
    pf: PhraseFeatures, numbers: list[float], survivors: list[Position]
) -> list[Position]:
    """Если признак фразы явно есть у части позиций — оставляем только их.

    «гофра 20-ка»: у трубы ПВХ Д20 диаметр 20 есть, у кабелей диаметра нет вовсе —
    кабели уходят. Если явного совпадения нет ни у кого, признак ничего не меняет.
    """
    for _kind, test in _positive_tests(pf, numbers):
        hit = [p for p in survivors if test(p)]
        if hit:
            survivors = hit
    return survivors


# Жаргон мастера, который называет тип позиции другим словом (как «шестёрка» в features.py)
TYPE_SLANG = {"трасс": "фре"}  # «трасса» — фреоновая трасса, т.е. фреонопровод


def _type_words(pos: Position) -> list[str]:
    """Первые три значимых слова короткого названия — «что это за позиция»."""
    return [w for w in words(compact_name(pos.name)) if len(w) >= 4][:3]


def _prefix3(word: str) -> str:
    for slang, prefix in TYPE_SLANG.items():
        if word.startswith(slang):
            return prefix
    return word[:3]


def names_type(text_words: list[str], pos: Position) -> int:
    """Сколько слов текста называют тип позиции (совпадение первых 3 букв)."""
    tw = {w[:3] for w in _type_words(pos)}
    return sum(1 for w in text_words if len(w) >= 4 and _prefix3(w) in tw)


def _word_hits(text_words: list[str], pos: Position) -> int:
    """Сколько слов фразы встречается в полном названии (4 буквы или короткое слово целиком)."""
    name = words(compact_name(pos.name))
    hits = 0
    for w in text_words:
        if len(w) >= 4:
            hits += any(nw.startswith(w[:4]) for nw in name)
        elif len(w) >= 2:
            hits += w in name
    return hits


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


# ─────────────── Ссылки «изоляцию на них столько же» ───────────────

_REFERENCE = re.compile(
    r"\b(?:на них|на обе|на оба|на нее|на него|на ту же|столько же|туда же|на них же)\b"
)


# ─────────────── Matcher ───────────────


class Matcher:
    def __init__(
        self,
        catalog: Catalog,
        llm: Extractor,
        synonyms: dict[str, int] | None = None,
        *,
        expand_siblings: bool = True,
    ) -> None:
        self.llm = llm
        self.expand_siblings = expand_siblings
        self.refresh(catalog, synonyms)

    def refresh(self, catalog: Catalog, synonyms: dict[str, int] | None = None) -> None:
        """Подставить свежий справочник и синонимы (после перечитывания таблицы).

        Бот живёт долго, а таблицу правят: без этого новые позиции появились бы
        только после перезапуска.
        """
        self.catalog = catalog
        # нормализованная фраза (без объёма) → строка
        self.synonyms = {normalize_text(k): v for k, v in (synonyms or {}).items()}
        # «варианты одного товара»: позиции с одинаковым первым словом и ед. изм.
        # (все кабели, все хомуты, все внутренние блоки). LLM часто находит товар,
        # но ошибается в варианте — нужный вариант код выбирает по признакам.
        self._groups: dict[tuple[str, str], list[Position]] = {}
        for p in catalog.positions:
            if key := self._group_key(p):
                self._groups.setdefault(key, []).append(p)

    @staticmethod
    def _group_key(pos: Position) -> tuple[str, str] | None:
        head = next((w for w in words(compact_name(pos.name)) if len(w) >= 4), None)
        return (head[:5], pos.unit) if head else None

    # ── сообщение целиком ──

    async def match(
        self, text: str, remaining: Mapping[int, float] | None = None
    ) -> list[MatchedItem]:
        """Сообщение мастера → позиции на подтверждение.

        remaining — остаток по смете (D − I) по номеру строки; если передан, отмечаем
        объёмы больше остатка (⚠️ в карточке). Ошибки LLM (LlmError) пробрасываются наверх.
        """
        if not normalize_text(text):
            return []
        mentions = await self._extract_complete(text)
        message_words = words(text)

        items: list[MatchedItem] = []
        for mention in mentions:
            for part in self._split(mention):
                row = self._synonym_row(part.phrase)
                items.append(self.validate(part, synonym_row=row, message_words=message_words))
        items = self._resolve_references(items)

        if remaining is not None:
            for item in items:
                if item.row is not None and item.qty is not None:
                    left = remaining.get(item.row)
                    item.over_remaining = left is not None and item.qty > left + 1e-9
        logger.info(
            "match: {} позиций: {}",
            len(items),
            ", ".join(f"{i.status}:{i.row or i.candidates}" for i in items),
        )
        return items

    async def _extract_complete(self, text: str) -> list[LlmMention]:
        """Вызов LLM + проверка, что ни один «объём с единицей» из сообщения не потерян.

        Если потерян — один раз переспрашиваем с подсказкой и берём более полный ответ.
        """
        response = await self.llm.extract(text, self.catalog)
        missing = _missing_quantities(text, response.mentions)
        if not missing:
            return response.mentions
        logger.warning("LLM пропустила объёмы {} — переспрашиваю", missing)
        hint = ", ".join(f"«{q}»" for q in missing)
        retry_text = (
            f"{text}\n\n(Проверка: в прошлом ответе пропущены упоминания с объёмами {hint}. "
            "Верни ВСЕ упоминания сообщения заново, каждое отдельно.)"
        )
        retry = await self.llm.extract(retry_text, self.catalog)
        if len(_missing_quantities(text, retry.mentions)) < len(missing):
            return retry.mentions
        return response.mentions

    def _split(self, mention: LlmMention) -> list[LlmMention]:
        """Слитое упоминание («20а и 32а по одному») → по одному на значение признака.
        Плюс объём и единица из текста, если LLM их перепутала («2 системы» → 1 шт)."""
        parts = split_by_features(mention.phrase)
        if parts is None:
            return [self._qty_from_text(mention)]
        logger.debug("«{}» разделено на {} части", mention.phrase, len(parts))
        out = []
        for part in parts:
            qty = part.qty if part.qty is not None else mention.qty
            sub = mention.model_copy(update={"phrase": part.phrase, "qty": qty})
            out.append(self._qty_from_text(sub))
        return out

    @staticmethod
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

    def _resolve_references(self, items: list[MatchedItem]) -> list[MatchedItem]:
        """«изоляцию на них столько же» → по позиции на каждый диаметр из предыдущих строк."""
        out: list[MatchedItem] = []
        for item in items:
            if item.status == MatchStatus.AMBIGUOUS and _REFERENCE.search(
                normalize_text(item.phrase)
            ):
                resolved = self._by_previous_diameters(item, out)
                if resolved:
                    out.extend(resolved)
                    continue
            out.append(item)
        return out

    def _by_previous_diameters(
        self, item: MatchedItem, previous: list[MatchedItem]
    ) -> list[MatchedItem]:
        diameters: list[float] = []
        for prev in previous:
            pos = self.catalog.by_row(prev.row) if prev.row is not None else None
            if pos and len(pos.features.diameters) == 1:
                d = pos.features.diameters[0]
                if d not in diameters:
                    diameters.append(d)
        picks: list[int] = []
        for d in diameters:
            rows = [
                r
                for r in item.candidates
                if (p := self.catalog.by_row(r))
                and any(_same_size(d, x) for x in p.features.diameters)
            ]
            if len(rows) != 1:
                return []
            picks.append(rows[0])
        if not picks:
            return []
        status = MatchStatus.OK if item.qty else MatchStatus.CLARIFY
        return [
            item.model_copy(update={"status": status, "row": r, "candidates": [], "message": None})
            for r in picks
        ]

    def _synonym_row(self, phrase: str) -> int | None:
        if not self.synonyms:
            return None
        key = normalize_text(phrase)
        for k in (key, _TRAILING_QTY.sub("", key)):
            if k in self.synonyms:
                return self.synonyms[k]
        return None

    # ── одно упоминание ──

    def validate(
        self,
        mention: LlmMention,
        synonym_row: int | None = None,
        message_words: list[str] | None = None,
    ) -> MatchedItem:
        """Проверить кандидатов LLM по признакам фразы и ед. изм.

        0 кандидатов → NOT_FOUND; ед. изм. не та → CLARIFY; >1 → AMBIGUOUS;
        1, но объём не назван → CLARIFY; иначе OK.
        """
        unit = normalize_unit(mention.unit)

        def item(status: MatchStatus, **kw: object) -> MatchedItem:
            return MatchedItem(
                phrase=mention.phrase, qty=mention.qty, unit=unit, status=status, **kw
            )

        synonym = self.catalog.by_row(synonym_row) if synonym_row is not None else None
        # синониму мастера доверяем без проверки признаков
        if synonym:
            survivors = [synonym]
        else:
            survivors = self._select(mention, message_words or words(mention.phrase))

        if not survivors:
            return item(MatchStatus.NOT_FOUND, message="❓ Не нашёл такой позиции в смете.")

        if unit is not None:
            same_unit = [p for p in survivors if p.unit == unit]
            if not same_unit:
                units = ", ".join(sorted({p.unit for p in survivors}))
                msg = (
                    f"❓ В смете эта позиция в «{units}», а указано «{unit}». Сколько в «{units}»?"
                )
                if len(survivors) == 1:
                    return item(MatchStatus.CLARIFY, row=survivors[0].row, message=msg)
                rows = [p.row for p in survivors]
                return item(MatchStatus.CLARIFY, candidates=rows, message=msg)
            survivors = same_unit

        if len(survivors) > 1:
            return item(
                MatchStatus.AMBIGUOUS,
                candidates=[p.row for p in survivors],
                message="❓ Подходит несколько позиций — выберите нужную.",
            )

        pos = survivors[0]
        if mention.qty is None or mention.qty <= 0:
            return item(MatchStatus.CLARIFY, row=pos.row, message=f"❓ Сколько ({pos.unit})?")
        return item(MatchStatus.OK, row=pos.row)

    def _select(self, mention: LlmMention, message_words: list[str]) -> list[Position]:
        """Кандидаты LLM → проверенные позиции.

        1. Кандидаты LLM + другие варианты того же товара (если expand_siblings).
        2. Отсев по признакам (диаметр, сечение, ток, размер, модель).
        3. Предпочтение позиций, где признак фразы есть явно.
        4. Размер без материала («шестёрку проложили») — все позиции с этим размером.
        5. Предпочтение позиций, которые фраза называет словами.
        """
        pf = extract_phrase_features(mention.phrase)
        exclude = {mention.qty} if mention.qty is not None else set()
        numbers = bare_numbers(mention.phrase, exclude)
        phrase_words = words(mention.phrase)

        llm_rows: list[Position] = []
        for row in dict.fromkeys(mention.candidates):
            pos = self.catalog.by_row(row)
            if pos is None:
                logger.warning("LLM вернула строку {}, которой нет в справочнике", row)
                continue
            llm_rows.append(pos)

        pool = list(llm_rows)
        if self.expand_siblings:
            for pos in llm_rows:
                key = self._group_key(pos)
                pool += self._groups.get(key, []) if key else []
        pool = list({p.row: p for p in pool}.values())

        survivors = []
        for pos in pool:
            if reason := features_conflict(pf, pos):
                logger.debug("«{}»: строка {} отсеяна: {}", mention.phrase, pos.row, reason)
                continue
            survivors.append(pos)
        survivors = prefer_positive(pf, numbers, survivors)

        if (
            pf.diameters
            and survivors
            and not any(
                names_type(phrase_words, p) or names_type(message_words, p) for p in survivors
            )
        ):
            survivors = self._same_size_positions(pf, survivors, normalize_unit(mention.unit))
            survivors = prefer_positive(pf, numbers, survivors)

        survivors = prefer_by_words(phrase_words, survivors)
        return sorted(survivors, key=lambda p: p.row)

    def _same_size_positions(
        self, pf: PhraseFeatures, survivors: list[Position], unit: str | None
    ) -> list[Position]:
        """Материал не назван ни во фразе, ни в сообщении: «девятку протянули» — это может
        быть медь, изоляция или капиллярная трубка. Все позиции с этим размером и той же ед.
        (ед. из фразы, а если не названа — ед. кандидатов LLM)."""
        units = {unit} if unit else {p.unit for p in survivors}
        extra = [
            p
            for p in self.catalog.positions
            if p.unit in units
            and any(_same_size(x, d) for x in pf.diameters for d in p.features.diameters)
        ]
        return list({p.row: p for p in survivors + extra}.values())


def _missing_quantities(text: str, mentions: list[LlmMention]) -> list[str]:
    """«Объём + единица» из сообщения, которых нет ни в одной фразе упоминаний."""
    found = Counter((q.value, q.unit) for q in find_quantities(text))
    covered: Counter[tuple[float, str]] = Counter()
    for m in mentions:
        covered.update((q.value, q.unit) for q in find_quantities(m.phrase))
    missing = found - covered
    return [f"{v:g} {u}" for (v, u), n in missing.items() for _ in range(n)]
