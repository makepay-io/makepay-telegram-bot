import json
from pathlib import Path

import pytest

from makepay_telegram_bot.catalog import CatalogError, load_catalog


def write_catalog(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loads_and_normalizes_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    write_catalog(
        path,
        [
            {
                "id": "coffee",
                "name": "Coffee voucher",
                "description": "A demo product",
                "amount": "5",
                "fiatCurrency": "usd",
            }
        ],
    )

    product = load_catalog(path)["coffee"]

    assert product.amount == "5.00"
    assert product.fiat_currency == "USD"


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"id": "a", "name": "", "description": "Desc", "amount": "1"}],
        [{"id": "a", "name": "Name", "description": "Desc", "amount": 1}],
        [{"id": "UPPER CASE", "name": "Name", "description": "Desc", "amount": "5"}],
        [{"id": "a", "name": "Name", "description": "Desc", "amount": "-1"}],
        [{"id": "a", "name": "Name", "description": "Desc", "amount": "1.001"}],
        [
            {"id": "a", "name": "Name", "description": "Desc", "amount": "1"},
            {"id": "a", "name": "Again", "description": "Desc", "amount": "2"},
        ],
    ],
)
def test_rejects_invalid_catalog(tmp_path: Path, value: object) -> None:
    path = tmp_path / "catalog.json"
    write_catalog(path, value)

    with pytest.raises(CatalogError):
        load_catalog(path)


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="Unable to read"):
        load_catalog(tmp_path / "missing.json")
