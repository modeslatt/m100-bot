"""Разбор фразы мастера кодом, без LLM: объёмы, слова, деление на части.

LLM на CPU (qwen3:8b) путает объёмы и сливает упоминания. Всё, что видно в тексте явно
(«2 системы», «20А и 32А по одному»), надёжнее достать регулярными выражениями.
"""

import re
from dataclasses import dataclass

from m100bot.matching.features import CABLE_WORDS, extract_phrase_features
from m100bot.matching.normalize import normalize_text, normalize_unit

# ─────────────── Объём с единицей ───────────────

_UNIT = (
    r"(?P<unit>метр\w*|мп|м2|м3|м(?![а-яa-z0-9])|шт\w*|штук\w*|кг|килограм\w*"
    r"|сист\w*|компл\w*)"
)
_NUM = r"(?P<num>\d+(?:\.\d+)?)"
# «15 м», «24м», «2 системы»; число не часть размера («3х2.5») и не дробь («1/2»)
_QTY = re.compile(rf"(?<![\d.х/\-]){_NUM}\s*{_UNIT}")
_PER_ONE = re.compile(r"\bпо\s+(?:одному|одной|одна|один|1)\b")
_PER_N = re.compile(rf"\bпо\s+{_NUM}\b")


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str  # нормализованная: м, шт, кг, сист, компл, м2, м3
    start: int
    end: int


def _unit(raw: str) -> str:
    if raw in ("м2", "м3"):
        return raw
    for prefix, unit in (("метр", "м"), ("шт", "шт"), ("сист", "сист"), ("компл", "компл")):
        if raw.startswith(prefix):
            return unit
    if raw.startswith("килограм"):
        return "кг"
    return normalize_unit(raw) or raw


def find_quantities(text: str) -> list[Quantity]:
    """Все «число + единица» в нормализованном тексте."""
    t = normalize_text(text)
    return [
        Quantity(float(m.group("num")), _unit(m.group("unit")), m.start(), m.end())
        for m in _QTY.finditer(t)
    ]


def distributive_qty(text: str) -> float | None:
    """«по одному» → 1, «по 14 метров» → 14 (один объём на все перечисленные позиции)."""
    t = normalize_text(text)
    if _PER_ONE.search(t):
        return 1.0
    if m := _PER_N.search(t):
        return float(m.group("num"))
    return None


# ─────────────── Слова ───────────────

_WORD = re.compile(r"[a-zа-я]+")
STOPWORDS = {"на", "в", "с", "по", "и", "для", "от", "под", "из", "к", "до", "за", "у", "о"}


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(normalize_text(text)) if w not in STOPWORDS]


# ─────────────── Деление слитого упоминания ───────────────

# «20а», «32 ампер», «на 16» — ток; дроби, жаргон и «6-ку» — диаметр; «4х1.5» — сечение
_SPLIT_PATTERNS = [
    re.compile(r"(?<![\d.])\d{1,3}\s*(?:а|ампер\w*)\b"),
    re.compile(r"(?<![\d./])(?:1/4|3/8|1/2|5/8|3/4)(?![\d/])"),
    re.compile(r"\bшестер\w*|\bдевят(?:к|очк)\w*|\bдвена(?:шк|дцатк)\w*"),
    re.compile(r"(?<![\d.])\d{1,2}-[а-я]{1,3}\b"),
    re.compile(r"(?<![\d.])[2-5]х\d+(?:\.\d+)?(?![\d.])"),
]


def _cable_words_to_digits(t: str) -> str:
    for rx, digit in CABLE_WORDS:
        t = rx.sub(digit, t)
    return normalize_text(t)


@dataclass(frozen=True)
class Part:
    phrase: str
    qty: float | None


def split_by_features(phrase: str) -> list[Part] | None:
    """«автоматы 20а и 32а по одному» → [«автоматы 20а», «автоматы 32а»] с объёмами.

    Делим, только если в упоминании ≥2 разных значения одного признака. Объём части —
    «число + единица» или голое число в её отрезке; если его нет — общий «по N».
    None — делить нечего.
    """
    t = _cable_words_to_digits(normalize_text(phrase))
    for rx in _SPLIT_PATTERNS:
        matches = list(rx.finditer(t))
        if len(matches) < 2:
            continue
        values = {_feature_key(m.group(0)) for m in matches}
        if len(values) < 2:
            continue
        prefix = t[: matches[0].start()]
        per_all = distributive_qty(t)
        parts = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
            segment = t[m.start() : end]
            tail = segment[len(m.group(0)) :]
            qty = _segment_qty(tail)
            parts.append(
                Part((prefix + segment).strip(" ,;и"), qty if qty is not None else per_all)
            )
        return parts
    return None


def _feature_key(token: str) -> str:
    f = extract_phrase_features(token)
    return repr((f.diameters, f.sections, f.currents))


def _segment_qty(tail: str) -> float | None:
    """Объём в хвосте отрезка: «15 м» или голое «24» (до следующего признака)."""
    if q := find_quantities(tail):
        return q[0].value
    if _PER_ONE.search(tail) or _PER_N.search(tail):
        return None  # это общий «по N» — его подставит вызывающий
    if m := re.search(r"(?<![\d.х/\-])(\d+(?:\.\d+)?)(?![\d.х/\-])", tail):
        return float(m.group(1))
    return None


# ─────────────── Голые числа ───────────────

_BARE = re.compile(r"(?<![\d./])(\d+(?:\.\d+)?)(?![\d/])")
_FRACTION = re.compile(r"(?<![\d./])\d/\d(?![\d/])")
_SQUARE = re.compile(r"\bм[23]\b")


def bare_numbers(phrase: str, exclude: set[float]) -> list[float]:
    """Числа во фразе, кроме объёма, дробей («1/2») и степени в «м2»."""
    t = _FRACTION.sub(" ", normalize_text(phrase))
    t = _SQUARE.sub(" ", t)
    out = []
    for x in _BARE.findall(t):
        v = float(x)
        if v not in exclude and v not in out:
            out.append(v)
    return out
