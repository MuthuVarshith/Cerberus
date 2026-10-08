"""
Pre-compute chunk embeddings for a benchmark file into the persistent cache (resumable).

    python evaluation/localization/warm_embeddings.py --model bge-small [--instances FILE]

Benchmark runs of vector methods then only embed queries. Interrupting and re-running continues
from the cache (vectors are committed every 512).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from evaluation.localization.run import load_instances, mirror_for  # noqa: E402
from repair.retrieval.chunking import chunk_repository  # noqa: E402
from repair.retrieval.embeddings import MODELS, EmbeddingCache, embed_text  # noqa: E402
from repair.retrieval.source import python_files, read_sources  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, choices=sorted(MODELS))
    p.add_argument("--instances", default=os.path.join(HERE, "instances-v1-sub40.jsonl"))
    args = p.parse_args(argv)
    cache = EmbeddingCache(MODELS[args.model])
    started = time.perf_counter()
    instances = load_instances(args.instances)
    for n, inst in enumerate(instances, 1):
        mirror = mirror_for(inst["repo"])
        chunks = chunk_repository(read_sources(mirror, inst["base_commit"], python_files(mirror, inst["base_commit"])))
        t0 = time.perf_counter()
        before = cache.misses
        cache.progress = lambda done, todo, n=n: print(f"  [{n}] embedded {done}/{todo}", flush=True)
        cache.embed_documents([embed_text(c) for c in chunks])
        new = cache.misses - before
        rate = new / (time.perf_counter() - t0) if new else 0.0
        print(f"[{n}/{len(instances)}] {inst['instance_id']}: {len(chunks)} chunks, {new} newly embedded "
              f"({rate:.2f}/s), elapsed {(time.perf_counter() - started) / 60:.1f} min", flush=True)
    print(f"done: {cache.misses} embedded, {cache.hits} from cache, {(time.perf_counter() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
