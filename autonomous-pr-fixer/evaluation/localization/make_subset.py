"""
Fixed 40-instance subset of cerberus-localization-v1 for methods too slow to run on all 120 on a
laptop CPU (dense embeddings). Every method compared on the subset is scored on exactly these ids.

Stratified by repository in proportion to the 120, sympy and matplotlib excluded (they account for
48% of the distinct chunks to embed), minimum 1 per remaining repository, seed 20261009.

    python evaluation/localization/make_subset.py
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261009
TARGET = 40
EXCLUDE = {"sympy/sympy", "matplotlib/matplotlib"}


def main() -> int:
    with open(os.path.join(HERE, "instances-v1.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    pool = [r for r in rows if r["repo"] not in EXCLUDE]
    by_repo: dict[str, list[dict]] = {}
    for r in pool:
        by_repo.setdefault(r["repo"], []).append(r)
    quota = {repo: max(1, round(TARGET * len(items) / len(pool))) for repo, items in by_repo.items()}
    largest = max(by_repo, key=lambda k: len(by_repo[k]))
    quota[largest] += TARGET - sum(quota.values())
    rng = random.Random(SEED)
    chosen = []
    for repo in sorted(by_repo):
        items = sorted(by_repo[repo], key=lambda r: r["instance_id"])
        chosen += rng.sample(items, min(quota[repo], len(items)))
    chosen.sort(key=lambda r: r["instance_id"])
    out = os.path.join(HERE, "instances-v1-sub40.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        for r in chosen:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    summary = {"subset": "cerberus-localization-v1-sub40", "seed": SEED, "instances": len(chosen),
               "excluded_repos": sorted(EXCLUDE),
               "per_repo": {repo: sum(1 for r in chosen if r["repo"] == repo) for repo in sorted(by_repo)},
               "mentions_gold_path": sum(1 for r in chosen if r["mentions_gold_path"]), "sha256": digest}
    with open(os.path.join(HERE, "MANIFEST-sub40.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
