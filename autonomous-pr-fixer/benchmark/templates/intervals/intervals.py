from typing import List, Tuple

Interval = Tuple[int, int]


def merge(intervals: List[Interval]) -> List[Interval]:
    """Merge overlapping or touching closed intervals. The result is sorted by start."""
    result: List[Interval] = []
    for start, end in sorted(intervals):
        if start > end:
            raise ValueError(f"invalid interval ({start}, {end})")
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def total_length(intervals: List[Interval]) -> int:
    """Total length covered by the intervals, counting overlaps once."""
    return sum(end - start for start, end in merge(intervals))


def contains(intervals: List[Interval], point: int) -> bool:
    """Whether any closed interval contains the point."""
    return any(start <= point <= end for start, end in merge(intervals))
