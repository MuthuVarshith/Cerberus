from rate_calculator import calculate_rate


def test_zero_total_returns_zero():
    # Zero total should safely return 0.0 rather than raising ZeroDivisionError.
    assert calculate_rate(10, 0) == 0.0
