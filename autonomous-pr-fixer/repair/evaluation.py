"""
Phase 1 agent evaluation: the 12 agent-capable tasks identified in the audit.

  9 tasks  one per reproducible bug of the frozen v1 benchmark. The bug, issue and a valid
           reproduction test come from benchmark/instances/v1.yml (read only); the fixed
           candidate patches are ignored. Hidden check: benchmark/hidden tests must all pass.
  3 tasks  the external upstream bugs in evaluation/external/cases.json at their pinned
           commits. Hidden check: the test file as it exists in the upstream fix commit, run
           against the agent's patch; every test that passes with the upstream fix must pass.

This is a separate measurement from the gate benchmark (fixed candidate patches) and is
never merged with it into one score. Frozen benchmark files are only read.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from repair.config import RepairConfig
from repair.metrics.repair_metrics import NOT_MEASURED, Measured, TaskResult, aggregate, task_result_from_state
from repair.prompts import PROMPT_VERSION
from repair.runner import RepairTask, run_repair

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_VERSION = "phase1-v1"
DEFAULT_RESULTS = os.path.join(ROOT, "results")
CACHE_DIR = os.path.join(ROOT, "artifacts", "repair-eval-cache")


@dataclass
class EvalTask:
    task_id: str
    source: str
    task: RepairTask
    ground_truth_files: List[str]
    hidden_check: Callable[[str], Measured]  # diff -> True / False / NOT_MEASURED


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


# ------------------------------------------------------------------ v1 tasks

def v1_tasks(work_dir: str, isolation: Optional[str]) -> List[EvalTask]:
    from evaluation.benchmark import REPRO_VALID_CATEGORIES, _all_pass, _hidden_tests, build_repo, load_instances, run_tests_with

    groups: Dict[tuple, list] = {}
    for inst in load_instances():
        groups.setdefault((inst.template, inst.issue["title"]), []).append(inst)
    tasks: List[EvalTask] = []
    for (template, title), insts in groups.items():
        valid = [i for i in insts if i.category in REPRO_VALID_CATEGORIES and i.bug]
        if not valid:
            continue  # tk-06: not reproducible by design
        chosen = next((i for i in valid if i.category == "correct-fix"), valid[0])
        repo = build_repo(chosen, os.path.join(work_dir, f"v1-{chosen.id}"))
        hidden = _hidden_tests(template)

        def check(diff: str, repo=repo, hidden=hidden) -> Measured:
            applied, report = run_tests_with(repo, diff, hidden, isolation)
            return bool(applied and _all_pass(report))

        tasks.append(EvalTask(
            task_id=f"v1-{template}-{_slug(title)}", source=f"benchmark v1 ({chosen.id} issue and reproduction test)",
            task=RepairTask(repo_dir=repo, issue_id=f"#{chosen.issue['number']}", title=chosen.issue["title"],
                            body=chosen.issue["body"], reproduction_test=chosen.reproduction_test,
                            issue_number=chosen.issue["number"]),
            ground_truth_files=sorted({op["file"] for op in chosen.bug}),
            hidden_check=check,
        ))
    return tasks


# ------------------------------------------------------------ external tasks

def _git(cwd: str, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout


def _external_hidden_passing(repo: str, base: str, diff: str, test_path: str, test_content: str,
                             isolation: Optional[str]) -> Optional[set]:
    """Test ids passing in `test_path` (upstream fix version) with `diff` applied, or None if unusable."""
    from agents.repo_setup_agent import RepoSetupAgent
    from harness.diff_utils import DiffUtils
    from harness.docker_sandbox import HARNESS_DIR, Sandbox
    from harness.junit import JUnitReportError, read_workspace_junit_report

    with Sandbox(base_dir=repo, isolation=isolation, base_ref=base, timeout_sec=600) as sb:
        plan = RepoSetupAgent(sb).detect()
        if any(r.exit_code != 0 for r in sb.run_setup(plan.install_commands, timeout=600)):
            return None
        if not DiffUtils.apply_diff_to_sandbox(sb, diff).success:
            return set()
        sb.write_file(test_path, test_content)
        sb.ensure_harness_dir()
        report_rel = f"{HARNESS_DIR}/hidden.xml"
        sb.exec(f"{sb.python_cmd} -m pytest {test_path} -p no:cacheprovider -q --junitxml={report_rel}", timeout=600)
        try:
            report = read_workspace_junit_report(sb, report_rel)
        except JUnitReportError:
            return None
        return {c.test_id for c in report.cases if c.outcome == "passed"}


def external_tasks(work_dir: str, isolation: Optional[str]) -> List[EvalTask]:
    from evaluation.external_repos import SPEC_DIR, checkout, mirror, upstream_inputs

    with open(os.path.join(SPEC_DIR, "cases.json"), encoding="utf-8") as f:
        spec = json.load(f)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tasks: List[EvalTask] = []
    for bug in spec["bugs"]:
        src = mirror(CACHE_DIR, bug["repo"], spec["repos"][bug["repo"]]["url"])
        inputs = upstream_inputs(src, bug)
        workdir = os.path.join(work_dir, f"ext-{bug['id']}")
        checkout(src, workdir, inputs["base"])
        fix = bug["fix_commit"]
        truth = [p for p in _git(src, "show", "--name-only", "--format=", fix, "--", *bug["source_paths"]).splitlines() if p.strip()]
        test_at_fix = _git(src, "show", f"{fix}:{bug['test_path']}")
        reference: Dict[str, Any] = {}

        def check(diff: str, workdir=workdir, base=inputs["base"], bug=bug, test_at_fix=test_at_fix,
                  fix_diff=inputs["fix_diff"], reference=reference) -> Measured:
            if "passing" not in reference:
                reference["passing"] = _external_hidden_passing(workdir, base, fix_diff, bug["test_path"], test_at_fix, isolation)
            ref = reference["passing"]
            got = _external_hidden_passing(workdir, base, diff, bug["test_path"], test_at_fix, isolation)
            if not ref or got is None:
                return NOT_MEASURED
            return ref.issubset(got)

        issue = bug["issue"]
        tasks.append(EvalTask(
            task_id=f"ext-{bug['id']}", source=f"{bug['repo']} @ {inputs['base'][:12]} (upstream fix {fix[:12]})",
            task=RepairTask(repo_dir=workdir, issue_id=f"#{issue['number']}", title=issue["title"], body=issue["body"],
                            reproduction_test=inputs["repro"], base_ref=inputs["base"], issue_number=int(issue["number"])),
            ground_truth_files=truth,
            hidden_check=check,
        ))
    return tasks


def build_tasks(work_dir: str, isolation: Optional[str]) -> List[EvalTask]:
    return v1_tasks(work_dir, isolation) + external_tasks(work_dir, isolation)


# --------------------------------------------------------------------- runner

def _git_commit() -> str:
    try:
        return _git(ROOT, "rev-parse", "HEAD").strip()
    except RuntimeError:
        return "unknown"


def _dirty() -> bool:
    try:
        return bool(_git(ROOT, "status", "--porcelain", "--", "repair", "main.py").strip())
    except RuntimeError:
        return True


def render_report(summary: Dict[str, Any], results: Sequence[TaskResult]) -> str:
    m = summary["metrics"]
    cfg = summary["config"]

    def money(v: Optional[float]) -> str:
        return "n/a" if v is None else f"${v:.4f}"

    lines = [
        f"# Agent evaluation {summary['experiment_id']}",
        "",
        f"Dataset `{summary['dataset']}` ({len(results)} tasks) · model `{cfg['model']}` · prompt `{summary['prompt_version']}` · "
        f"Cerberus `{summary['git_commit'][:12]}`{' (dirty)' if summary['git_dirty'] else ''} · "
        f"max attempts {cfg['max_attempts']} · tool budget {cfg['max_tool_calls']} · token budget {cfg['max_agent_tokens']} · "
        f"cost budget ${cfg['max_cost_per_task']}/task, ${cfg['max_evaluation_cost']} total.",
        "",
        "LLM-generated patches judged by the unchanged Cerberus gate. This is not comparable with the gate benchmark",
        "(`benchmark/reports/v1-docker.md`), which judges fixed candidate patches.",
        "",
        "| Task | Status | Attempts | Time (s) | Cost | Tokens | Patch files | Gate results | Failure stage | Hidden check |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | {r.final_status} | {r.attempts} | {r.duration_seconds:.0f} | {money(r.cost_usd)} | {r.total_tokens} | "
            f"{', '.join(r.patch_files) or '-'} | {' → '.join(r.gate_results) or '-'} | {r.failure_stage or '-'} | {r.hidden_check} |"
        )
    lines += ["", "## Metrics", "", "```json", json.dumps(m, indent=2), "```", ""]
    return "\n".join(lines)


def evaluate(config: RepairConfig, limit: Optional[int] = None, only: Optional[Sequence[str]] = None,
             model_factory: Optional[Callable[[], Any]] = None, results_root: str = DEFAULT_RESULTS,
             experiment_id: Optional[str] = None, tasks: Optional[List[EvalTask]] = None,
             printer: Callable[[str], None] = print) -> Dict[str, Any]:
    from repair.llm.provider import create_model

    experiment_id = experiment_id or f"exp-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out = os.path.join(results_root, experiment_id)
    if os.path.exists(out):
        raise FileExistsError(f"{out} exists; experiments are never overwritten.")
    os.makedirs(out)
    with tempfile.TemporaryDirectory(prefix="cerberus_agent_eval_") as work:
        if tasks is None:
            tasks = build_tasks(work, config.isolation)
        if only:
            tasks = [t for t in tasks if t.task_id in set(only)]
        if limit is not None:
            tasks = tasks[:limit]
        cfg_record = {
            "experiment_id": experiment_id,
            "dataset": DATASET_VERSION,
            "git_commit": _git_commit(),
            "git_dirty": _dirty(),
            "prompt_version": PROMPT_VERSION,
            "config": config.model_dump(),
            "task_ids": [t.task_id for t in tasks],
            "task_sources": {t.task_id: t.source for t in tasks},
            "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        }
        with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg_record, f, indent=2)

        results: List[TaskResult] = []
        spent = 0.0
        for t in tasks:
            if spent >= config.max_evaluation_cost:
                r = TaskResult(task_id=t.task_id, final_status="SKIPPED_BUDGET", admitted=False, attempts=0,
                               duration_seconds=0, input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0.0,
                               tool_calls=0, ground_truth_files=t.ground_truth_files,
                               note=f"evaluation cost budget ${config.max_evaluation_cost} reached")
            else:
                printer(f"\n=== {t.task_id} ({t.source}) ===")
                model = model_factory() if model_factory else create_model(config.provider, config.model, config.effort, config.server_fallbacks)
                state = run_repair(t.task, config, model=model, run_id=f"{experiment_id}-{t.task_id}",
                                   out_root=os.path.join(out, "runs"), printer=printer)
                hidden: Measured = NOT_MEASURED
                if state.final_status and state.final_status.value == "ADMITTED":
                    printer("  hidden check: running independent tests on the admitted patch")
                    hidden = t.hidden_check(state.generated_patch)
                r = task_result_from_state(t.task_id, state, t.ground_truth_files, hidden)
                spent += state.estimated_cost or 0.0
            results.append(r)
            with open(os.path.join(out, "task-results.jsonl"), "a", encoding="utf-8") as f:
                f.write(r.model_dump_json() + "\n")

    metrics = aggregate(results)
    summary = {**cfg_record, "finished_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(), "metrics": metrics}
    with open(os.path.join(out, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(summary, results))
    printer(f"\nResults: {out}")
    return summary
