"""Reciprocal rank fusion (RRF) of several ranked lists.

score(item) = sum over lists of 1 / (k + rank), rank starting at 1. Only ranks are used, so
lists with incomparable score scales (BM25 vs cosine similarity) can be combined. k = 60 is the
value from Cormack et al. (2009) and is used unless stated otherwise.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Sequence
from typing import TypeVar

T = TypeVar("T", bound=Hashable)

RRF_K = 60


def rrf(rankings: Sequence[Sequence[T]], k: int = RRF_K, weights: Sequence[float] | None = None) -> list[T]:
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("one weight per ranking")
    scores: dict[T, float] = defaultdict(float)
    first_seen: dict[T, tuple[int, int]] = {}
    for li, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[li]
        for rank, item in enumerate(dict.fromkeys(ranking), 1):
            scores[item] += w / (k + rank)
            first_seen.setdefault(item, (rank, li))
    # Ties: better best rank first, then earlier list; deterministic without comparing items.
    return sorted(scores, key=lambda it: (-scores[it], first_seen[it]))
