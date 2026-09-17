"""Hidden specification tests for intervals. Never shown to Cerberus or to patch sources."""
import pytest

from intervals import contains, merge, total_length


@pytest.mark.parametrize("given,expected", [
    ([], []),
    ([(1, 3), (2, 4)], [(1, 4)]),
    ([(1, 10), (2, 3)], [(1, 10)]),
    ([(5, 6), (1, 2)], [(1, 2), (5, 6)]),
    ([(1, 10), (2, 3), (12, 15)], [(1, 10), (12, 15)]),
    ([(1, 2), (2, 3)], [(1, 3)]),
    ([(2, 2)], [(2, 2)]),
    ([(8, 9), (1, 4), (3, 5)], [(1, 5), (8, 9)]),
])
def test_merge_spec(given, expected):
    assert merge(given) == expected


def test_merge_rejects_inverted():
    with pytest.raises(ValueError):
        merge([(1, 2), (5, 4)])


def test_total_length_spec():
    assert total_length([(1, 10), (2, 3), (20, 22)]) == 11


@pytest.mark.parametrize("point,expected", [(0, False), (1, True), (5, True), (6, False), (9, True)])
def test_contains_spec(point, expected):
    assert contains([(1, 5), (9, 9)], point) is expected
