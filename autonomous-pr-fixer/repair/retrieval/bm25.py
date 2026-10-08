"""BM25 (Okapi) keyword search over code chunks.

Tokenization is code-aware: identifiers are kept whole (lower-cased) and also split on snake_case
and camelCase, so ``get_real_name`` matches both the exact identifier and the words "real" and
"name"; ``HTTPResponseError`` yields ``httpresponseerror``, ``http``, ``response``, ``error``.
Each chunk is indexed with its path and qualified symbol name prepended, so a file or function
named in an issue matches strongly. Deterministic; no external dependency.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from repair.retrieval.chunking import Chunk

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
STOPWORDS = frozenset("""
a an and are as at be but by for from has have i if in into is it its of on or that the then there these
this to was were when which while will with not no do does did can could should would we you they he she
self cls def return none true false pass import class else elif try except finally raise lambda yield
""".split())


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        whole = word.lower().strip("_")
        if len(whole) >= 2 and whole not in STOPWORDS:
            out.append(whole)
        parts = [p.lower() for w in word.split("_") for p in _CAMEL.split(w) if p]
        if len(parts) > 1:
            out += [p for p in parts if len(p) >= 2 and p not in STOPWORDS]
    return out


def chunk_document(chunk: Chunk) -> str:
    return f"{chunk.path} {chunk.symbol}\n{chunk.docstring}\n{chunk.text}"


class BM25Index:
    def __init__(self, chunks: Sequence[Chunk], k1: float = 1.2, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths: list[int] = []
        for i, chunk in enumerate(self.chunks):
            counts = Counter(tokenize(chunk_document(chunk)))
            self.lengths.append(sum(counts.values()))
            for term, tf in counts.items():
                self.postings[term].append((i, tf))
        n = len(self.chunks)
        self.avg_len = (sum(self.lengths) / n) if n else 0.0
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def search(self, query: str, k: int | None = None) -> list[tuple[Chunk, float]]:
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokenize(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc, tf in self.postings[term]:
                norm = self.k1 * (1 - self.b + self.b * self.lengths[doc] / self.avg_len)
                scores[doc] += idf * tf * (self.k1 + 1) / (tf + norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(self.chunks[i], s) for i, s in (ranked[:k] if k else ranked)]
