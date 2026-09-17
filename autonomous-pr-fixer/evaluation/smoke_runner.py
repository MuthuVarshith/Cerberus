"""
Smoke scenarios for the verification gates.

Five small synthetic repositories exercise the real RED gate, baseline, patch
loop, GREEN check, regression gate, scope gate and admission controller. The
reproduction tests and patches are scripted, not model-generated, so this
measures gate behaviour on known cases. It is not SWE-bench and says nothing
about repair ability on real issues.

Usage:
    python evaluation/smoke_runner.py                        # Docker sandbox (default)
    python evaluation/smoke_runner.py --unsafe-local-sandbox # trusted fixtures on the host
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.llm_patch_generator import ScriptedPatchGenerator
from agents.patch_agent import PatchAgent, PatchLoopResult
from agents.regression_agent import RegressionAgent
from agents.reproduction_agent import ReproductionAgent
from evaluation.metrics_reporter import MetricsReporter, RunRecord
from harness.admission_controller import AdmissionController
from harness.diff_utils import DiffUtils
from harness.docker_sandbox import Sandbox, SandboxError
from harness.scope_gate import analyze_scope


@dataclass
class Scenario:
    instance_id: str
    files: Dict[str, str]
    allowed: List[str]
    issue_title: str
    issue_body: str
    repro_test: str
    patches: List[str]


SCENARIOS: List[Scenario] = [
    Scenario(
        "SMOKE-001 (arithmetic_bug)",
        {"calc.py": "def add(a, b):\n    return a - b\n",
         "tests/test_calc.py": "from calc import add\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n"},
        ["calc.py"],
        "add subtracts instead of adding", "add(2, 3) gives -1",
        "from calc import add\n\ndef test_repro():\n    assert add(2, 3) == 5\n",
        ["--- a/calc.py\n+++ b/calc.py\n@@ -2 +2 @@\n-    return a - b\n+    return a + b\n"],
    ),
    Scenario(
        "SMOKE-002 (off_by_one_self_heal)",
        {"slicer.py": "def get_slice(items):\n    return items[:1]\n",
         "tests/test_slicer.py": "from slicer import get_slice\n\ndef test_slice_empty():\n    assert get_slice([]) == []\n"},
        ["slicer.py"],
        "get_slice misses second element", "get_slice([1, 2, 3]) should return the first two items",
        "from slicer import get_slice\n\ndef test_repro():\n    assert len(get_slice([1, 2, 3])) == 2\n",
        ["--- a/slicer.py\n+++ b/slicer.py\n@@ -2 +2 @@\n-    return items[:1]\n+    return items[:0]\n",
         "--- a/slicer.py\n+++ b/slicer.py\n@@ -2 +2 @@\n-    return items[:1]\n+    return items[:2]\n"],
    ),
    Scenario(
        "SMOKE-003 (non_reproducible_issue)",
        {"valid.py": "def is_positive(x):\n    return x > 0\n",
         "tests/test_valid.py": "from valid import is_positive\n\ndef test_one():\n    assert is_positive(1)\n"},
        ["valid.py"],
        "is_positive is wrong", "is_positive(5) should be True",
        "from valid import is_positive\n\ndef test_repro():\n    assert is_positive(5) is True\n",
        [],
    ),
    Scenario(
        "SMOKE-004 (regression_breaker)",
        {"core.py": "def process(x):\n    if x == 'new':\n        return 'ok_new'\n    return 'legacy_default'\n",
         "tests/test_core.py": "from core import process\n\ndef test_legacy():\n    assert process('old') == 'legacy_default'\n"},
        ["core.py"],
        "process ignores special", "process('special') should return 'special_val'",
        "from core import process\n\ndef test_repro():\n    assert process('special') == 'special_val'\n",
        ["--- a/core.py\n+++ b/core.py\n@@ -1,4 +1,4 @@\n def process(x):\n-    if x == 'new':\n-        return 'ok_new'\n-    return 'legacy_default'\n+    if x == 'special':\n+        return 'special_val'\n+    return None\n"],
    ),
    Scenario(
        "SMOKE-005 (scope_leak)",
        {"auth.py": "def get_user():\n    return 'admin'\n",
         "vault.py": "SECRET = 'unmodified'\n",
         "tests/test_auth.py": "from auth import get_user\n\ndef test_basic():\n    assert get_user() != ''\n"},
        ["auth.py"],
        "get_user returns the wrong role", "get_user() should return 'authenticated'",
        "from auth import get_user\n\ndef test_repro():\n    assert get_user() == 'authenticated'\n",
        ["--- a/auth.py\n+++ b/auth.py\n@@ -1,2 +1,2 @@\n def get_user():\n-    return 'admin'\n+    return 'authenticated'\n"
         "--- a/vault.py\n+++ b/vault.py\n@@ -1 +1 @@\n-SECRET = 'unmodified'\n+SECRET = 'leaked'\n"],
    ),
]


def run_scenario(scenario: Scenario, isolation: Optional[str] = None) -> RunRecord:
    started = time.monotonic()
    with Sandbox(isolation=isolation) as sb:
        for path, content in scenario.files.items():
            sb.write_file(path, content)
        sb.exec("git init -q")
        sb.exec("git config user.name \"Bot\"")
        sb.exec("git config user.email \"bot@localhost\"")
        sb.exec("git add -A")
        sb.exec("git commit -q -m \"initial\"")
        sb.base_commit = sb.exec("git rev-parse HEAD").stdout.strip()
        sb.ensure_harness_dir()

        repro_agent = ReproductionAgent(sb)
        red = repro_agent.run_reproduction_gate(scenario.issue_title, scenario.issue_body, scenario.repro_test)
        record = dict(
            instance_id=scenario.instance_id,
            reproduced=red.reproduced,
            top_1_correct=False,
            top_3_correct=False,
            target_passed=False,
            regression_clean=True,
            blast_radius_clean=True,
            admitted_for_pr=False,
            patch_attempts=0,
            total_tokens=0,  # no model in the loop; see tokens_measured
            runtime_sec=0.0,
            patch_size_lines=0,
            rejection_reason=None,
        )
        if not red.reproduced:
            record.update(rejection_reason=f"{red.refusal_code}: {red.error_message}", runtime_sec=round(time.monotonic() - started, 2))
            return RunRecord(**record)

        regr_agent = RegressionAgent(sb)
        test_cmd = f"{sb.python_cmd} -m pytest -q"
        baseline = regr_agent.record_baseline(test_cmd)
        if baseline.report is None:
            record.update(rejection_reason=f"BASELINE_UNVERIFIABLE: {baseline.error}", runtime_sec=round(time.monotonic() - started, 2))
            return RunRecord(**record)

        loop = PatchAgent(sb, max_attempts=3).run_patch_loop(
            scenario.allowed, ScriptedPatchGenerator(scenario.patches), verifier=repro_agent.green_verifier(red),
        )
        changes = DiffUtils.workspace_changes(sb)
        patch_res = PatchLoopResult(
            reached_green=loop.reached_green, total_attempts=loop.total_attempts,
            winning_diff=changes.diff_text, history=loop.history, total_lines_changed=changes.total_lines,
        )
        regression = regr_agent.run_regression_suite(test_cmd, baseline.report)
        scope = analyze_scope(sb, scenario.allowed, changes=changes)
        decision = AdmissionController.evaluate(patch_res, regression, scope)
        record.update(
            target_passed=loop.reached_green,
            regression_clean=regression.regression_free,
            blast_radius_clean=scope.is_acceptable,
            admitted_for_pr=decision.approved,
            patch_attempts=loop.total_attempts,
            patch_size_lines=changes.total_lines,
            rejection_reason=None if decision.approved else f"{decision.rejection_state}: {decision.rejection_summary}",
            runtime_sec=round(time.monotonic() - started, 2),
        )
        return RunRecord(**record)


def run_smoke_scenarios(isolation: Optional[str] = None) -> List[RunRecord]:
    return [run_scenario(s, isolation) for s in SCENARIOS]


def run_benchmark(isolation: Optional[str] = None) -> MetricsReporter:
    records = run_smoke_scenarios(isolation)
    reporter = MetricsReporter(records)
    print(reporter.format_markdown_table(title="Gate smoke scenarios (5 synthetic repos, scripted patches, measured sandbox runs)"))
    for r in records:
        outcome = "ADMITTED" if r.admitted_for_pr else f"REFUSED ({r.rejection_reason})"
        print(f"- {r.instance_id}: {outcome}")
    return reporter


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--unsafe-local-sandbox", action="store_true", help="Run fixtures on the host (trusted fixtures only).")
    args = parser.parse_args()
    try:
        run_benchmark("host-unsafe" if args.unsafe_local_sandbox else None)
    except SandboxError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
