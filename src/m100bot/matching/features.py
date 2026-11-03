"""Извлечение различающих признаков из названий позиций.

Похожие позиции отличаются одним числом: диаметром (Д6,35 / Д9,52 / Д12.7), сечением кабеля
(3х2,5 / 3х1,5 / 4х1,5), током автомата (16 / 20 / 32 А), размером короба (150х60 / 90х60).
Валидатор в matcher.py сверяет эти признаки с тем, что назвал мастер.
"""

import re

from pydantic import BaseModel

from m100bot.matching.normalize import normalize_text


class Features(BaseModel):
    diameters: list[float] = []  # Д6.35, Ф9, Д25 (мм)
    thickness: list[float] = []  # толщина стенки изоляции «б=9мм»
    sections: list[str] = []  # сечение кабеля «3х2.5»
    current_a: float | None = None  # ток расцепителя Iр=16А
    sizes: list[str] = []  # габариты «150х60», «25х16»
    models: list[str] = []  # модели оборудования «mdoc-18hfn8»


_DIAM = re.compile(r"(?:\bд|\bф)\s*=?\s*(\d+(?:\.\d+)?)")
_CABLE = re.compile(r"(?<!\d)([2-5])х(\d+(?:\.\d+)?)(?![\d.])")
_CURRENT = re.compile(r"i[рp]\s*=\s*(\d+(?:\.\d+)?)\s*а")
_SIZE = re.compile(r"(?<!\d)(\d{2,3})х(\d{2,3})(?!\d)")
_MODEL = re.compile(r"\b(md[a-zа-я0-9]*-\d+[a-zа-я0-9]*)", re.IGNORECASE)
_OUTER_DIAM = re.compile(r"наружным диаметром\s*(\d+)\s*мм")
_THICKNESS = re.compile(r"\bб\s*=\s*(\d+(?:\.\d+)?)\s*мм")


def extract_features(name: str) -> Features:
    t = normalize_text(name)
    is_cable = "кабел" in t
    f = Features()

    f.diameters = [float(x) for x in _DIAM.findall(t)]
    if m := _OUTER_DIAM.search(t):
        f.diameters.append(float(m.group(1)))

    f.thickness = [float(x) for x in _THICKNESS.findall(t)]

    if is_cable:
        # «сечением 3х2.5-0.66» — 0.66 кВ это напряжение, не сечение
        f.sections = [f"{a}х{b}" for a, b in _CABLE.findall(t)]
    else:
        f.sizes = [f"{a}х{b}" for a, b in _SIZE.findall(t)]

    if m := _CURRENT.search(t):
        f.current_a = float(m.group(1))

    f.models = [m.lower() for m in _MODEL.findall(name)]
    return f


# ─────────────── Признаки из разговорной фразы мастера ───────────────

