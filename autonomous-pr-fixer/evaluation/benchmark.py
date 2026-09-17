"""
Cerberus benchmark runner.

Measures the verification gate's decisions on a frozen set of instances, using
hidden tests as ground truth, and compares them with an ungated baseline
measured on the same instances.

    python evaluation/benchmark.py --validate [--unsafe-local-sandbox]   # authoring integrity, no Cerberus run
    python evaluation/benchmark.py --freeze                               # write benchmark/MANIFEST.json
    python evaluation/benchmark.py [--unsafe-local-sandbox]               # scored run (requires an unchanged manifest)

Definitions (per instance):
  hidden_correct   the candidate applies and every hidden test passes
  ungated admits   the candidate applies and the reproduction test passes with it
                   (what an agent that stops at "my test passes" would ship)
  false admission  admitted although the ideal decision is REFUSED
  rejection correct refused, the ideal decision is REFUSED, and the refusal code is acceptable

Nothing in the report is typed in by hand: every number is computed from the run.
Instances are authored by the Cerberus developer, which biases them; the report says so.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import io
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.patch_sources import DiffPatchSource, tree_diff
from harness.diff_utils import DiffUtils
from harness.docker_sandbox import HARNESS_DIR, ISOLATION_HOST_UNSAFE, Sandbox, SandboxError, host_unsafe_environment
from harness.junit import PASSED, JUnitReportError, TestReport, read_junit_report

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BENCHMARK_DIR = os.path.join(ROOT, "benchmark")
INSTANCES_FILE = os.path.join(BENCHMARK_DIR, "instances", "v1.yml")
MANIFEST_FILE = os.path.join(BENCHMARK_DIR, "MANIFEST.json")
VERSION = "v1"

REPRO_VALID_CATEGORIES = {"correct-fix", "plausible-wrong-fix", "regression", "scope-leak", "test-weakening", "ineffective-patch"}


class BenchmarkError(RuntimeError):
    pass


@dataclass
class Instance:
    id: str
    category: str
    template: str
    issue: Dict[str, Any]
    bug: List[Dict[str, Any]]
    reproduction_test: str
    candidate: List[Dict[str, Any]]
    expected_decision: str
    refusal_codes: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ loading

def load_instances(path: str = INSTANCES_FILE) -> List[Instance]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out: List[Instance] = []
    seen = set()
    for raw in data["instances"]:
        inst = Instance(
            id=raw["id"],
            category=raw["category"],
            template=raw["template"],
            issue=raw["issue"],
            bug=raw.get("bug") or [],
            reproduction_test=raw["reproduction_test"],
            candidate=raw["candidate"],
            expected_decision=raw["expected"]["decision"],
            refusal_codes=raw["expected"].get("refusal_codes", []),
        )
        if inst.id in seen:
            raise BenchmarkError(f"duplicate instance id {inst.id}")
        if inst.expected_decision not in ("ADMITTED", "REFUSED"):
            raise BenchmarkError(f"{inst.id}: expected.decision must be ADMITTED or REFUSED")
        if not os.path.isdir(os.path.join(BENCHMARK_DIR, "templates", inst.template)):
            raise BenchmarkError(f"{inst.id}: unknown template {inst.template}")
        seen.add(inst.id)
        out.append(inst)
    return out


def manifest_files() -> List[str]:
    files = []
    for sub in ("instances", "templates", "hidden"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(BENCHMARK_DIR, sub)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                files.append(os.path.relpath(os.path.join(dirpath, name), BENCHMARK_DIR).replace("\\", "/"))
    return sorted(files)


def compute_manifest() -> Dict[str, Any]:
    entries = {}
    for rel in manifest_files():
        with open(os.path.join(BENCHMARK_DIR, rel), "rb") as f:
            # Normalise line endings so a checkout's autocrlf setting doesn't unfreeze the set.
            entries[rel] = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return {"version": VERSION, "digest": digest, "files": entries}


def check_manifest() -> str:
    if not os.path.isfile(MANIFEST_FILE):
        raise BenchmarkError("benchmark/MANIFEST.json is missing; validate and then freeze the instance set first.")
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        frozen = json.load(f)
    current = compute_manifest()
    if frozen["digest"] != current["digest"]:
        changed = sorted(
            set(k for k in set(frozen["files"]) | set(current["files"]) if frozen["files"].get(k) != current["files"].get(k))
        )
        raise BenchmarkError(f"benchmark files changed since freezing: {', '.join(changed)}")
    return current["digest"]


# --------------------------------------------------------------- repository

def _git(cwd: str, *args: str) -> None:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=host_unsafe_environment())
    if res.returncode != 0:
        raise BenchmarkError(f"git {' '.join(args)} failed: {res.stderr.strip()}")


def apply_ops(root: str, ops: Sequence[Dict[str, Any]], where: str) -> None:
    for op in ops:
        path = os.path.join(root, op["file"])
        if op.get("delete"):
            if not os.path.isfile(path):
                raise BenchmarkError(f"{where}: cannot delete missing {op['file']}")
            os.remove(path)
        elif "content" in op:
            os.makedirs(os.path.dirname(path) or root, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(op["content"])
        else:
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
            count = text.count(op["find"])
            if count != 1:
                raise BenchmarkError(f"{where}: {op['find']!r} occurs {count} times in {op['file']}")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text.replace(op["find"], op["replace"]))


def _copy_template(template: str, dest: str) -> None:
    shutil.copytree(
        os.path.join(BENCHMARK_DIR, "templates", template), dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def build_repo(inst: Instance, dest: str, with_bug: bool = True) -> str:
    """A one-commit git repository: the template, with the bug seeded unless with_bug is False."""
    _copy_template(inst.template, dest)
    if with_bug:
        apply_ops(dest, inst.bug, f"{inst.id} bug")
    _git(dest, "init", "-q")
    _git(dest, "config", "user.name", "benchmark")
    _git(dest, "config", "user.email", "benchmark@localhost")
    _git(dest, "config", "core.autocrlf", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"{inst.id} base")
    return dest


def ops_diff(repo: str, ops: Sequence[Dict[str, Any]], where: str) -> str:
    """Unified diff that applies `ops` to the working tree of `repo`."""
    with tempfile.TemporaryDirectory(prefix="cerberus_bench_diff_") as tmp:
        before = os.path.join(tmp, "before")
        after = os.path.join(tmp, "after")
        ignore = shutil.ignore_patterns(".git", "__pycache__")
        shutil.copytree(repo, before, ignore=ignore)
        shutil.copytree(repo, after, ignore=ignore)
        apply_ops(after, ops, where)
        return tree_diff(before, after)


def reference_ops(inst: Instance) -> List[Dict[str, Any]]:
    ops = []
    for op in inst.bug:
        if "find" not in op:
            raise BenchmarkError(f"{inst.id}: bug operations must be find/replace to derive a reference fix")
        ops.append({"file": op["file"], "find": op["replace"], "replace": op["find"]})
    return ops


def run_tests_with(
    repo: str,
    diff: Optional[str],
    test_files: Dict[str, str],
    isolation: Optional[str],
    timeout: int = 300,
) -> Tuple[bool, Optional[TestReport]]:
    """Apply `diff` (if any) to a sandboxed clone of `repo`, run the given test files, return (applied, report)."""
    with Sandbox(base_dir=repo, isolation=isolation, timeout_sec=timeout) as sb:
        if diff:
            if not DiffUtils.apply_diff_to_sandbox(sb, diff).success:
                return False, None
        sb.ensure_harness_dir()
        paths = []
        for name, content in test_files.items():
            rel = f"{HARNESS_DIR}/eval/{name}"
            sb.write_file(rel, content)
            paths.append(rel)
        report_rel = f"{HARNESS_DIR}/eval.xml"
        sb.exec(f"{sb.python_cmd} -m pytest {' '.join(paths)} -p no:cacheprovider -q --junitxml={report_rel}", timeout=timeout)
        try:
            return True, read_junit_report(os.path.join(sb.workspace_dir, report_rel))
        except JUnitReportError:
            return True, None


def _all_pass(report: Optional[TestReport]) -> bool:
    return report is not None and report.total > 0 and all(c.outcome == PASSED for c in report.cases)


def _hidden_tests(template: str) -> Dict[str, str]:
    with open(os.path.join(BENCHMARK_DIR, "hidden", f"test_hidden_{template}.py"), "r", encoding="utf-8") as f:
        return {f"test_hidden_{template}.py": f.read()}


# ---------------------------------------------------------------- validate

def validate(instances: Sequence[Instance], isolation: Optional[str]) -> List[str]:
    """Check authoring integrity without running Cerberus. Returns a list of problems."""
    problems: List[str] = []
    checked_templates = set()
    with tempfile.TemporaryDirectory(prefix="cerberus_bench_validate_") as tmp:
        for inst in instances:
            work = os.path.join(tmp, inst.id)
            os.makedirs(work)
            hidden = _hidden_tests(inst.template)
            if inst.template not in checked_templates:
                clean = build_repo(inst, os.path.join(work, "clean"), with_bug=False)
                _, report = run_tests_with(clean, None, hidden, isolation)
                if not _all_pass(report):
                    problems.append(f"{inst.template}: hidden tests do not all pass on the clean template")
                checked_templates.add(inst.template)
            try:
                base = build_repo(inst, os.path.join(work, "base"))
                candidate = ops_diff(base, inst.candidate, f"{inst.id} candidate")
            except BenchmarkError as exc:
                problems.append(str(exc))
                continue
            applied, _ = run_tests_with(base, candidate, {}, isolation) if candidate else (False, None)
            if not applied:
                problems.append(f"{inst.id}: candidate patch does not apply")
            repro = {"test_reproduce.py": inst.reproduction_test}
            if inst.bug:
                _, hidden_base = run_tests_with(base, None, hidden, isolation)
                if _all_pass(hidden_base):
                    problems.append(f"{inst.id}: hidden tests pass on the seeded bug, so the bug is not observable")
                try:
                    reference = ops_diff(base, reference_ops(inst), f"{inst.id} reference")
                except BenchmarkError as exc:
                    problems.append(str(exc))
                    continue
                _, hidden_ref = run_tests_with(base, reference, hidden, isolation)
                if not _all_pass(hidden_ref):
                    problems.append(f"{inst.id}: hidden tests do not pass with the reference fix")
                if inst.category in REPRO_VALID_CATEGORIES:
                    _, repro_base = run_tests_with(base, None, repro, isolation)
                    if repro_base is None or _all_pass(repro_base):
                        problems.append(f"{inst.id}: reproduction test does not fail on the seeded bug")
                    _, repro_ref = run_tests_with(base, reference, repro, isolation)
                    if not _all_pass(repro_ref):
                        problems.append(f"{inst.id}: reproduction test does not pass with the reference fix")
            print(f"validated {inst.id}")
    return problems


# --------------------------------------------------------------------- run

def evaluate_instance(inst: Instance, isolation: Optional[str], out_dir: str) -> Dict[str, Any]:
    import main

    with tempfile.TemporaryDirectory(prefix="cerberus_bench_") as tmp:
        base = build_repo(inst, os.path.join(tmp, "base"))
        candidate = ops_diff(base, inst.candidate, f"{inst.id} candidate")
        base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base, capture_output=True, text=True).stdout.strip()

        runs_dir = os.path.join(out_dir, "runs")
        log_path = os.path.join(out_dir, "logs", f"{inst.id}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        previous = os.environ.get("RUN_ARTIFACTS_DIR")
        os.environ["RUN_ARTIFACTS_DIR"] = runs_dir
        run_id = f"bench_{inst.id}"
        started = time.monotonic()
        try:
            with open(log_path, "w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                admitted = main.run_pipeline(
                    repo_dir=base,
                    issue_number=inst.issue["number"],
                    issue_title=inst.issue["title"],
                    issue_body=inst.issue["body"],
                    repro_test_code=inst.reproduction_test,
                    patch_source=DiffPatchSource([candidate], name=f"benchmark candidate {inst.id}"),
                    sandbox_isolation=isolation,
                    run_id=run_id,
                )
        finally:
            if previous is None:
                os.environ.pop("RUN_ARTIFACTS_DIR", None)
            else:
                os.environ["RUN_ARTIFACTS_DIR"] = previous
        wall = time.monotonic() - started
        with open(os.path.join(runs_dir, run_id, "run.json"), "r", encoding="utf-8") as f:
            artifact = json.load(f)

        hidden_applied, hidden_report = run_tests_with(base, candidate, _hidden_tests(inst.template), isolation)
        _, repro_with_candidate = run_tests_with(base, candidate, {"test_reproduce.py": inst.reproduction_test}, isolation)
        repro_valid_reference: Optional[bool] = None
        if inst.bug and artifact.get("red_gate") and artifact["red_gate"].get("reproduced"):
            reference = ops_diff(base, reference_ops(inst), f"{inst.id} reference")
            _, repro_ref = run_tests_with(base, reference, {"test_reproduce.py": inst.reproduction_test}, isolation)
            repro_valid_reference = _all_pass(repro_ref)

    refusal = artifact.get("refusal") or {}
    return {
        "id": inst.id,
        "category": inst.category,
        "expected_decision": inst.expected_decision,
        "acceptable_refusal_codes": inst.refusal_codes,
        "base_commit": base_sha,
        "final_state": artifact["final_state"],
        "admitted": bool(admitted),
        "refusal_code": refusal.get("code"),
        "refusal_stage": refusal.get("stage"),
        "red_passed": bool(artifact.get("red_gate") and artifact["red_gate"].get("reproduced")),
        "reproduction_valid_on_reference": repro_valid_reference,
        "patch_attempts": artifact.get("patch_attempts", 0),
        "wall_time_sec": round(wall, 2),
        "candidate_applies": hidden_applied,
        "hidden_correct": hidden_applied and _all_pass(hidden_report),
        "ungated_admits": hidden_applied and _all_pass(repro_with_candidate),
        "tokens": None,
    }


def wilson(k: int, n: int, z: float = 1.96) -> Optional[Tuple[float, float]]:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def _rate(k: int, n: int) -> Dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "wilson_95": wilson(k, n)}


def _code_ok(r: Dict[str, Any]) -> bool:
    codes = r["acceptable_refusal_codes"]
    return "*" in codes or r["refusal_code"] in codes


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    expected_admit = [r for r in results if r["expected_decision"] == "ADMITTED"]
    expected_refuse = [r for r in results if r["expected_decision"] == "REFUSED"]
    admitted = [r for r in results if r["admitted"]]
    ungated = [r for r in results if r["ungated_admits"]]
    red_passed = [r for r in results if r["red_passed"] and r["reproduction_valid_on_reference"] is not None]
    times = [r["wall_time_sec"] for r in results]
    attempts = [r["patch_attempts"] for r in results if r["patch_attempts"]]
    return {
        "instances": len(results),
        "expected_admit": len(expected_admit),
        "expected_refuse": len(expected_refuse),
        "cerberus": {
            "admitted": len(admitted),
            "admitted_fix_rate": _rate(sum(1 for r in expected_admit if r["admitted"] and r["hidden_correct"]), len(expected_admit)),
            "false_admission_rate": _rate(sum(1 for r in admitted if r["expected_decision"] == "REFUSED"), len(admitted)),
            "admitted_code_failing_hidden_tests": sum(1 for r in admitted if not r["hidden_correct"]),
            "false_rejection_rate": _rate(sum(1 for r in expected_admit if not r["admitted"]), len(expected_admit)),
            "rejection_accuracy": _rate(sum(1 for r in expected_refuse if not r["admitted"] and _code_ok(r)), len(expected_refuse)),
            "refused_when_should_refuse": _rate(sum(1 for r in expected_refuse if not r["admitted"]), len(expected_refuse)),
            "reproduction_validity": _rate(sum(1 for r in red_passed if r["reproduction_valid_on_reference"]), len(red_passed)),
            "refusals_by_code": dict(Counter(r["refusal_code"] for r in results if not r["admitted"])),
            "mean_patch_attempts": round(statistics.mean(attempts), 2) if attempts else None,
            "wall_time_sec": {
                "median": round(statistics.median(times), 2) if times else None,
                "p90": round(sorted(times)[max(0, math.ceil(0.9 * len(times)) - 1)], 2) if times else None,
            },
            "tokens": "not measured (no model in the loop)",
        },
        "ungated_baseline": {
            "definition": "admit when the candidate applies and the reproduction test passes with it",
            "admitted": len(ungated),
            "false_admission_rate": _rate(sum(1 for r in ungated if r["expected_decision"] == "REFUSED"), len(ungated)),
            "admitted_code_failing_hidden_tests": sum(1 for r in ungated if not r["hidden_correct"]),
            "admitted_fix_rate": _rate(sum(1 for r in expected_admit if r["ungated_admits"] and r["hidden_correct"]), len(expected_admit)),
        },
    }


def _fmt_rate(entry: Dict[str, Any]) -> str:
    if entry["n"] == 0:
        return "n/a (0 cases)"
    lo, hi = entry["wilson_95"]
    return f"{entry['k']}/{entry['n']} = {entry['rate']:.0%} (95% CI {lo:.0%}–{hi:.0%})"


def render_report(summary: Dict[str, Any], results: Sequence[Dict[str, Any]], provenance: Dict[str, Any]) -> str:
    c = summary["cerberus"]
    u = summary["ungated_baseline"]
    lines = [
        f"# Cerberus benchmark {VERSION} results",
        "",
        f"- Date (UTC): {provenance['date']}",
        f"- Cerberus commit: `{provenance['cerberus_commit']}`",
        f"- Benchmark manifest digest: `{provenance['manifest_digest']}`",
        f"- Sandbox: `{provenance['isolation']}`; Python {provenance['python']}; {provenance['platform']}",
        f"- Patch source: pre-written candidate diffs (no model in the loop)",
        "",
        "> Instances are small synthetic libraries with seeded bugs, authored by the Cerberus developer. This measures",
        "> the gate's decisions on known cases, not repair ability or performance on real-world repositories.",
        "",
        "## Decisions",
        "",
        "| Metric | Cerberus | Ungated baseline |",
        "| --- | --- | --- |",
        f"| Admitted | {c['admitted']} | {u['admitted']} |",
        f"| False-admission rate (admitted but should be refused) | {_fmt_rate(c['false_admission_rate'])} | {_fmt_rate(u['false_admission_rate'])} |",
        f"| Admitted code failing hidden tests | {c['admitted_code_failing_hidden_tests']} | {u['admitted_code_failing_hidden_tests']} |",
        f"| Admitted-fix rate (correct fixes admitted) | {_fmt_rate(c['admitted_fix_rate'])} | {_fmt_rate(u['admitted_fix_rate'])} |",
        "",
        "## Cerberus only",
        "",
        f"- Rejection accuracy (refused with an acceptable code): {_fmt_rate(c['rejection_accuracy'])}",
        f"- Refused when it should refuse (any code): {_fmt_rate(c['refused_when_should_refuse'])}",
        f"- False-rejection rate: {_fmt_rate(c['false_rejection_rate'])}",
        f"- Reproduction validity (RED-passing tests that pass on the reference fix): {_fmt_rate(c['reproduction_validity'])}",
        f"- Refusals by code: {json.dumps(c['refusals_by_code'], sort_keys=True)}",
        f"- Mean patch attempts: {c['mean_patch_attempts']}",
        f"- Wall time per instance: median {c['wall_time_sec']['median']}s, p90 {c['wall_time_sec']['p90']}s (includes sandbox setup and repeated RED/GREEN runs)",
        f"- Cost per admitted fix: {c['tokens']}",
        "",
        "## Per instance",
        "",
        "| Instance | Category | Ideal | Cerberus | Code | Hidden tests | Ungated |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['category']} | {r['expected_decision']} | {r['final_state']} | {r['refusal_code'] or ''} | "
            f"{'pass' if r['hidden_correct'] else 'fail'} | {'admit' if r['ungated_admits'] else 'refuse'} |"
        )
    return "\n".join(lines) + "\n"


def _cerberus_commit() -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return (res.stdout.strip() or "unknown") + ("+uncommitted" if dirty else "")


def main_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--validate", action="store_true", help="check instance authoring; does not run Cerberus")
    parser.add_argument("--freeze", action="store_true", help="write benchmark/MANIFEST.json")
    parser.add_argument("--only", nargs="*", help="run a subset of instance ids (unscored exploration)")
    parser.add_argument("--output", help="results directory (default benchmark/results/<version>-<timestamp>-<sandbox>)")
    parser.add_argument("--unsafe-local-sandbox", action="store_true", help="run on the host (benchmark fixtures are trusted)")
    args = parser.parse_args(argv)
    isolation = ISOLATION_HOST_UNSAFE if args.unsafe_local_sandbox else None

    try:
        instances = load_instances()
        if args.freeze:
            manifest = compute_manifest()
            with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, sort_keys=True)
                f.write("\n")
            print(f"froze {len(manifest['files'])} files, digest {manifest['digest']}")
            return 0
        if args.validate:
            problems = validate(instances, isolation)
            for p in problems:
                print(f"PROBLEM: {p}")
            print(f"{len(instances)} instances validated, {len(problems)} problem(s)")
            return 1 if problems else 0

        digest = check_manifest()
        if args.only:
            instances = [i for i in instances if i.id in set(args.only)]
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = args.output or os.path.join(BENCHMARK_DIR, "results", f"{VERSION}-{stamp}-{isolation or 'docker'}")
        os.makedirs(out_dir, exist_ok=True)

        results = []
        for inst in instances:
            record = evaluate_instance(inst, isolation, out_dir)
            results.append(record)
            print(f"{inst.id}: {record['final_state']} {record['refusal_code'] or ''} (hidden {'pass' if record['hidden_correct'] else 'fail'})")

        provenance = {
            "date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "cerberus_commit": _cerberus_commit(),
            "manifest_digest": digest,
            "isolation": isolation or "docker",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "subset": args.only or None,
        }
        summary = summarize(results)
        with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump({"provenance": provenance, "summary": summary, "instances": results}, f, indent=2)
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(render_report(summary, results, provenance))
        print(f"results: {out_dir}")
        return 0
    except (BenchmarkError, SandboxError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main_cli())
