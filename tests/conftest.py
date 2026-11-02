from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml

from m100bot.sheets.catalog import Catalog, parse_catalog

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden" / "phrases.yaml"


def load_xlsx_rows(path: Path) -> list[list[Any]]:
    """Те же «сырые» строки, что вернёт gspread get_values(UNFORMATTED_VALUE)."""
    ws = openpyxl.load_workbook(path).active
    return [list(r) for r in ws.iter_rows(values_only=True)]


@pytest.fixture(scope="session")
def sample_rows() -> list[list[Any]]:
    return load_xlsx_rows(FIXTURES / "sample.xlsx")


@pytest.fixture(scope="session")
def catalog(sample_rows: list[list[Any]]) -> Catalog:
    return parse_catalog(sample_rows)


def load_golden() -> list[dict]:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
