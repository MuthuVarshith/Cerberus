"""
Baseline Comparison Runner.
Evaluates three architectures on representative SWE-bench instances:
- Baseline A: One-Shot LLM Patch Generation (Issue -> Direct Patch -> Commit)
- Baseline B: Standard mini-swe-agent (Interactive Shell Tool Loop without Verification Gates)
- System C: Cerberus: Verification-First Autonomous Software Repair Harness (Full Verification Pipeline)
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from harness.docker_sandbox import Sandbox
from harness.admission_controller import AdmissionController
from agents.triage_agent import TriageAgent
from agents.reproduction_agent import ReproductionAgent
from agents.localization_agent import LocalizationAgent
from agents.patch_agent import PatchAgent
from agents.regression_agent import RegressionAgent
from evaluation.metrics_reporter import MetricsReporter, RunRecord


def run_baseline_comparison() -> str:
    print("=" * 65)
    print("🔬 Running Baseline Comparison: Baseline A vs. Baseline B vs. System C")
    print("=" * 65)

    # -------------------------------------------------------------
    # Simulated 5 benchmark instances:
    # 1. Standard bug: cleanly solvable
    # 2. Hard bug: needs self-healing retry
    # 3. Flaky/Bogus issue: passes immediately on master
    # 4. Dangerous bug: patch causes regression in legacy feature
    # 5. Overbroad patch: patch edits unauthorized files
    # -------------------------------------------------------------

    # Baseline A: One-Shot (Generates patch blindly, no gates, no regression check)
    records_baseline_a = [
        RunRecord("inst_1", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=850, runtime_sec=1.2, patch_size_lines=4),
        RunRecord("inst_2", reproduced=False, top_1_correct=False, top_3_correct=False, target_passed=False, regression_clean=False, blast_radius_clean=True, admitted_for_pr=False, patch_attempts=1, total_tokens=900, runtime_sec=1.1, patch_size_lines=12),
        RunRecord("inst_3", reproduced=False, top_1_correct=False, top_3_correct=False, target_passed=False, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=780, runtime_sec=1.0, patch_size_lines=8), # Hallucinated fix merged!
        RunRecord("inst_4", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=False, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=820, runtime_sec=1.3, patch_size_lines=6), # Regression merged!
        RunRecord("inst_5", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=False, admitted_for_pr=True, patch_attempts=1, total_tokens=890, runtime_sec=1.2, patch_size_lines=45), # Leak merged!
    ]

    # Baseline B: mini-swe-agent (Interactive loop with tests, but no RED gate & no Admission Controller)
    records_baseline_b = [
        RunRecord("inst_1", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=1800, runtime_sec=3.5, patch_size_lines=4),
        RunRecord("inst_2", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=2, total_tokens=3200, runtime_sec=5.8, patch_size_lines=5),
        RunRecord("inst_3", reproduced=False, top_1_correct=False, top_3_correct=False, target_passed=False, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=3, total_tokens=4100, runtime_sec=8.2, patch_size_lines=15), # Edits non-bug
        RunRecord("inst_4", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=False, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=2100, runtime_sec=4.1, patch_size_lines=6), # Broke regression!
        RunRecord("inst_5", reproduced=False, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=False, admitted_for_pr=True, patch_attempts=1, total_tokens=2200, runtime_sec=4.0, patch_size_lines=35), # Leaked!
    ]

    # System C: Cerberus: Verification-First Autonomous Software Repair Harness
    records_system_c = [
        RunRecord("inst_1", reproduced=True, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=1, total_tokens=2100, runtime_sec=3.8, patch_size_lines=4),
        RunRecord("inst_2", reproduced=True, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=True, admitted_for_pr=True, patch_attempts=2, total_tokens=3500, runtime_sec=6.2, patch_size_lines=5),
        RunRecord("inst_3", reproduced=False, top_1_correct=False, top_3_correct=False, target_passed=False, regression_clean=True, blast_radius_clean=True, admitted_for_pr=False, patch_attempts=0, total_tokens=850, runtime_sec=1.5, patch_size_lines=0, rejection_reason="Blocked at RED Gate"),
        RunRecord("inst_4", reproduced=True, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=False, blast_radius_clean=True, admitted_for_pr=False, patch_attempts=1, total_tokens=2200, runtime_sec=4.2, patch_size_lines=6, rejection_reason="Blocked by Regression Gate"),
        RunRecord("inst_5", reproduced=True, top_1_correct=True, top_3_correct=True, target_passed=True, regression_clean=True, blast_radius_clean=False, admitted_for_pr=False, patch_attempts=1, total_tokens=2300, runtime_sec=4.1, patch_size_lines=35, rejection_reason="Blocked by Blast-Radius Gate"),
    ]

    rep_a = MetricsReporter(records_baseline_a).compute_metrics()
    rep_b = MetricsReporter(records_baseline_b).compute_metrics()
    rep_c = MetricsReporter(records_system_c).compute_metrics()
    n = rep_c["total_instances"]

    # The localization booleans in these records were authored, not checked against
    # a ground-truth patch file, so this row cannot claim AST retrieval localizes
    # better than a baseline. `MetricsReporter` refuses to print them as
    # percentages; this table has to refuse for the same reason.
    def _loc(rep: Dict[str, Any]) -> str:
        if rep["localization_measured"]:
            return f"{rep['top_1_acc']}% / {rep['top_3_acc']}%"
        return "`not measured`"

    # Every cell below is derived from the records above. An earlier version wrote
    # the percentages out by hand and three of them disagreed with the records they
    # claimed to summarise; interpolation makes that class of error impossible.
    table = f"""### 🏆 Multi-Architecture Comparative Baseline Benchmark
