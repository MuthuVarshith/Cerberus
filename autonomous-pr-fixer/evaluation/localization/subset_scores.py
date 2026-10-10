"""
Score saved localization results on a subset of instances (no re-running).

    python evaluation/localization/subset_scores.py --ids instances-v1-sub40.jsonl results/A.json results/B.json ...

Prints one row per result file, computed only over the instance ids in --ids, so methods run on
the full benchmark can be compared with methods run on the subset on exactly the same instances.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from repair.retrieval.metrics import InstanceScore, aggregate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ids", required=True)
    p.add_argument("results", nargs="+")
    args = p.parse_args(argv)
    with open(args.ids, encoding="utf-8") as f:
        ids = {json.loads(line)["instance_id"] for line in f if line.strip()}
    print("| Method | n | R@1 | R@5 | R@10 | All@10 | MRR | Gold never ranked | Median time per instance |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for path in args.results:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        rows = [r for r in record["instances"] if r["instance_id"] in ids]
        scores = [InstanceScore(**{k: r[k] for k in InstanceScore.model_fields}) for r in rows]
        if len(scores) != len(ids):
            print(f"warning: {record['method']} covers {len(scores)} of {len(ids)} subset instances", file=sys.stderr)
        a = aggregate(scores)
        times = sorted(r["rank_seconds"] for r in rows)
        median = times[len(times) // 2] if times else float("nan")
        print(f"| {record['method']} | {a.n} | {a.recall[1]:.3f} | {a.recall[5]:.3f} | {a.recall[10]:.3f} | "
              f"{a.all_found[10]:.3f} | {a.mrr:.3f} | {a.not_ranked} | {median:.2f} s |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
