from rate_calculator import calculate_rate


def test_simple_rate():
    assert calculate_rate(10, 2) == 5.0


def test_fractional_rate():
    assert calculate_rate(1, 4) == 0.25
