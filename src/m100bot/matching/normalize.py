"""Нормализация текста фраз мастера и названий позиций.

Чистые функции без сети. Задача — привести «Д12.7», «д 12,7», «Tpyбoпpoвод» и т.п.
к единому виду, чтобы дальше сравнивать признаки и делать нечёткий поиск.
"""

import re

# Латинские буквы, похожие на кириллические (частый OCR-мусор в смете).
_LAT_TO_CYR = str.maketrans(
    {
        "a": "а",
        "c": "с",
        "e": "е",
        "o": "о",
        "p": "р",
        "x": "х",
        "y": "у",
        "k": "к",
        "m": "т",  # «omвepcmuй» → «отверстий» (рукописная т)
        "u": "и",  # «Сберленuе» → «сверление»
        "b": "в",
        "h": "н",
        "t": "т",
    }
)
_CYR_TO_LAT = str.maketrans(
    {
        "а": "a",
        "с": "c",
        "е": "e",
        "о": "o",
        "р": "p",
        "х": "x",
        "у": "y",
        "к": "k",
        "т": "t",
        "в": "b",
        "н": "h",
        "м": "m",
    }
)
_LAT_LOOKALIKE = set("aceopxykmubht")
_CYR_LOOKALIKE = set("асеорхуктвнм")
_CYR = re.compile(r"[а-яё]")
_LAT = re.compile(r"[a-z]")
_WORD = re.compile(r"[a-zа-яё]+")

UNIT_ALIASES: dict[str, str] = {
    "м": "м",
    "м.": "м",
    "метр": "м",
    "метра": "м",
    "метров": "м",
    "мп": "м",
    "пм": "м",
    "шт": "шт",
    "шт.": "шт",
    "штук": "шт",
    "штуки": "шт",
    "штука": "шт",
    "кг": "кг",
    "килограмм": "кг",
    "кило": "кг",
    "компл": "компл",
    "компл.": "компл",
    "комплект": "компл",
    "сист": "сист",
    "сист.": "сист",
    "система": "сист",
    "систем": "сист",
    "системы": "сист",
}


def _fix_mixed_word(word: str) -> str:
    """Слово со смешанными алфавитами приводим к одному алфавиту.

    Если есть кириллическая буква без латинского двойника (б, п, д, й…) — слово русское:
    «tpyбoпpoвод» → «трубопровод». Если есть латинская без кириллического двойника
    (d, i, n…) — слово латинское: «mdcaс» (кириллическая «с» в модели) → «mdcac».
    Иначе — по большинству букв. Чистые слова не трогаем.
    """
    cyr, lat = set(_CYR.findall(word)), set(_LAT.findall(word))
    if not (cyr and lat):
        return word
    if cyr - _CYR_LOOKALIKE:
        return word.translate(_LAT_TO_CYR)
    if lat - _LAT_LOOKALIKE:
        return word.translate(_CYR_TO_LAT)
    n_cyr, n_lat = len(_CYR.findall(word)), len(_LAT.findall(word))
    return word.translate(_LAT_TO_CYR) if n_cyr >= n_lat else word.translate(_CYR_TO_LAT)


# Типичные опечатки сметы
_TYPOS = [(re.compile(r"сберлен", re.IGNORECASE), "сверлен")]


def fix_typos(text: str) -> str:
    for rx, fixed in _TYPOS:
        text = rx.sub(fixed, text)
    return text


def normalize_text(text: str) -> str:
    """Нижний регистр, ё→е, исправление смешанной раскладки, единый вид чисел и размеров."""
    t = text.lower().replace("ё", "е")
    t = _WORD.sub(lambda m: _fix_mixed_word(m.group(0)), t)
    t = fix_typos(t)
    # десятичная запятая между цифрами → точка: 6,35 → 6.35
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    # «3 на 2.5», «3x2.5», «3*2.5», «3 х 2.5» → «3х2.5»
    t = re.sub(r"(?<=\d)\s*(?:на|x|\*|×|х)\s*(?=\d)", "х", t)
    # схлопываем пробелы
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Формы, которые не перечислить: «метрами», «штуками», «системах», «килограммов»
_UNIT_PREFIXES = (
    ("метр", "м"),
    ("шт", "шт"),
    ("сист", "сист"),
    ("компл", "компл"),
    ("килограм", "кг"),
)


