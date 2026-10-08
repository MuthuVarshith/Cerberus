"""File-localization methods, as used by the benchmark (and later by the agent tool).

Each method takes the issue text and the repository's non-test sources ({path: text}) and returns
a file ranking plus timings in seconds. ``needs_files`` methods also get a directory with the same
files written out (the Phase 1 localizer reads from disk).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from repair.retrieval.aggregate import files_from_chunks
from repair.retrieval.bm25 import BM25Index
from repair.retrieval.chunking import chunk_repository

Timings = dict[str, float]
RankFn = Callable[[str, str, dict[str, str]], tuple[list[str], Timings]]


@dataclass(frozen=True)
class Method:
    name: str
    rank: RankFn
    needs_files: bool = False


def _phase1(workspace_dir: str, problem: str, sources: dict[str, str]) -> tuple[list[str], Timings]:
    from repair.retrieval.baseline import phase1_ranking
    t0 = time.perf_counter()
    ranking = phase1_ranking(workspace_dir, problem)
    return ranking, {"query_seconds": time.perf_counter() - t0}


def _bm25(aggregation: str) -> RankFn:
    def rank(workspace_dir: str, problem: str, sources: dict[str, str]) -> tuple[list[str], Timings]:
        t0 = time.perf_counter()
        chunks = chunk_repository(sources)
        t1 = time.perf_counter()
        index = BM25Index(chunks)
        t2 = time.perf_counter()
        ranking = files_from_chunks(index.search(problem), aggregation)
        t3 = time.perf_counter()
        return ranking, {"chunk_seconds": t1 - t0, "index_seconds": t2 - t1, "query_seconds": t3 - t2,
                         "chunks": float(len(chunks))}
    return rank


METHODS: dict[str, Method] = {
    "phase1": Method("phase1", _phase1, needs_files=True),
    "bm25": Method("bm25", _bm25("max")),
    "bm25-sum3": Method("bm25-sum3", _bm25("sum_top3")),
}
