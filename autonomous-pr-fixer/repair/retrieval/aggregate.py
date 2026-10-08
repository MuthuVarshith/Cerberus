"""Roll chunk scores up to a file ranking (localization is judged per file).

- ``max``: a file scores as its best chunk.
- ``sum_top3``: sum of a file's three best chunk scores (rewards files with several relevant units).
- ``rrf`` rankings use ranks, not scores; see ``repair.retrieval.fusion``.
Ties are broken by path so rankings are deterministic.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from repair.retrieval.chunking import Chunk


def files_from_chunks(scored: Sequence[tuple[Chunk, float]], method: str = "max") -> list[str]:
    per_file: dict[str, list[float]] = defaultdict(list)
    for chunk, score in scored:
        per_file[chunk.path].append(score)
    if method == "max":
        agg = {p: max(s) for p, s in per_file.items()}
    elif method == "sum_top3":
        agg = {p: sum(sorted(s, reverse=True)[:3]) for p, s in per_file.items()}
    else:
        raise ValueError(f"unknown aggregation {method!r}")
    return [p for p, _ in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))]
