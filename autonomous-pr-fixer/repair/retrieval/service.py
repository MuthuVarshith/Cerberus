"""Retrieval over the agent's workspace, behind the `retrieve_code` tool.

The index is built once per repository commit and cached on disk by commit hash (chunks only;
embeddings have their own content-keyed cache). After the agent edits files, only files whose
content changed are re-chunked. The workspace is read as text: symlinks are never followed, nothing
is imported or executed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from typing import Any

from repair.retrieval.bm25 import BM25Index
from repair.retrieval.chunking import Chunk, chunk_source
from repair.retrieval.embeddings import CACHE_DIR
from repair.retrieval.fusion import rrf

MAX_FILE_BYTES = 1024 * 1024
_SKIP_DIRS = {"__pycache__", "venv", "env", "node_modules", "build", "dist"}
CANDIDATES = 300
KINDS = ("bm25", "hybrid-bge-small", "hybrid-jina-code")


def _read_workspace(root: str) -> dict[str, str]:
    sources: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                   and not os.path.islink(os.path.join(dirpath, d))]
        for name in files:
            full = os.path.join(dirpath, name)
            if not name.endswith(".py") or os.path.islink(full):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            sources[os.path.relpath(full, root).replace("\\", "/")] = data.decode("utf-8", errors="replace")
    return sources


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class WorkspaceRetriever:
    def __init__(self, workspace_dir: str, commit: str | None, kind: str = "bm25", cache_dir: str | None = None):
        if kind not in KINDS:
            raise ValueError(f"unknown retriever {kind!r}; choose from {KINDS}")
        self.root = workspace_dir
        self.commit = commit or ""
        self.kind = kind
        self.cache_dir = cache_dir or os.path.join(CACHE_DIR, "chunks")
        self._files: dict[str, tuple[str, list[Chunk]]] = {}  # path -> (content digest, chunks)
        self._bm25: BM25Index | None = None
        self._vector: Any = None
        self._load_commit_cache()

    # ------------------------------------------------------------------ cache by commit

    def _cache_path(self) -> str | None:
        return os.path.join(self.cache_dir, f"{self.commit}.json") if self.commit else None

    def _load_commit_cache(self) -> None:
        path = self._cache_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._files = {p: (d, [Chunk(**{**c, "imports": tuple(c["imports"])}) for c in chunks])
                           for p, (d, chunks) in data.items()}
        except (OSError, ValueError, TypeError, KeyError):
            self._files = {}

    def _save_commit_cache(self) -> None:
        path = self._cache_path()
        if not path:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        data = {p: (d, [dataclasses.asdict(c) for c in chunks]) for p, (d, chunks) in self._files.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    # ------------------------------------------------------------------ indexing

    def refresh(self) -> dict[str, int]:
        """Re-chunk only files whose content changed since the last refresh."""
        sources = _read_workspace(self.root)
        changed = 0
        for path, text in sources.items():
            digest = _digest(text)
            if self._files.get(path, ("", []))[0] != digest:
                self._files[path] = (digest, chunk_source(path, text))
                changed += 1
        removed = [p for p in self._files if p not in sources]
        for p in removed:
            del self._files[p]
        if changed or removed or self._bm25 is None:
            chunks = [c for _, cs in self._files.values() for c in cs]
            self._bm25 = BM25Index(chunks)
            if self._vector is not None:
                self._vector.close()
            self._vector = None
            if self.kind != "bm25":
                from repair.retrieval.embeddings import MODELS, EmbeddingCache
                from repair.retrieval.vector import VectorIndex
                self._vector = VectorIndex(chunks, EmbeddingCache(MODELS[self.kind.removeprefix("hybrid-")]))
        return {"files": len(sources), "rechunked": changed, "removed": len(removed)}

    def build(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        stats = self.refresh()
        self._save_commit_cache()
        return {**stats, "seconds": round(time.perf_counter() - t0, 3)}

    # ------------------------------------------------------------------ query

    def search(self, query: str, k: int = 10) -> list[tuple[Chunk, float]]:
        self.refresh()
        assert self._bm25 is not None
        lexical = self._bm25.search(query, CANDIDATES)
        if self._vector is None:
            return lexical[:k]
        dense = self._vector.search(query, CANDIDATES)
        fused = rrf([[c for c, _ in lexical], [c for c, _ in dense]])
        return [(c, 0.0) for c in fused[:k]]

    def close(self) -> None:
        if self._vector is not None:
            self._vector.close()
