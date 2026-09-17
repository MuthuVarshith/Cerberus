import pytest

from intervals import contains, merge, total_length


def test_merge_overlapping():
    assert merge([(1, 3), (2, 4)]) == [(1, 4)]


def test_merge_disjoint():
    assert merge([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]


def test_merge_zero_length_interval():
    assert merge([(2, 2)]) == [(2, 2)]


def test_merge_rejects_inverted_interval():
    with pytest.raises(ValueError):
        merge([(3, 1)])


def test_total_length():
    assert total_length([(1, 3), (2, 4)]) == 3


def test_contains_inside():
    assert contains([(1, 5)], 3)
