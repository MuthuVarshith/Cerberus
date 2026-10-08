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
from repair.retrieval.chunking import Chunk, chunk_repository
from repair.retrieval.fusion import rrf
from repair.retrieval.rerank import rerank
from repair.retrieval.vector import VectorIndex

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


CANDIDATES = 300   # chunks taken from each retriever before file aggregation / fusion
RERANK_TOP = 30    # fused chunks passed to the cross-encoder


def _vector_index(model_key: str, chunks: list[Chunk]) -> tuple[VectorIndex, Timings]:
    from repair.retrieval.embeddings import MODELS, EmbeddingCache

    cache = EmbeddingCache(MODELS[model_key])
    t0 = time.perf_counter()
    index = VectorIndex(chunks, cache)
    # With a warmed cache this is lookup + Qdrant upload; `embedded` > 0 means vectors were computed now.
    return index, {"index_seconds": time.perf_counter() - t0, "embedded": float(cache.misses)}


def _vector(model_key: str) -> RankFn:
    def rank(workspace_dir: str, problem: str, sources: dict[str, str]) -> tuple[list[str], Timings]:
        chunks = chunk_repository(sources)
        index, timings = _vector_index(model_key, chunks)
        try:
            t0 = time.perf_counter()
            ranking = files_from_chunks(index.search(problem, CANDIDATES), "sum_top3")
            timings["query_seconds"] = time.perf_counter() - t0
        finally:
            index.close()
        return ranking, {**timings, "chunks": float(len(chunks))}
    return rank


def _hybrid(model_key: str, rerank_top: int = 0) -> RankFn:
    def rank(workspace_dir: str, problem: str, sources: dict[str, str]) -> tuple[list[str], Timings]:
        chunks = chunk_repository(sources)
        t0 = time.perf_counter()
        bm25 = BM25Index(chunks)
        t_bm25 = time.perf_counter() - t0
        index, timings = _vector_index(model_key, chunks)
        try:
            t1 = time.perf_counter()
            lexical = bm25.search(problem, CANDIDATES)
            dense = index.search(problem, CANDIDATES)
            fused_files = rrf([files_from_chunks(lexical, "sum_top3"), files_from_chunks(dense, "sum_top3")])
            timings["query_seconds"] = time.perf_counter() - t1
            if rerank_top:
                t2 = time.perf_counter()
                fused_chunks = rrf([[c for c, _ in lexical], [c for c, _ in dense]])[:rerank_top]
                reranked = files_from_chunks(rerank(problem, fused_chunks), "max")
                # Reranked files first, then the rest of the fused file ranking.
                fused_files = list(dict.fromkeys(reranked + fused_files))
                timings["rerank_seconds"] = time.perf_counter() - t2
        finally:
            index.close()
        timings["index_seconds"] += t_bm25
        return fused_files, {**timings, "chunks": float(len(chunks))}
    return rank


METHODS: dict[str, Method] = {
    "phase1": Method("phase1", _phase1, needs_files=True),
    "bm25": Method("bm25", _bm25("max")),
    "bm25-sum3": Method("bm25-sum3", _bm25("sum_top3")),
    "vector-bge-small": Method("vector-bge-small", _vector("bge-small")),
    "vector-jina-code": Method("vector-jina-code", _vector("jina-code")),
    "hybrid-bge-small": Method("hybrid-bge-small", _hybrid("bge-small")),
    "hybrid-jina-code": Method("hybrid-jina-code", _hybrid("jina-code")),
    "hybrid-bge-small-rerank": Method("hybrid-bge-small-rerank", _hybrid("bge-small", RERANK_TOP)),
    "hybrid-jina-code-rerank": Method("hybrid-jina-code-rerank", _hybrid("jina-code", RERANK_TOP)),
}
