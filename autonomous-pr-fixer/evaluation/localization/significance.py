"""
Paired significance tests between two localization result files (same instances, same order free).

    python evaluation/localization/significance.py A.json B.json [--exclude ID ...] [--only-ids FILE]

For each instance both methods are scored on identical inputs, so tests are paired:

- **Exact McNemar** on hit@k (first gold file ranked within the top k), k in 1, 5, 10: two-sided
  binomial test on the discordant pairs (A hit / B miss vs A miss / B hit).
- **Paired bootstrap** (10,000 resamples of instances, fixed seed) for the difference B − A in mean
  Recall@k and MRR, reported as the observed difference and a 95% percentile interval.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from typing import Any

KS = (1, 5, 10)
RESAMPLES = 10_000
SEED = 20261010


def load(path: str) -> dict[str, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    return {r["instance_id"]: r for r in record["instances"]}


def hit(r: dict[str, Any], k: int) -> bool:
    return r["first_gold_rank"] is not None and r["first_gold_rank"] <= k


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def bootstrap_ci(diffs: list[float], seed: int = SEED) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(statistics.fmean(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(RESAMPLES))
    return means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]


def compare(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]], ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(ids), "mcnemar": {}, "bootstrap": {}}
    for k in KS:
        b_only = sum(1 for i in ids if hit(b[i], k) and not hit(a[i], k))
        a_only = sum(1 for i in ids if hit(a[i], k) and not hit(b[i], k))
        out["mcnemar"][k] = {"a_hits": sum(hit(a[i], k) for i in ids), "b_hits": sum(hit(b[i], k) for i in ids),
                             "a_only": a_only, "b_only": b_only, "p": mcnemar_exact(a_only, b_only)}
        diffs = [b[i]["recall"][str(k)] - a[i]["recall"][str(k)] for i in ids]
        lo, hi = bootstrap_ci(diffs)
        out["bootstrap"][f"recall@{k}"] = {"diff": statistics.fmean(diffs), "ci95": [lo, hi]}
    diffs = [b[i]["reciprocal_rank"] - a[i]["reciprocal_rank"] for i in ids]
    lo, hi = bootstrap_ci(diffs)
    out["bootstrap"]["mrr"] = {"diff": statistics.fmean(diffs), "ci95": [lo, hi]}
    return out


def render(name_a: str, name_b: str, res: dict[str, Any]) -> str:
    lines = [f"**{name_b} vs {name_a}** (n = {res['n']})", "",
             "| Metric | Diff (B - A) | 95% CI | McNemar: A-only / B-only | p (exact) |",
             "| --- | --- | --- | --- | --- |"]
    for k in KS:
        bs, mc = res["bootstrap"][f"recall@{k}"], res["mcnemar"][k]
        lines.append(f"| Recall@{k} | {bs['diff']:+.3f} | [{bs['ci95'][0]:+.3f}, {bs['ci95'][1]:+.3f}] | "
                     f"{mc['a_only']} / {mc['b_only']} | {mc['p']:.2g} |")
    m = res["bootstrap"]["mrr"]
    lines.append(f"| MRR | {m['diff']:+.3f} | [{m['ci95'][0]:+.3f}, {m['ci95'][1]:+.3f}] | - | - |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--exclude", nargs="*", default=[])
    p.add_argument("--only-ids", help="jsonl file whose instance_id values define the subset")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    a, b = load(args.a), load(args.b)
    ids = sorted(set(a) & set(b) - set(args.exclude))
    if args.only_ids:
        with open(args.only_ids, encoding="utf-8") as f:
            keep = {json.loads(line)["instance_id"] for line in f if line.strip()}
        ids = [i for i in ids if i in keep]
    res = compare(a, b, ids)
    name = lambda path: path.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".json")  # noqa: E731
    print(json.dumps(res, indent=2) if args.json else render(name(args.a), name(args.b), res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
