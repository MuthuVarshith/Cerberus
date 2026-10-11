"""
Compare end-to-end agent configurations over repeated runs.

    python evaluation/agent_ablation.py --config baseline=exp-a1,exp-a2,exp-a3 --config bm25=exp-b1,exp-b2,exp-b3

Each experiment is a results/<id>/ directory written by `main.py agent evaluate`. Repeats are
separated by commas; one repeat may span several experiment directories joined with `+`
(e.g. `bm25=s4-bm25-r1-d1+s4-bm25-r1-d2,s4-bm25-r2-d1`, when a repeat was resumed in a second run
after a quota stop). Their task results are pooled per task id: a completed result (not SKIPPED_*)
replaces a skipped one, and if a task completed in more than one directory the first one listed is
kept and the duplicate reported.

For every configuration and every repeat this computes: fix rate (admitted / tasks), false
admissions (admitted but the hidden check failed), tool calls before the first edit and model tokens
per task (from the run records of the counted tasks); then reports the mean and min–max over
repeats. The first configuration is the reference: any configuration with more false admissions is
flagged. Tasks that ended in SKIPPED_* (budget, quota, provider error) are excluded and counted
separately, because they say nothing about the configuration.
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


def _skipped(task: dict[str, Any]) -> bool:
    return bool(task["final_status"].startswith("SKIPPED"))


def pool_tasks(experiments: list[str], results_root: str = RESULTS
               ) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Pool task results of one repeat spread over several experiment directories.

    Returns (experiment, task) pairs in first-seen task order, the experiments' config.json records
    and the task ids that completed in more than one directory (first listed kept).
    """
    chosen: dict[str, tuple[str, dict[str, Any]]] = {}
    configs: list[dict[str, Any]] = []
    duplicates: list[str] = []
    for experiment in experiments:
        base = os.path.join(results_root, experiment)
        with open(os.path.join(base, "config.json"), encoding="utf-8") as f:
            configs.append(json.load(f))
        with open(os.path.join(base, "task-results.jsonl"), encoding="utf-8") as f:
            tasks = [json.loads(line) for line in f if line.strip()]
        for task in tasks:
            previous = chosen.get(task["task_id"])
            if previous is None or (_skipped(previous[1]) and not _skipped(task)):
                chosen[task["task_id"]] = (experiment, task)
            elif not _skipped(previous[1]) and not _skipped(task):
                duplicates.append(task["task_id"])
    return list(chosen.values()), configs, duplicates


def _config_value(configs: list[dict[str, Any]], key: str) -> Any:
    values = list(dict.fromkeys(str(c["config"].get(key)) for c in configs))
    if len(values) > 1:
        print(f"warning: experiments pooled into one repeat differ in {key}: {values}", file=sys.stderr)
        return " / ".join(values)
    return configs[0]["config"].get(key)


def repeat_metrics(experiment: str | list[str], results_root: str = RESULTS) -> dict[str, Any]:
    """Metrics for one repeat: one experiment id, `a+b+...`, or a list of ids pooled together."""
    experiments = experiment.split("+") if isinstance(experiment, str) else list(experiment)
    pooled, configs, duplicates = pool_tasks(experiments, results_root)
    if duplicates:
        print(f"warning: tasks completed in more than one of {experiments} (first kept): {duplicates}",
              file=sys.stderr)
    counted = [(e, t) for e, t in pooled if not _skipped(t)]
    admitted = [t for _, t in counted if t["admitted"]]
    before_edit: list[int] = []
    for exp, task in counted:
        run_id = task.get("run_id") or f"{exp}-{task['task_id']}"
        path = os.path.join(results_root, exp, "runs", run_id, "agent_run.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                stats = first_edit_stats(json.load(f))
            if stats:
                before_edit.append(stats["tool_calls_before_first_edit"])
    costs = [t["cost_usd"] for _, t in counted]
    counted_tasks = [t for _, t in counted]
    return {
        "experiment": "+".join(experiments),
        "model": _config_value(configs, "model"),
        "retrieval": _config_value(configs, "retrieval"),
        "tasks": len(counted),
        "skipped": len(pooled) - len(counted),
        "duplicates": len(duplicates),
        "fix_rate": len(admitted) / len(counted) if counted else None,
        "false_admissions": sum(1 for t in admitted if t["false_admission"] is True),
        "false_admission_unmeasured": sum(1 for t in admitted if not isinstance(t["false_admission"], bool)),
        "tool_calls_before_first_edit": statistics.median(before_edit) if before_edit else None,
        "tokens_per_task": statistics.fmean(t["total_tokens"] for t in counted_tasks) if counted else None,
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
    p.add_argument("--config", action="append", required=True,
                   help="NAME=exp1,exp2,... (one repeat per comma; join directories of one repeat with +)")
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
