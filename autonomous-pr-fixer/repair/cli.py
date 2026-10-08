"""
`python main.py agent ...` — the repair agent's command line.

  agent run       --repo PATH --issue "title" [--body TEXT] --repro-test PATH [--model M]
  agent evaluate  [--limit N] [--only ID ...] [--experiment-id ID]
  agent demo      scripted negative-path demo: bad patch -> REGRESSION -> feedback -> fix -> ADMITTED

Existing `python main.py` flags are unaffected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import List, Optional

from harness.docker_sandbox import ISOLATION_HOST_UNSAFE
from repair.config import RepairConfig
from repair.llm.provider import LLMError


def _config(args: argparse.Namespace) -> RepairConfig:
    if getattr(args, "provider", None):
        os.environ["LLM_PROVIDER"] = args.provider  # before from_env, so provider defaults follow it
    return RepairConfig.from_env(
        model=getattr(args, "model", None),
        max_attempts=getattr(args, "max_attempts", None),
        isolation=ISOLATION_HOST_UNSAFE if getattr(args, "unsafe_local_sandbox", False) else None,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from repair.runner import RepairTask, run_repair

    with open(args.repro_test, encoding="utf-8") as f:
        repro = f.read()
    task = RepairTask(repo_dir=os.path.abspath(args.repo), issue_id=f"#{args.issue_number}", title=args.issue,
                      body=args.body or args.issue, reproduction_test=repro, base_ref=args.base,
                      issue_number=args.issue_number)
    try:
        state = run_repair(task, _config(args), run_id=args.run_id, printer=print)
    except LLMError as exc:
        print(f"LLM_FAILURE: {exc}", file=sys.stderr)
        return 2
    if state.final_status and state.final_status.value == "SETUP_FAILURE":
        print(f"SETUP_FAILURE: {state.final_message}", file=sys.stderr)
    return 0 if state.final_status and state.final_status.value == "ADMITTED" else 1


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from repair.evaluation import evaluate

    summary = evaluate(_config(args), limit=args.limit, only=args.only, experiment_id=args.experiment_id)
    m = summary["metrics"]
    print(json.dumps({k: m[k] for k in ("total_tasks", "successful_repairs", "fix_rate", "false_admission_rate",
                                        "total_cost", "median_repair_time")}, indent=2))
    return 0


def demo_scenario(work_dir: str):
    """iv-04's regressing candidate, then iv-01's correct candidate, for the same benchmark bug."""
    from evaluation.benchmark import build_repo, load_instances, ops_diff
    from repair.runner import RepairTask

    insts = {i.id: i for i in load_instances()}
    good, bad = insts["iv-01"], insts["iv-04"]
    repo = build_repo(good, os.path.join(work_dir, "intervals"))
    bad_diff = ops_diff(repo, bad.candidate, "iv-04 candidate")
    good_diff = ops_diff(repo, good.candidate, "iv-01 candidate")
    task = RepairTask(repo_dir=repo, issue_id=f"#{good.issue['number']}", title=good.issue["title"],
                      body=good.issue["body"], reproduction_test=good.reproduction_test, issue_number=good.issue["number"])
    return task, bad_diff, good_diff


def scripted_demo_model(bad_diff: str, good_diff: str):
    from repair.llm.fake import FakeChatModel, call, respond
    from repair.tools.base import REPRO_TEST_PATH

    return FakeChatModel([
        respond(call("read_test", path=REPRO_TEST_PATH), call("search_code", query="def merge")),
        respond(call("apply_patch", diff=bad_diff), call("run_test", target=REPRO_TEST_PATH)),
        respond(call("submit_patch", reasoning="Scripted bad patch: fixes the containment case but changes merging of touching intervals.")),
        respond(call("reset_workspace"), call("apply_patch", diff=good_diff), call("run_test", target=REPRO_TEST_PATH)),
        respond(call("submit_patch", reasoning="Scripted correct patch: keep the larger end when an interval contains a later one.")),
    ], model="scripted-demo")


def _cmd_demo(args: argparse.Namespace) -> int:
    from repair.runner import run_repair

    print("SCRIPTED DEMO: the model is a fixed script (no LLM). The Cerberus gate runs for real.\n")
    with tempfile.TemporaryDirectory(prefix="cerberus_agent_demo_") as tmp:
        task, bad, good = demo_scenario(tmp)
        state = run_repair(task, _config(args), model=scripted_demo_model(bad, good), run_id=args.run_id, printer=print)
    return 0 if state.final_status and state.final_status.value == "ADMITTED" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py agent", description="Cerberus repair agent")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--provider", choices=["anthropic", "gemini", "openrouter"], help="Default: LLM_PROVIDER or anthropic")
        sp.add_argument("--model", help="Model id (default: LLM_MODEL, else gemini-3.8-flash / claude-sonnet-5-5)")
        sp.add_argument("--max-attempts", type=int, help="Gate submissions (default: MAX_ATTEMPTS or 3)")
        sp.add_argument("--unsafe-local-sandbox", action="store_true",
                        help="Run repository code on the host (trusted fixtures only)")

    r = sub.add_parser("run", help="Repair one issue")
    r.add_argument("--repo", required=True)
    r.add_argument("--issue", required=True, help="Issue title / one-line description")
    r.add_argument("--body", default="", help="Issue body")
    r.add_argument("--issue-number", type=int, default=1)
    r.add_argument("--repro-test", required=True, help="Reproduction test file Cerberus holds the patch to")
    r.add_argument("--base", help="Base commit (default: HEAD)")
    r.add_argument("--run-id")
    common(r)
    r.set_defaults(func=_cmd_run)

    e = sub.add_parser("evaluate", help="Run the Phase 1 agent evaluation")
    e.add_argument("--dataset", default="phase1-v1", choices=["phase1-v1"])
    e.add_argument("--limit", type=int)
    e.add_argument("--only", nargs="*")
    e.add_argument("--experiment-id")
    common(e)
    e.set_defaults(func=_cmd_evaluate)

    d = sub.add_parser("demo", help="Scripted negative-path demo against the real gate")
    d.add_argument("--run-id")
    common(d)
    d.set_defaults(func=_cmd_demo)
    return p


def _load_dotenv() -> None:
    """Read autonomous-pr-fixer/.env (git-ignored) without overriding variables already set."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
        return
    load_dotenv(path, override=False)


def _use_project_tempdir() -> None:
    """Keep sandbox workspaces out of %TEMP%.

    On Windows a temp cleaner deleted a live agent workspace mid-run (files the agent had just
    read came back NOT_FOUND). Every Sandbox and gate run in this process creates its temporary
    directories through `tempfile`, so pointing `tempfile.tempdir` at artifacts/tmp moves them
    without changing harness/. CERBERUS_TMPDIR overrides the location.
    """
    root = os.environ.get("CERBERUS_TMPDIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "tmp")
    os.makedirs(root, exist_ok=True)
    tempfile.tempdir = root


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv()
    _use_project_tempdir()
    args = build_parser().parse_args(argv)
    return args.func(args)
