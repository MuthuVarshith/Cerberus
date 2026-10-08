"""Offline embeddings with a persistent cache keyed by content.

Models run locally through fastembed (ONNX on CPU): no API calls, no PyTorch, and no model-repository
code is executed. Vectors are cached in SQLite by (model, SHA-256 of the embedded text), so indexing a
new commit of a repository only embeds chunks whose text changed.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import numpy as np

from repair.retrieval.chunking import Chunk

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "artifacts", "retrieval-cache")
MAX_CHARS = 1500  # ~400–500 tokens: keeps CPU embedding time bounded; long chunks keep their head
BATCH = 64

MODELS = {
    "jina-code": "jinaai/jina-embeddings-v2-base-code",
    "bge-small": "BAAI/bge-small-en-v1.5",
}


def embed_text(chunk: Chunk) -> str:
    return f"{chunk.path} {chunk.symbol}\n{chunk.docstring}\n{chunk.text}"[:MAX_CHARS]


@lru_cache(maxsize=4)
def _model(name: str) -> Any:
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=name, cache_dir=os.path.join(CACHE_DIR, "models"))


class EmbeddingCache:
    def __init__(self, model_name: str, path: str | None = None):
        self.model_name = model_name
        os.makedirs(CACHE_DIR, exist_ok=True)
        safe = model_name.replace("/", "__")
        self.db = sqlite3.connect(path or os.path.join(CACHE_DIR, f"{safe}.sqlite"))
        self.db.execute("CREATE TABLE IF NOT EXISTS vec (h TEXT PRIMARY KEY, v BLOB)")
        self.hits = 0
        self.misses = 0

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        keys = [hashlib.sha256(t.encode("utf-8", "replace")).hexdigest() for t in texts]
        found: dict[str, np.ndarray] = {}
        for i in range(0, len(keys), 900):
            part = keys[i:i + 900]
            rows = self.db.execute(f"SELECT h, v FROM vec WHERE h IN ({','.join('?' * len(part))})", part)
            for h, blob in rows:
                found[h] = np.frombuffer(blob, dtype=np.float32)
        missing = [i for i, k in enumerate(keys) if k not in found]
        self.hits += len(keys) - len(missing)
        self.misses += len(missing)
        if missing:
            model = _model(self.model_name)
            todo = [texts[i] for i in missing]
            vectors = list(model.passage_embed(todo, batch_size=BATCH))
            with self.db:
                for i, vec in zip(missing, vectors, strict=True):
                    arr = np.asarray(vec, dtype=np.float32)
                    found[keys[i]] = arr
                    self.db.execute("INSERT OR REPLACE INTO vec VALUES (?, ?)", (keys[i], arr.tobytes()))
        return np.stack([found[k] for k in keys]) if keys else np.zeros((0, 0), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(next(iter(_model(self.model_name).query_embed([text[:MAX_CHARS * 2]]))), dtype=np.float32)
