from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import List


def parse_amount(text: str) -> Decimal:
    """Parse '1,234.50', '$12' or '-3.5' into a Decimal with two places (banker's rounding)."""
    cleaned = text.strip().replace(",", "").replace("$", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def split(total: Decimal, parts: int) -> List[Decimal]:
    """Split a non-negative total into `parts` amounts that differ by at most one cent and sum to total."""
    if parts <= 0:
        raise ValueError("parts must be positive")
    cents = int((total * 100).to_integral_value())
    base, remainder = divmod(cents, parts)
    return [Decimal(base + (1 if i < remainder else 0)) / 100 for i in range(parts)]
