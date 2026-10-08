"""
How the Phase 1 agent finds code, measured from recorded runs (results/*/runs/*/agent_run.json).

    python evaluation/localization/agent_behaviour.py [--exclude SUBSTRING ...]

Per run: tool calls before the first edit (edit_file / apply_patch), model tokens spent up to and
including the call that issued that edit, whether the first edited file is a gold file of the task,
and overall tool usage. Runs that never edited are reported separately, with their final status.
Nothing is estimated: runs without a recorded edit have no "before first edit" numbers.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from collections import Counter
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EDIT_TOOLS = {"edit_file", "apply_patch"}


def first_edit_stats(run: dict[str, Any]) -> dict[str, Any] | None:
    names = [c["tool"] for c in run["tool_calls"]]
    first = next((i for i, n in enumerate(names) if n in EDIT_TOOLS), None)
    if first is None:
        return None
    tokens, issued = 0, 0
    for call in run["llm_calls"]:
        tokens += call["input_tokens"] + call["output_tokens"]
        issued += len(call["tool_calls"])
        if issued > first:
            break
    return {"tool_calls_before_first_edit": first, "tokens_through_first_edit": tokens}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--exclude", nargs="*", default=["nemotron"], help="skip run paths containing these strings")
    args = p.parse_args(argv)

    edited: list[dict[str, Any]] = []
    never: list[dict[str, Any]] = []
    usage: Counter[str] = Counter()
    for path in sorted(glob.glob(os.path.join(ROOT, "results", "*", "runs", "*", "agent_run.json"))):
        if any(x in path for x in args.exclude):
            continue
        with open(path, encoding="utf-8") as f:
            run = json.load(f)
        if not run["tool_calls"]:
            continue
        usage.update(c["tool"] for c in run["tool_calls"])
        row = {"run": os.path.basename(os.path.dirname(path)), "model": run["model"],
               "final_status": run["final_status"], "tool_calls": len(run["tool_calls"])}
        stats = first_edit_stats(run)
        (edited if stats else never).append({**row, **(stats or {})})

    print(f"runs with tool calls: {len(edited) + len(never)} (edited: {len(edited)}, never edited: {len(never)})")
    if edited:
        before = [r["tool_calls_before_first_edit"] for r in edited]
        tok = [r["tokens_through_first_edit"] for r in edited]
        print(f"tool calls before first edit: median {statistics.median(before)}, min {min(before)}, max {max(before)}")
        print(f"tokens through first edit:    median {statistics.median(tok)}, min {min(tok)}, max {max(tok)}")
    for r in never:
        print(f"never edited: {r['run']} ({r['model']}, {r['final_status']}, {r['tool_calls']} tool calls)")
    print("tool usage:", json.dumps(dict(usage.most_common())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
