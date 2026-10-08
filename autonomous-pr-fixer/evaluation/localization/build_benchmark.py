"""
Build the file-localization benchmark from SWE-bench Verified.

    python evaluation/localization/build_benchmark.py --parquet artifacts/swebench/verified.parquet

Sampling: 120 instances, stratified by repository in proportion to its share of the 500, with at
least 2 per repository (or all it has), fixed seed. Gold: the non-test `.py` files changed by the
reference patch. `mentions_gold_path` flags issues whose text names a gold file (by path, dotted
module or file name), because those make localization much easier; results are reported with and
without them. Writes instances-v1.jsonl and MANIFEST.json (SHA-256 of the file and the source).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from harness.scope_gate import is_test_path  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261008
TARGET = 120
MIN_PER_REPO = 2


def gold_files(patch: str) -> list[str]:
    files = re.findall(r"^diff --git a/(\S+) b/\S+$", patch, flags=re.MULTILINE)
    return sorted({f for f in files if f.endswith(".py") and not is_test_path(f)})


def mentions(problem: str, path: str) -> bool:
    module = path[:-3].replace("/", ".")
    name = os.path.basename(path)
    if path in problem or module in problem:
        return True
    return name != "__init__.py" and re.search(rf"\b{re.escape(name)}\b", problem) is not None


def sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_repo.setdefault(r["repo"], []).append(r)
    total = len(rows)
    quota = {repo: min(len(items), max(MIN_PER_REPO, round(TARGET * len(items) / total)))
             for repo, items in by_repo.items()}
    # Adjust the largest repository so the total is exactly TARGET.
    largest = max(by_repo, key=lambda k: len(by_repo[k]))
    quota[largest] += TARGET - sum(quota.values())
    chosen: list[dict[str, Any]] = []
    for repo in sorted(by_repo):
        items = sorted(by_repo[repo], key=lambda r: r["instance_id"])
        chosen += rng.sample(items, quota[repo])
    return sorted(chosen, key=lambda r: r["instance_id"])


def main(argv: list[str] | None = None) -> int:
    import pandas as pd

    p = argparse.ArgumentParser()
    p.add_argument("--parquet", required=True)
    p.add_argument("--out", default=os.path.join(HERE, "instances-v1.jsonl"))
    args = p.parse_args(argv)

    frame = pd.read_parquet(args.parquet)
    rows = []
    for rec in frame.to_dict(orient="records"):
        gold = gold_files(rec["patch"])
        if not gold:
            continue
        rows.append({
            "instance_id": rec["instance_id"],
            "repo": rec["repo"],
            "base_commit": rec["base_commit"],
            "problem_statement": rec["problem_statement"],
            "gold_files": gold,
            "mentions_gold_path": any(mentions(rec["problem_statement"], g) for g in gold),
            "difficulty": rec.get("difficulty"),
        })
    chosen = sample(rows)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for r in chosen:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(args.out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(args.parquet, "rb") as f:
        source_digest = hashlib.sha256(f.read()).hexdigest()
    manifest = {
        "benchmark": "cerberus-localization-v1",
        "source": "princeton-nlp/SWE-bench_Verified (test split, 500 instances)",
        "source_sha256": source_digest,
        "seed": SEED,
        "instances": len(chosen),
        "eligible": len(rows),
        "per_repo": {repo: sum(1 for r in chosen if r["repo"] == repo) for repo in sorted({r["repo"] for r in chosen})},
        "mentions_gold_path": sum(1 for r in chosen if r["mentions_gold_path"]),
        "instances_sha256": digest,
    }
    with open(os.path.join(HERE, "MANIFEST.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
