"""Vector search over chunks with Qdrant in embedded (local, in-process) mode. No server."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import numpy as np

from repair.retrieval.chunking import Chunk
from repair.retrieval.embeddings import EmbeddingCache, embed_text


class VectorIndex:
    def __init__(self, chunks: Sequence[Chunk], cache: EmbeddingCache):
        from qdrant_client import QdrantClient, models

        self.chunks = list(chunks)
        self.cache = cache
        self.client = QdrantClient(location=":memory:")
        self.collection = f"chunks_{uuid.uuid4().hex[:8]}"
        vectors = cache.embed_documents([embed_text(c) for c in self.chunks])
        dim = int(vectors.shape[1]) if len(self.chunks) else 1
        self.client.create_collection(self.collection,
                                      vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
        if len(self.chunks):
            self.client.upload_collection(self.collection, vectors=vectors, ids=list(range(len(self.chunks))),
                                          payload=[{"path": c.path, "symbol": c.symbol} for c in self.chunks])
        self.dim = dim

    def search(self, query: str, k: int = 200) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        q = self.cache.embed_query(query)
        hits = self.client.query_points(self.collection, query=np.asarray(q).tolist(), limit=k).points
        return [(self.chunks[int(h.id)], float(h.score)) for h in hits]

    def close(self) -> None:
        self.client.close()
