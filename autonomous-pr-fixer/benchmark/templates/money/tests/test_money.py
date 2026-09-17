from decimal import Decimal

import pytest

from money import parse_amount, split


def test_parse_plain():
    assert parse_amount("12.5") == Decimal("12.50")


def test_parse_dollar_sign():
    assert parse_amount("$12") == Decimal("12.00")


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_amount("twelve")


def test_split_even():
    assert split(Decimal("9.00"), 3) == [Decimal("3.00")] * 3


def test_split_rejects_zero_parts():
    with pytest.raises(ValueError):
        split(Decimal("1.00"), 0)