> **Evaluation Status:** *Simulated Scenario Archetypes* (Illustrates comparative gatekeeper mechanics on controlled failure modes; not unconstrained live LLM generation). Every figure in this table is computed from the {n} scenario records in `evaluation/baseline_runner.py`, which are stipulated inputs rather than measured runs.

| Metric | Baseline A (One-Shot LLM) | Baseline B (mini-swe-agent) | System C (Cerberus Harness) |
| :--- | :---: | :---: | :---: |
| **Pipeline Type** | Unchecked 1-Shot | Free-form Loop | **Constrained Multi-Agent** |
| **Reproduction Gate (RED)** | {rep_a['reproduction_rate']}% (no gate) | {rep_b['reproduction_rate']}% (no gate) | **✅ {rep_c['reproduction_rate']}% Enforced** |
| **True Resolution Rate (Safe PRs)** | {rep_a['safe_resolution_rate']}% | {rep_b['safe_resolution_rate']}% | **{rep_c['safe_resolution_rate']}%** |
| **Unsafe PRs Merged (Regressions/Leaks)** | ⚠️ {rep_a['unsafe_pr_rate']}% ({rep_a['unsafe_admitted']}/{n}) | ⚠️ {rep_b['unsafe_pr_rate']}% ({rep_b['unsafe_admitted']}/{n}) | **🛡️ {rep_c['unsafe_pr_rate']}% ({rep_c['unsafe_admitted']}/{n})** |
| **Regression-Free Rate** | {rep_a['regression_free_rate']}% | {rep_b['regression_free_rate']}% | **{rep_c['regression_free_rate']}%** |
| **Top-1 / Top-3 Localization** | {_loc(rep_a)} | {_loc(rep_b)} | **{_loc(rep_c)}** |
| **Average Patch Size** | +{rep_a['avg_patch_size_lines']} lines | +{rep_b['avg_patch_size_lines']} lines | **+{rep_c['avg_patch_size_lines']} lines (Minimal Diffs)** |
| **Admission Rejections (Audit)** | {rep_a['rejections']} | {rep_b['rejections']} | **{rep_c['rejections']} (Active Gatekeeper)** |

#### 🔬 Key Finding:
- **Baselines A & B open PRs that break regression tests or modify unintended files** ({rep_a['unsafe_admitted']} and {rep_b['unsafe_admitted']} of {n} respectively).
- **Cerberus (Verification-First) acts as an authoritative firewall:** it reaches the same safe-resolution rate as Baseline B ({rep_c['safe_resolution_rate']}% vs {rep_b['safe_resolution_rate']}%) while admitting {rep_c['unsafe_admitted']} unsafe patches, by rejecting non-reproduced, regression-inducing, and scope-leaking ones.
"""
    return table


if __name__ == "__main__":
    out = run_baseline_comparison()
    print(out)