def normalize_unit(unit: str | None) -> str | None:
    """Единица из таблицы, от LLM или из текста мастера → м, шт, кг, компл, сист, м2, м3."""
    if unit is None:
        return None
    u = unit.strip().lower().replace("ё", "е")
    if not u:
        return None
    if u in UNIT_ALIASES:
        return UNIT_ALIASES[u]
    u = u.rstrip(".")
    if u in UNIT_ALIASES:
        return UNIT_ALIASES[u]
    for prefix, canonical in _UNIT_PREFIXES:
        if u.startswith(prefix):
            return canonical
    return u


def parse_number(value: object) -> float | None:
    """Число из ячейки или текста: 2.35, '2,35', '56 м' → float; пусто/мусор → None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


# ─────────────── Короткое название позиции ───────────────

# Шаблонный текст спецификаций, который не различает позиции: стандарты, горючесть,
# электрические параметры, напряжение кабеля. Это общие правила, а не список позиций.
# Различающие признаки (Д, Ф, сечение, Iр, размеры, модели, б=) не трогаем —
# tests/test_llm.py проверяет, что признаки короткого названия совпадают с полным.
_NOISE_RX = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"марка\s+меди\s+\S+",
        r"\bпо\s+(?:EN|ГОСТ|ТУ|DIN|ISO)\s*[\w.\-–]+",
        r"(?:класс|групп)\w*\s+горючести\s+[ГгKК]\d",
        r"\bс\s+(?:[а-я]+\s+)?(?:и\s+[а-я]+\s+)?расцепител\w*",
        r"\b(?:G|H|N|U[нn]?|I[нn])\s*=\s*[\d.,]+\s*[^\s,]*",
        r"\bхарактеристик\w*\s*\"?[A-ZА-Я]\"?",
        r"(?<=[\s,])\dп\b",
        r"\bемкость\s+[\d,.]+\s*л",
        r"(?<![/\d])\b\d{3}\s*В\b",
        r"\s*-\s*0[.,]66:?",
        r"\bсечением,?(?:\s*мм²)?",
        r"\bиз\s+вспененного\s+полиэтилена",
        r"\bс\s+медными\s+жилами\b",
    )
]
# Слово из букв со смешанными алфавитами — OCR-мусор («Tpyбoпpoвод», «Сберленuе»).
# Только буквы и от 4 символов: «Iр=16А» и модели с цифрами («MDCAС4I») не трогаем.
_MIXED_WORD = re.compile(
    r"(?<![\w-])(?=[a-zA-Zа-яА-ЯёЁ]{4,}(?![\w-]))"
    r"[a-zA-Zа-яА-ЯёЁ]*(?:[a-zA-Z][а-яА-ЯёЁ]|[а-яА-ЯёЁ][a-zA-Z])[a-zA-Zа-яА-ЯёЁ]*"
)


def compact_name(name: str) -> str:
    """Короткое название: без шаблонного текста и с исправленным OCR-мусором.

    Идёт в промпт LLM (меньше токенов) и в тексты бота. У позиции считается один раз:
    `Position.short_name`.
    """
    s = name
    for rx in _NOISE_RX:
        s = rx.sub(" ", s)
    s = _MIXED_WORD.sub(lambda m: normalize_text(m.group(0)), s)
    s = fix_typos(s)
    s = re.sub(r"\s*,\s*(?:,\s*)+", ", ", s)
    s = re.sub(r"\s+([,.:;])", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:")
    return re.sub(r"\s+шт$", "", s)  # «… Aspen шт» — ед. изм. и так в отдельной колонке
