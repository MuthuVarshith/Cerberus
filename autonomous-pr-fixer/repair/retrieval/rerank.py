"""Optional cross-encoder reranking of the top candidate chunks (offline, ONNX via fastembed).

A cross-encoder reads the query and each candidate together, which is more accurate than comparing
separate embeddings but costs one model call per candidate, so it is applied only to the top N.
It is kept in the final configuration only if the benchmark shows a Recall@5 gain worth the latency.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from repair.retrieval.chunking import Chunk
from repair.retrieval.embeddings import CACHE_DIR, MAX_CHARS, embed_text

RERANKER = "jinaai/jina-reranker-v1-turbo-en"


@lru_cache(maxsize=2)
def _model(name: str) -> Any:
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=name, cache_dir=os.path.join(CACHE_DIR, "models"))


def rerank(query: str, candidates: Sequence[Chunk], model_name: str = RERANKER) -> list[tuple[Chunk, float]]:
    if not candidates:
        return []
    scores = list(_model(model_name).rerank(query[:MAX_CHARS * 2], [embed_text(c) for c in candidates]))
    ranked = sorted(zip(candidates, scores, strict=True), key=lambda cs: -float(cs[1]))
    return [(c, float(s)) for c, s in ranked]
