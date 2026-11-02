import pytest

from m100bot.matching.normalize import normalize_text
from m100bot.sheets.catalog import Catalog


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Кабель 3 на 2,5", "кабель 3х2.5"),
        ("3x1,5", "3х1.5"),
        ("Tpyбoпpoвод дренажныи", "трубопровод дренажныи"),
        ("Сберленuе omвepcmuй Д80", "сверление отверстий д80"),
        ("Медь  шестёрка", "медь шестерка"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_copper_diameters(catalog: Catalog) -> None:
    assert [catalog.by_row(r).features.diameters for r in (17, 18, 19)] == [[6.35], [9.52], [12.7]]
    assert [catalog.by_row(r).features.diameters for r in (20, 21, 22)] == [[6.35], [9.52], [12.7]]


def test_cable_sections(catalog: Catalog) -> None:
    got = [catalog.by_row(r).features.sections for r in range(55, 61)]
    assert got == [["3х2.5"], ["3х1.5"], ["4х1.5"], ["3х4"], ["5х6"], ["2х1.5"]]


def test_breaker_current(catalog: Catalog) -> None:
    assert [catalog.by_row(r).features.current_a for r in (61, 62, 63)] == [16, 20, 32]


def test_sizes_and_pvc_pipes(catalog: Catalog) -> None:
    assert catalog.by_row(29).features.sizes == ["150х60"]
    assert catalog.by_row(67).features.sizes == ["25х16"]
    assert catalog.by_row(64).features.diameters == [20]
    assert catalog.by_row(65).features.diameters == [25]


def test_models(catalog: Catalog) -> None:
    assert catalog.by_row(10).features.models == catalog.by_row(15).features.models
    assert catalog.by_row(5).features.models == ["mdoc-18hfn8"]


# ─────────────── Признаки из фразы мастера ───────────────

from m100bot.matching.features import extract_phrase_features  # noqa: E402


@pytest.mark.parametrize(
    ("phrase", "diameters"),
    [
        ("шестёрка", [6.35]),
        ("проложили шестёрку 20 метров", [6.35]),
        ("девятка", [9.52]),
        ("двенашки 45", [12.7]),
        ("медь 6-ка 15 м", [6.35]),
        ("1/2 дюйма", [12.7]),
        ("трасса 1/4 и 3/8 по 14 метров", [6.35, 9.52]),
        ("медная труба 9,52 — 8 метров", [9.52]),
        ("гильзы под фреон 76-е", [76.0]),
        ("гофру 20-ку 100 метров", [20.0]),
        ("дозаправили фреоном 0,5 кг", []),  # объём, а не диаметр
        ("шпилек 5", []),
    ],
)
def test_phrase_diameters(phrase: str, diameters: list[float]) -> None:
    assert extract_phrase_features(phrase).diameters == diameters


def test_phrase_mm_is_not_diameter() -> None:
    f = extract_phrase_features("кифлекс 9 мм 10 метров")
    assert f.diameters == [] and f.dims_mm == [9.0]


@pytest.mark.parametrize(
    ("phrase", "sections"),
    [
        ("3 на полтора", ["3х1.5"]),
        ("кабель 3 на 2,5 — 56 м", ["3х2.5"]),
        ("тройку на полтора протянули 16 метров", ["3х1.5"]),
        ("кабель три на четыре 20 метров", ["3х4"]),
        ("двушку полтора 10 м", ["2х1.5"]),
        ("4х1.5 24м", ["4х1.5"]),  # «24» — не сечение 2х4
        ("кабель полторашку 30 м", ["?х1.5"]),
        ("кабель-канал 25 на 16", []),  # это размер короба
    ],
)
def test_phrase_sections(phrase: str, sections: list[str]) -> None:
    assert extract_phrase_features(phrase).sections == sections


@pytest.mark.parametrize(
    ("phrase", "current"),
    [
        ("на 16", 16.0),
        ("поставили автомат на 16", 16.0),
        ("автоматы 20А", 20.0),
        ("автомат на 25 ампер 1 шт", 25.0),
        ("медь на 1/2 дюйма 10 м", None),
        ("кабель на 16 метров", None),
    ],
)
def test_phrase_current(phrase: str, current: float | None) -> None:
    assert extract_phrase_features(phrase).current_a == current


def test_phrase_sizes_and_models() -> None:
    assert extract_phrase_features("короб 150 на 60 — 6 м").sizes == ["150х60"]
    assert extract_phrase_features("внутренний блок MDSAI2-07 один").models == ["mdsai2-07"]


def test_insulation_thickness(catalog: Catalog) -> None:
    assert [catalog.by_row(r).features.thickness for r in (20, 21, 22)] == [[9.0]] * 3
    assert catalog.by_row(17).features.thickness == []