# Дюймовые размеры медных труб → мм
INCH_TO_MM = {"1/4": 6.35, "3/8": 9.52, "1/2": 12.7, "5/8": 15.88, "3/4": 19.05}
# «6-ка», «девятка», «двенашка» — номинал медной трубы → точный диаметр
_SLANG_NOMINAL = {6: 6.35, 9: 9.52, 12: 12.7}
SLANG_WORDS = [
    (re.compile(r"\bшестер\w*"), 6),
    (re.compile(r"\bдевят(?:к|очк)\w*"), 9),
    (re.compile(r"\bдвена(?:шк|дцатк)\w*"), 12),
]
# «6-ка», «25-й», «76-е», «80-х», «12-ю», «20-ку»
_NUM_SUFFIX = re.compile(r"(?<![\d.])(\d{1,3})-[а-я]{1,3}\b")
FRACTION = re.compile(r"(?<![\d./])(1/4|3/8|1/2|5/8|3/4)(?![\d/])")
# явная десятичная: «12.7», «9.52» — но не объём «0.5 кг», «8.5 м»
_DECIMAL = re.compile(r"(?<![\d.х])(\d{1,2}\.\d{1,2})(?![\d.х])(?!\s*(?:м\b|кг|шт|л\b|м2|м3))")
_MM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*мм\b")
_PHRASE_CURRENT = [
    re.compile(r"(?<![\d.])(\d{1,3})\s*(?:а|ампер\w*)\b"),
    re.compile(r"\bна\s+(\d{1,3})\b(?![.х/\-]\d|\s*(?:мм|м\b|метр|шт|кг|дюйм))"),
]
# Слова-числа, которыми называют сечение кабеля: «тройку на полтора», «три на четыре»
CABLE_WORDS = [
    (re.compile(r"\bполторашк\w*|\bполтора\b"), "1.5"),
    (re.compile(r"\bдва с половиной\b"), "2.5"),
    (re.compile(r"\b(?:двушк|двойк)\w*|\bдва\b|\bдве\b"), "2"),
    (re.compile(r"\bтройк\w*|\bтри\b"), "3"),
    (re.compile(r"\bчетверк\w*|\bчетыре\b"), "4"),
    (re.compile(r"\bпятерк\w*|\bпять\b"), "5"),
    (re.compile(r"\bшесть\b"), "6"),
]
# «2 1.5» после замены слов («двушку полтора»); жилы и сечение — обязательно через разделитель
_PHRASE_SECTION = re.compile(r"(?<![\d.])([2-5])(?:\s*(?:х|на)\s*|\s+)(1\.5|2\.5)(?![\d.х])")
_CROSS_ONLY = re.compile(r"\bполтор")


class PhraseFeatures(BaseModel):
    """Признаки из фразы мастера. Отличия от признаков позиции:
    - dims_mm: «9 мм» без пояснения — это может быть и диаметр, и толщина стенки;
    - sections: «?х1.5», если названо только сечение жилы («полторашка»);
    - currents: все токи во фразе («20а и 32а»), у позиции ток один."""

    diameters: list[float] = []
    dims_mm: list[float] = []
    sections: list[str] = []
    currents: list[float] = []
    sizes: list[str] = []
    models: list[str] = []


def _uniq(values: list) -> list:
    return list(dict.fromkeys(values))


def extract_phrase_features(phrase: str) -> PhraseFeatures:
    """Признаки из того, что сказал мастер: «медь 6-ку», «кабель 3 на полтора», «автомат на 16»."""
    t = normalize_text(phrase)
    f = PhraseFeatures()

    # диаметры: явные «д25»/«ф9», дюймы, жаргон и десятичные
    diams = [float(x) for x in _DIAM.findall(t)]
    diams += [INCH_TO_MM[x] for x in FRACTION.findall(t)]
    for rx, nominal in SLANG_WORDS:
        if rx.search(t):
            diams.append(_SLANG_NOMINAL[nominal])
    for x in _NUM_SUFFIX.findall(t):
        n = int(x)
        diams.append(_SLANG_NOMINAL.get(n, float(n)))
    diams += [float(x) for x in _DECIMAL.findall(t)]
    f.diameters = _uniq(diams)
    f.dims_mm = _uniq([float(x) for x in _MM.findall(t)])

    # ток автомата: «16а», «25 ампер», «на 16»; «20а и 32а» — оба
    for rx in _PHRASE_CURRENT:
        found = _uniq([float(x) for x in rx.findall(t)])
        if found:
            f.currents = found
            break

    # сечение кабеля: слова-числа → цифры, затем «3х1.5» / «2 1.5»
    tc = t
    for rx, digit in CABLE_WORDS:
        tc = rx.sub(digit, tc)
    tc = normalize_text(tc)  # «3 на 1.5» → «3х1.5»
    f.sections = _uniq([f"{a}х{b}" for a, b in _CABLE.findall(tc)])
    f.sections += [
        s for s in (f"{a}х{b}" for a, b in _PHRASE_SECTION.findall(tc)) if s not in f.sections
    ]
    if not f.sections and _CROSS_ONLY.search(t):
        f.sections = ["?х1.5"]

    f.sizes = _uniq([f"{a}х{b}" for a, b in _SIZE.findall(t)])
    f.models = [m.lower() for m in _MODEL.findall(normalize_text(phrase))]
    return f
