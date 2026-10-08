"""
Run file-localization methods on the benchmark. No LLM calls; free to run.

    python evaluation/localization/run.py --method phase1 [--limit N] [--out NAME]

For each instance the repository's non-test Python files at the base commit are exported (text
only, nothing executed), the method ranks files from the issue text, and the ranking is scored
against the gold files. Results: evaluation/localization/results/<out>.json and .md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from repair.retrieval.methods import METHODS  # noqa: E402
from repair.retrieval.metrics import KS, InstanceScore, aggregate, score_instance  # noqa: E402
from repair.retrieval.source import materialize, python_files, read_sources  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MIRRORS = os.path.join(ROOT, "artifacts", "swebench", "mirrors")
WORK = os.path.join(ROOT, "artifacts", "swebench", "work")


def mirror_for(repo: str) -> str:
    return os.path.join(MIRRORS, repo.replace("/", "__") + ".git")


def load_instances(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _table(agg: Any) -> str:
    return (f"| {agg.n} | {agg.recall[1]:.3f} | {agg.recall[5]:.3f} | {agg.recall[10]:.3f} | "
            f"{agg.all_found[1]:.3f} | {agg.all_found[5]:.3f} | {agg.all_found[10]:.3f} | "
            f"{agg.mrr:.3f} | {agg.not_ranked} |")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--method", required=True, choices=sorted(METHODS))
    p.add_argument("--instances", default=os.path.join(HERE, "instances-v1.jsonl"))
    p.add_argument("--limit", type=int)
    p.add_argument("--out")
    args = p.parse_args(argv)

    method = METHODS[args.method]
    instances = load_instances(args.instances)[: args.limit]
    os.makedirs(WORK, exist_ok=True)
    per_instance: list[dict[str, Any]] = []
    scores: list[InstanceScore] = []
    for i, inst in enumerate(instances, 1):
        mirror = mirror_for(inst["repo"])
        if not os.path.isdir(mirror):
            print(f"[{i}/{len(instances)}] {inst['instance_id']}: mirror missing, skipped")
            continue
        ws = tempfile.mkdtemp(prefix="loc_", dir=WORK)
        try:
            t0 = time.perf_counter()
            paths = python_files(mirror, inst["base_commit"])
            sources = read_sources(mirror, inst["base_commit"], paths)
            if method.needs_files:
                materialize(mirror, inst["base_commit"], ws)
            t1 = time.perf_counter()
            ranking, timings = method.rank(ws, inst["problem_statement"], sources)
            t2 = time.perf_counter()
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        s = score_instance(inst["instance_id"], ranking, set(inst["gold_files"]))
        scores.append(s)
        per_instance.append({**s.model_dump(), "repo": inst["repo"], "gold_files": inst["gold_files"],
                             "mentions_gold_path": inst["mentions_gold_path"], "candidate_files": len(sources),
                             "export_seconds": round(t1 - t0, 3), "rank_seconds": round(t2 - t1, 3),
                             "timings": {k: round(v, 4) for k, v in timings.items()}, "top10": ranking[:10]})
        print(f"[{i}/{len(instances)}] {inst['instance_id']}: first gold rank {s.first_gold_rank} "
              f"of {len(sources)} files ({t2 - t1:.2f}s)", flush=True)

    if not scores:
        print("no instances scored")
        return 1
    overall = aggregate(scores)
    named = [s for s, r in zip(scores, per_instance, strict=True) if r["mentions_gold_path"]]
    unnamed = [s for s, r in zip(scores, per_instance, strict=True) if not r["mentions_gold_path"]]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    out = args.out or f"{args.method}"
    record = {
        "method": args.method,
        "benchmark": "cerberus-localization-v1",
        "cerberus_commit": commit,
        "run_utc": _dt.datetime.now(_dt.UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "overall": overall.model_dump(),
        "issue_names_gold_file": aggregate(named).model_dump() if named else None,
        "issue_does_not_name_gold_file": aggregate(unnamed).model_dump() if unnamed else None,
        "median_rank_seconds": round(statistics.median(r["rank_seconds"] for r in per_instance), 3),
        "median_timings": {k: round(statistics.median(r["timings"][k] for r in per_instance), 4)
                           for k in per_instance[0]["timings"]},
        "median_candidate_files": statistics.median(r["candidate_files"] for r in per_instance),
        "instances": per_instance,
    }
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    with open(os.path.join(HERE, "results", f"{out}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, indent=2)
    header = ("| Subset | n | R@1 | R@5 | R@10 | All@1 | All@5 | All@10 | MRR | gold never ranked |\n"
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    provenance = f"Cerberus `{commit[:12]}`, {record['run_utc'][:19]}Z, {platform.platform()}"
    lines = [f"# Localization: {args.method}", "", provenance, "", header, "| all " + _table(overall)]
    if named:
        lines.append("| issue names a gold file " + _table(aggregate(named)))
    if unnamed:
        lines.append("| issue does not name one " + _table(aggregate(unnamed)))
    lines += ["", f"Median ranking time {record['median_rank_seconds']}s over a median of "
                  f"{record['median_candidate_files']} candidate files. Ks: {list(KS)}."]
    with open(os.path.join(HERE, "results", f"{out}.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
