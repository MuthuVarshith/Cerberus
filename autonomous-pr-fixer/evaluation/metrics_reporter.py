"""
Comprehensive Metrics Reporter for Autonomous Software Repair.
Tracks Primary, Secondary, and Safety metrics conforming to SWE-bench
and the Verification-First specification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass
class RunRecord:
    instance_id: str
    reproduced: bool
    top_1_correct: bool
    top_3_correct: bool
    target_passed: bool
    regression_clean: bool
    blast_radius_clean: bool
    admitted_for_pr: bool
    patch_attempts: int
    total_tokens: int
    runtime_sec: float
    patch_size_lines: int
    rejection_reason: Optional[str] = None
    #: Whether total_tokens came from a real model call. False means no model ran,
    #: so the reporter must not present an average as a measurement.
    tokens_measured: bool = False
    #: Whether top_1_correct/top_3_correct were checked against a known fault
    #: location rather than asserted by the scenario author.
    localization_measured: bool = False


class MetricsReporter:
    """Computes and formats primary, secondary, and safety research metrics."""

    def __init__(self, records: List[RunRecord]):
        self.records = records

    def compute_metrics(self) -> Dict[str, Any]:
        n = len(self.records)
        if n == 0:
            return {"total_instances": 0}

        # Primary Metrics
        repro_count = sum(1 for r in self.records if r.reproduced)
        admitted_count = sum(1 for r in self.records if r.admitted_for_pr)
        reg_clean_count = sum(1 for r in self.records if r.regression_clean)
        target_passed_count = sum(1 for r in self.records if r.target_passed)

        repro_rate = (repro_count / n) * 100.0
        resolution_rate = (admitted_count / n) * 100.0
        reg_free_rate = (reg_clean_count / n) * 100.0
        pr_admission_rate = (admitted_count / n) * 100.0

        # Secondary Metrics
        top_1_count = sum(1 for r in self.records if r.top_1_correct)
        top_3_count = sum(1 for r in self.records if r.top_3_correct)
        top_1_acc = (top_1_count / n) * 100.0
        top_3_acc = (top_3_count / n) * 100.0

        attempts_list = [r.patch_attempts for r in self.records if r.patch_attempts > 0]
        avg_attempts = sum(attempts_list) / len(attempts_list) if attempts_list else 0.0

        avg_tokens = sum(r.total_tokens for r in self.records) / n
        avg_runtime = sum(r.runtime_sec for r in self.records) / n

        patch_sizes = [r.patch_size_lines for r in self.records if r.patch_size_lines > 0]
        avg_patch_size = sum(patch_sizes) / len(patch_sizes) if patch_sizes else 0.0

        rejections = sum(1 for r in self.records if not r.admitted_for_pr)
        rejection_rate = (rejections / n) * 100.0

        # A PR is only "safe" if what it claims held on every axis. Splitting the
        # admitted set this way is the whole comparison against an ungated agent:
        # both may admit the same number of PRs, but only one admits broken ones.
        def _is_safe(r: RunRecord) -> bool:
            return r.target_passed and r.regression_clean and r.blast_radius_clean

        safe_admitted = sum(1 for r in self.records if r.admitted_for_pr and _is_safe(r))
        unsafe_admitted = sum(1 for r in self.records if r.admitted_for_pr and not _is_safe(r))

        # Safety Metrics
        reg_rejections = sum(
            1 for r in self.records if r.target_passed and not r.regression_clean
        )
        blast_rejections = sum(
            1 for r in self.records if r.target_passed and not r.blast_radius_clean
        )
        non_repro_rejections = sum(1 for r in self.records if not r.reproduced)

        return {
            "total_instances": n,
            "reproduction_rate": round(repro_rate, 1),
            "resolution_rate": round(resolution_rate, 1),
            "regression_free_rate": round(reg_free_rate, 1),
            "pr_admission_rate": round(pr_admission_rate, 1),
            "top_1_acc": round(top_1_acc, 1),
            "top_3_acc": round(top_3_acc, 1),
            "avg_attempts": round(avg_attempts, 2),
            "avg_tokens": int(avg_tokens),
            "avg_runtime_sec": round(avg_runtime, 2),
            "avg_patch_size_lines": round(avg_patch_size, 1),
            "rejection_rate": round(rejection_rate, 1),
            "rejections": rejections,
            "safe_resolution_rate": round((safe_admitted / n) * 100.0, 1),
            "unsafe_pr_rate": round((unsafe_admitted / n) * 100.0, 1),
            "safe_admitted": safe_admitted,
            "unsafe_admitted": unsafe_admitted,
            "tokens_measured": any(r.tokens_measured for r in self.records),
            "localization_measured": any(r.localization_measured for r in self.records),
            "reg_rejections": reg_rejections,
            "blast_rejections": blast_rejections,
            "non_repro_rejections": non_repro_rejections,
        }

    def format_markdown_table(self, title: str = "Empirical Evaluation Benchmark") -> str:
        m = self.compute_metrics()
        # Unmeasured quantities are labelled, not averaged into a number that
        # would be indistinguishable from the gate outcomes above them.
        unmeasured = "`not measured`"
        loc_1 = f"`{m['top_1_acc']}%`" if m["localization_measured"] else unmeasured
        loc_3 = f"`{m['top_3_acc']}%`" if m["localization_measured"] else unmeasured
        tokens = f"`{m['avg_tokens']}`" if m["tokens_measured"] else unmeasured
        return f"""### {title}
| Category | Metric | Measurement |
| :--- | :--- | :---: |
| **Primary** | **Admission Rate** | **`{m['pr_admission_rate']}%`** |
| **Primary** | **Reproduction Success Rate (RED Gate)** | **`{m['reproduction_rate']}%`** |
| **Primary** | **Regression-Free Rate** | **`{m['regression_free_rate']}%`** |
| **Secondary** | **Top-1 Localization Accuracy** | {loc_1} |
| **Secondary** | **Top-3 Localization Accuracy** | {loc_3} |
| **Secondary** | **Average Patch Attempts** | `{m['avg_attempts']}` |
| **Secondary** | **Average Tokens per Issue** | {tokens} |
| **Secondary** | **Average Wall-Clock Runtime** | `{m['avg_runtime_sec']}s` |
| **Secondary** | **Average Patch Size** | `+{m['avg_patch_size_lines']} lines` |
| **Safety** | **Regression-Induced Rejections** | **`{m['reg_rejections']}`** |
| **Safety** | **Blast-Radius Rejections** | **`{m['blast_rejections']}`** |
| **Safety** | **Non-Reproducible Issue Rejections** | **`{m['non_repro_rejections']}`** |
"""
