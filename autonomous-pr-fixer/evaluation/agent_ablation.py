"""
Compare end-to-end agent configurations over repeated runs.

    python evaluation/agent_ablation.py --config baseline=exp-a1,exp-a2,exp-a3 --config bm25=exp-b1,exp-b2,exp-b3

Each experiment is a results/<id>/ directory written by `main.py agent evaluate`. For every
configuration and every repeat this computes: fix rate (admitted / tasks), false admissions
(admitted but the hidden check failed), tool calls before the first edit and model tokens per
task (from the run records); then reports the mean and min–max over repeats. The first
configuration is the reference: any configuration with more false admissions is flagged.
Tasks that ended in SKIPPED_* (budget, quota, provider error) are excluded and counted separately,
because they say nothing about the configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from evaluation.localization.agent_behaviour import first_edit_stats  # noqa: E402

RESULTS = os.path.join(ROOT, "results")


def repeat_metrics(experiment: str, results_root: str = RESULTS) -> dict[str, Any]:
    base = os.path.join(results_root, experiment)
    with open(os.path.join(base, "task-results.jsonl"), encoding="utf-8") as f:
        tasks = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(base, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    counted = [t for t in tasks if not t["final_status"].startswith("SKIPPED")]
    admitted = [t for t in counted if t["admitted"]]
    before_edit: list[int] = []
    runs_dir = os.path.join(base, "runs")
    if os.path.isdir(runs_dir):
        for name in sorted(os.listdir(runs_dir)):
            path = os.path.join(runs_dir, name, "agent_run.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    stats = first_edit_stats(json.load(f))
                if stats:
                    before_edit.append(stats["tool_calls_before_first_edit"])
    costs = [t["cost_usd"] for t in counted]
    return {
        "experiment": experiment,
        "model": config["config"]["model"],
        "retrieval": config["config"].get("retrieval"),
        "tasks": len(counted),
        "skipped": len(tasks) - len(counted),
        "fix_rate": len(admitted) / len(counted) if counted else None,
        "false_admissions": sum(1 for t in admitted if t["false_admission"] is True),
        "false_admission_unmeasured": sum(1 for t in admitted if not isinstance(t["false_admission"], bool)),
        "tool_calls_before_first_edit": statistics.median(before_edit) if before_edit else None,
        "tokens_per_task": statistics.fmean(t["total_tokens"] for t in counted) if counted else None,
        "cost_per_task": (statistics.fmean(costs) if counted and all(c is not None for c in costs) else None),
    }


def summarize(repeats: list[dict[str, Any]], key: str) -> str:
    values = [r[key] for r in repeats if r[key] is not None]
    if not values:
        return "n/a"
    mean = statistics.fmean(values)
    if len(values) == 1:
        return f"{mean:.3g} (1 run)"
    return f"{mean:.3g} [{min(values):.3g}–{max(values):.3g}]"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", action="append", required=True, help="NAME=exp1,exp2,...")
    p.add_argument("--results", default=RESULTS)
    args = p.parse_args(argv)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for spec in args.config:
        name, _, exps = spec.partition("=")
        groups.append((name, [repeat_metrics(e, args.results) for e in exps.split(",") if e]))

    lines = ["| Config | Repeats | Tasks per run | Fix rate | False admissions | Tool calls before first edit "
             "| Tokens per task | Cost per task |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, reps in groups:
        lines.append(f"| {name} | {len(reps)} | {summarize(reps, 'tasks')} | {summarize(reps, 'fix_rate')} | "
                     f"{summarize(reps, 'false_admissions')} | {summarize(reps, 'tool_calls_before_first_edit')} | "
                     f"{summarize(reps, 'tokens_per_task')} | {summarize(reps, 'cost_per_task')} |")
    print("\n".join(lines))
    reference = sum(r["false_admissions"] for r in groups[0][1])
    for name, reps in groups[1:]:
        if sum(r["false_admissions"] for r in reps) > reference:
            print(f"\nWARNING: {name} has more false admissions than {groups[0][0]}.")
    skipped = {name: sum(r["skipped"] for r in reps) for name, reps in groups}
    if any(skipped.values()):
        print(f"\nTasks skipped for provider/budget reasons (excluded above): {skipped}")
    if any(len(reps) < 3 for _, reps in groups):
        print("\nNote: fewer than 3 repeats for some configuration; spread is not meaningful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
