"""Hidden specification tests for money. Never shown to Cerberus or to patch sources."""
from decimal import Decimal

import pytest

from money import parse_amount, split


@pytest.mark.parametrize("text,expected", [
    ("12.5", "12.50"),
    ("$12", "12.00"),
    ("1,234.50", "1234.50"),
    ("$1,000,000", "1000000.00"),
    ("-3.5", "-3.50"),
    ("2.675", "2.68"),
    ("2.665", "2.66"),
    ("  7 ", "7.00"),
])
def test_parse_amount_spec(text, expected):
    assert parse_amount(text) == Decimal(expected)


def test_parse_amount_invalid():
    with pytest.raises(ValueError):
        parse_amount("1.2.3")


@pytest.mark.parametrize("total,parts", [
    ("10.00", 3), ("0.10", 4), ("0.05", 4), ("100.00", 7), ("0.00", 3), ("1.00", 1), ("0.99", 5),
])
def test_split_spec(total, parts):
    amounts = split(Decimal(total), parts)
    assert len(amounts) == parts
    assert sum(amounts) == Decimal(total)
    assert max(amounts) - min(amounts) <= Decimal("0.01")
