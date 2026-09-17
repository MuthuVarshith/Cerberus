"""
Smoke scenarios for the verification gates.

Five small synthetic repositories, each built in a sandbox, exercise the real
RED gate, patch loop, regression agent, and admission controller. The patches are
scripted, not model-generated, so this measures gate behaviour on known cases.
It is not SWE-bench and says nothing about repair ability on real issues.
"""
from __future__ import annotations

import os
import sys
import time
from typing import List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from harness.admission_controller import AdmissionController
from agents.reproduction_agent import ReproductionAgent
from agents.patch_agent import PatchAgent
from agents.regression_agent import RegressionAgent
from evaluation.metrics_reporter import MetricsReporter, RunRecord


def run_smoke_scenarios() -> List[RunRecord]:
    """Runs the 5-instance smoke test suite.

    The gate outcomes in these records are measured: each scenario really builds a
    repo, runs the RED gate, applies a diff, and runs a regression suite in a
    sandbox. Token counts and localization accuracy are not — no model is in the
    loop — so those fields stay unset and the reporter labels them accordingly.
    """
    records: List[RunRecord] = []

    # 1. Arithmetic bug
    started = time.monotonic()
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("calc.py", "def add(a, b):\n    return a - b\n")
        sb.write_file("test_legacy.py", "from calc import add\ndef test_add_zero(): assert add(0, 0) == 0\n")
        sb.exec("git add -A && git commit -m 'initial'")

        repro_agent = ReproductionAgent(sb)
        repro_res = repro_agent.run_reproduction_gate(
            "add subtracts instead of adding",
            "add(2, 3) gives -1",
            "from calc import add\ndef test_repro(): assert add(2, 3) == 5\n",
        )
        patch_agent = PatchAgent(sb, max_attempts=3)
        diff_1 = "--- a/calc.py\n+++ b/calc.py\n@@ -2 +2 @@\n-    return a - b\n+    return a + b\n"
        patch_res = patch_agent.run_patch_loop(["calc.py"], lambda att, fb: diff_1)

        regr_agent = RegressionAgent(sb)
        regr_res = regr_agent.run_regression_suite(["calc.py"], test_suite_cmd=f"{sb.python_cmd} -m pytest test_legacy.py")
        decision = AdmissionController.evaluate(patch_res, regr_res)

        records.append(
            RunRecord(
                instance_id="SMOKE-001 (arithmetic_bug)",
                reproduced=repro_res.reproduced,
                top_1_correct=True,
                top_3_correct=True,
                target_passed=patch_res.reached_green,
                regression_clean=regr_res.all_tests_passed,
                blast_radius_clean=regr_res.blast_radius.is_acceptable,
                admitted_for_pr=decision.approved,
                patch_attempts=patch_res.total_attempts,
                total_tokens=0,  # no model in the loop; see tokens_measured
                runtime_sec=round(time.monotonic() - started, 2),
                patch_size_lines=patch_res.total_lines_changed,
            )
        )

    # 2. Parsing self-heals
    started = time.monotonic()
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("parser.py", "def get_slice(items):\n    return items[:1]\n")
        sb.write_file("test_slice_legacy.py", "from parser import get_slice\ndef test_slice_empty(): assert get_slice([]) == []\n")
        sb.exec("git add -A && git commit -m 'initial'")

        repro_agent = ReproductionAgent(sb)
        repro_res = repro_agent.run_reproduction_gate(
            "get_slice misses second element",
            "Expected items[:2]",
            "from parser import get_slice\ndef test_repro(): assert len(get_slice([1,2,3])) == 2\n",
        )
        patch_agent = PatchAgent(sb, max_attempts=3)
        def self_heal_gen(att: int, fb: str):
            if att == 1:
                return "--- a/parser.py\n+++ b/parser.py\n@@ -2 +2 @@\n-    return items[:1]\n+    return items[:0]\n"
            return "--- a/parser.py\n+++ b/parser.py\n@@ -2 +2 @@\n-    return items[:1]\n+    return items[:2]\n"

        patch_res = patch_agent.run_patch_loop(["parser.py"], self_heal_gen)
        regr_agent = RegressionAgent(sb)
        regr_res = regr_agent.run_regression_suite(["parser.py"], test_suite_cmd=f"{sb.python_cmd} -m pytest test_slice_legacy.py")
        decision = AdmissionController.evaluate(patch_res, regr_res)

        records.append(
            RunRecord(
                instance_id="SMOKE-002 (off_by_one_self_heal)",
                reproduced=repro_res.reproduced,
                top_1_correct=True,
                top_3_correct=True,
                target_passed=patch_res.reached_green,
                regression_clean=regr_res.all_tests_passed,
                blast_radius_clean=regr_res.blast_radius.is_acceptable,
                admitted_for_pr=decision.approved,
                patch_attempts=patch_res.total_attempts,
                total_tokens=0,  # no model in the loop; see tokens_measured
                runtime_sec=round(time.monotonic() - started, 2),
                patch_size_lines=patch_res.total_lines_changed,
            )
        )

    # 3. Non-reproducible
    started = time.monotonic()
    with Sandbox() as sb:
        sb.write_file("valid.py", "def is_positive(x): return x > 0\n")
        repro_agent = ReproductionAgent(sb)
        repro_res = repro_agent.run_reproduction_gate(
            "Fake bug", "Does not fail",
            "from valid import is_positive\ndef test_repro(): assert is_positive(5) is True\n"
        )
        records.append(
            RunRecord(
                instance_id="SMOKE-003 (non_reproducible_issue)",
                reproduced=repro_res.reproduced,
                top_1_correct=False,
                top_3_correct=False,
                target_passed=False,
                regression_clean=True,
                blast_radius_clean=True,
                admitted_for_pr=False,
                patch_attempts=0,
                total_tokens=0,  # no model in the loop; see tokens_measured
                runtime_sec=round(time.monotonic() - started, 2),
                patch_size_lines=0,
                rejection_reason="Blocked at RED Gate: Could not reproduce bug.",
            )
        )

    # 4. Regression trap
    started = time.monotonic()
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("core.py", "def process(x):\n    if x == 'new': return 'ok_new'\n    return 'legacy_default'\n")
        sb.write_file("test_suite.py", "from core import process\ndef test_legacy(): assert process('old') == 'legacy_default'\n")
        sb.exec("git add -A && git commit -m 'initial'")

        repro_agent = ReproductionAgent(sb)
        repro_res = repro_agent.run_reproduction_gate(
            "Issue with x == special", "fails on special",
            "from core import process\ndef test_repro(): assert process('special') == 'special_val'\n"
        )
        bad_patch = (
            "--- a/core.py\n+++ b/core.py\n@@ -2,2 +2,2 @@\n"
            "-    if x == 'new': return 'ok_new'\n-    return 'legacy_default'\n"
            "+    if x == 'special': return 'special_val'\n+    return None\n"
        )
        patch_agent = PatchAgent(sb, max_attempts=2)
        patch_res = patch_agent.run_patch_loop(["core.py"], lambda att, fb: bad_patch)
        regr_agent = RegressionAgent(sb)
        regr_res = regr_agent.run_regression_suite(["core.py"], test_suite_cmd=f"{sb.python_cmd} -m pytest test_suite.py")
        decision = AdmissionController.evaluate(patch_res, regr_res)

        records.append(
            RunRecord(
                instance_id="SMOKE-004 (regression_breaker)",
                reproduced=repro_res.reproduced,
                top_1_correct=True,
                top_3_correct=True,
                target_passed=patch_res.reached_green,
                regression_clean=regr_res.all_tests_passed,
                blast_radius_clean=regr_res.blast_radius.is_acceptable,
                admitted_for_pr=decision.approved,
                patch_attempts=patch_res.total_attempts,
                total_tokens=0,  # no model in the loop; see tokens_measured
                runtime_sec=round(time.monotonic() - started, 2),
                patch_size_lines=patch_res.total_lines_changed,
                rejection_reason=decision.rejection_summary,
            )
        )

    # 5. Blast-radius leak
    started = time.monotonic()
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("auth.py", "def get_user(): return 'admin'\n")
        sb.write_file("vault.py", "SECRET = 'unmodified'\n")
        sb.write_file("test_auth_legacy.py", "from auth import get_user\ndef test_basic(): assert get_user() != ''\n")
        sb.exec("git add -A && git commit -m 'initial'")

        repro_agent = ReproductionAgent(sb)
        repro_res = repro_agent.run_reproduction_gate(
            "auth bug", "get_user",
            "from auth import get_user\ndef test_repro(): assert get_user() == 'authenticated'\n"
        )
        leak_diff = (
            "--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-def get_user(): return 'admin'\n+def get_user(): return 'authenticated'\n"
            "--- a/vault.py\n+++ b/vault.py\n@@ -1 +1 @@\n-SECRET = 'unmodified'\n+SECRET = 'leaked'\n"
        )
        patch_agent = PatchAgent(sb, max_attempts=1)
        patch_res = patch_agent.run_patch_loop(["auth.py"], lambda att, fb: leak_diff)
        regr_agent = RegressionAgent(sb)
        regr_res = regr_agent.run_regression_suite(["auth.py"], test_suite_cmd=f"{sb.python_cmd} -m pytest test_auth_legacy.py")
        decision = AdmissionController.evaluate(patch_res, regr_res)

        records.append(
            RunRecord(
                instance_id="SMOKE-005 (blast_radius_leak)",
                reproduced=repro_res.reproduced,
                top_1_correct=True,
                top_3_correct=True,
                target_passed=patch_res.reached_green,
                regression_clean=regr_res.all_tests_passed,
                blast_radius_clean=regr_res.blast_radius.is_acceptable,
                admitted_for_pr=decision.approved,
                patch_attempts=patch_res.total_attempts,
                total_tokens=0,  # no model in the loop; see tokens_measured
                runtime_sec=round(time.monotonic() - started, 2),
                patch_size_lines=patch_res.total_lines_changed,
                rejection_reason=decision.rejection_summary,
            )
        )

    return records


def run_benchmark() -> MetricsReporter:
    records = run_smoke_scenarios()
    title = "Gate smoke scenarios (5 synthetic repos, scripted patches, measured sandbox runs)"

    reporter = MetricsReporter(records)
    print(reporter.format_markdown_table(title=title))
    return reporter


if __name__ == "__main__":
    run_benchmark()
