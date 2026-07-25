from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import Product

_PRODUCT_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_FIAT_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class CatalogError(ValueError):
    """Raised when the product catalog is invalid."""


def _text(item: dict[str, Any], key: str, *, maximum: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"Catalog field {key!r} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise CatalogError(f"Catalog field {key!r} is longer than {maximum} characters.")
    return normalized


def _amount(item: dict[str, Any]) -> str:
    value = item.get("amount")
    if not isinstance(value, str):
        raise CatalogError("Catalog field 'amount' must be a decimal string.")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise CatalogError("Catalog field 'amount' must be a decimal string.") from error
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount <= 0 or not isinstance(exponent, int) or exponent < -2:
        raise CatalogError("Catalog amount must be positive with at most two decimal places.")
    return f"{amount:.2f}"


def load_catalog(path: Path) -> dict[str, Product]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CatalogError(f"Unable to read catalog at {path}.") from error
    except json.JSONDecodeError as error:
        raise CatalogError(f"Catalog at {path} is not valid JSON.") from error

    if not isinstance(decoded, list) or not decoded:
        raise CatalogError("Catalog must be a non-empty JSON array.")

    products: dict[str, Product] = {}
    for raw in decoded:
        if not isinstance(raw, dict):
            raise CatalogError("Every catalog item must be an object.")
        product_id = _text(raw, "id", maximum=32).lower()
        if not _PRODUCT_ID_PATTERN.fullmatch(product_id):
            raise CatalogError(
                "Catalog product IDs may contain only lowercase letters, numbers, _ and -."
            )
        if product_id in products:
            raise CatalogError(f"Duplicate catalog product ID: {product_id}.")
        fiat_currency = str(raw.get("fiatCurrency", "USD")).strip().upper()
        if not _FIAT_CURRENCY_PATTERN.fullmatch(fiat_currency):
            raise CatalogError("fiatCurrency must be a three-letter ISO currency code.")
        products[product_id] = Product(
            id=product_id,
            name=_text(raw, "name", maximum=80),
            description=_text(raw, "description", maximum=240),
            amount=_amount(raw),
            fiat_currency=fiat_currency,
        )
    return products
