from datetime import date

from m100bot.sheets.catalog import Catalog


def test_positions_count(catalog: Catalog) -> None:
    # позиции без разделов, подзаголовков групп и итогов
    assert len(catalog.positions) == 62


def test_skips_headers_and_totals(catalog: Catalog) -> None:
    names = [p.name for p in catalog.positions]
    assert not any(
        n.startswith(("ИТОГО", "Всего", "Материалы для", "Дренаж системы")) for n in names
    )
    assert not any(n.startswith("Крепежные") for n in names)


def test_sections_and_groups(catalog: Catalog) -> None:
    copper = catalog.by_row(17)
    assert copper and copper.section == "ОВ 3" and copper.group == "Материалы для сист К1-К7"
    cable = catalog.by_row(55)
    assert cable and cable.section == "ЭМ3"


def test_units_normalized(catalog: Catalog) -> None:
    assert catalog.by_row(55).unit == "м"  # в таблице «М»
    assert catalog.by_row(50).unit == "компл"
    assert catalog.by_row(51).unit == "сист"
    assert catalog.by_row(28).planned == 2.35


def test_last_row_without_number(catalog: Catalog) -> None:
    p = catalog.by_row(71)
    assert p and p.number is None and "скобах" in p.name


def test_date_columns_and_duplicates(catalog: Catalog) -> None:
    assert catalog.column_for(date(2026, 8, 19)) == 10  # J
    assert catalog.column_for(date(2026, 8, 22)) is None  # пропущенный день
    assert catalog.duplicate_dates() == {date(2026, 9, 30): [48, 49]}  # AV, AW
    assert catalog.last_col == 49
