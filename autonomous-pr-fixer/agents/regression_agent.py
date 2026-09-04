"""
Regression Agent and Structural Blast-Radius Analysis.
Executes the full existing repository test suite, parses granular test outcomes
(passed, failed, skipped, errors, duration), and compares expected repair scope
against actual AST and file modifications.
"""
from __future__ import annotations

import ast
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from harness.docker_sandbox import Sandbox


@dataclass
class StructuralBlastRadius:
    expected_files: List[str]
    observed_files: List[str]
    unauthorized_files: List[str]
    lines_added: int
    lines_deleted: int
    modified_symbols: List[str] = field(default_factory=list)
    is_acceptable: bool = True
    scope_violation_reason: Optional[str] = None


@dataclass
class RegressionReport:
    all_tests_passed: bool
    total_tests: int
    passed_count: int
    failed_count: int
    skipped_count: int
    error_count: int
    execution_time_sec: float
    raw_output: str
    blast_radius: StructuralBlastRadius


class RegressionAgent:
    """Runs repository regression tests and conducts structural blast-radius analysis."""

    def __init__(
        self,
        sandbox: Sandbox,
        max_files_threshold: int = 3,
        max_total_lines: int = 200,
    ):
        self.sandbox = sandbox
        self.max_files_threshold = max_files_threshold
        self.max_total_lines = max_total_lines

    def analyze_structural_blast_radius(
        self,
        expected_candidates: List[str],
    ) -> StructuralBlastRadius:
        """
        Compares expected repair scope against actual git diff.
        Extracts AST nodes for modified functions/classes.
        """
        # Files changed in git
        diff_names = self.sandbox.exec("git diff --name-only HEAD")
        changed_raw = diff_names.stdout.strip().splitlines() if diff_names.stdout.strip() else []
        changed_files = [
            f.replace("\\", "/")
            for f in changed_raw
            if not f.endswith("test_reproduce.py") and not f.startswith(".harness")
        ]

        expected_clean = {f.replace("\\", "/") for f in expected_candidates}
        unauthorized = [f for f in changed_files if f not in expected_clean]

        # Lines added / deleted
        numstat = self.sandbox.exec("git diff --numstat HEAD")
        added = 0
        deleted = 0
        for line in numstat.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and not parts[2].endswith("test_reproduce.py"):
                try:
                    added += int(parts[0])
                    deleted += int(parts[1])
                except ValueError:
                    pass

        # Identify modified AST functions/classes
        modified_symbols: List[str] = []
        for cfile in changed_files:
            full_path = os.path.join(self.sandbox.workspace_dir, cfile)
            if os.path.exists(full_path) and cfile.endswith(".py"):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as pf:
                        tree = ast.parse(pf.read(), filename=cfile)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                            modified_symbols.append(f"{cfile}:{node.name}")
                except Exception:
                    pass

        # Check safety policy
        is_acceptable = True
        violation_reason = None

        if len(unauthorized) > 0:
            is_acceptable = False
            violation_reason = f"Unauthorized files touched outside repair scope: {', '.join(unauthorized)}"
        elif len(changed_files) > self.max_files_threshold:
            is_acceptable = False
            violation_reason = f"Changed {len(changed_files)} files (threshold is {self.max_files_threshold})"
        elif (added + deleted) > self.max_total_lines:
            is_acceptable = False
            violation_reason = f"Total diff footprint ({added + deleted} lines) exceeds threshold ({self.max_total_lines})"

        return StructuralBlastRadius(
            expected_files=list(expected_clean),
            observed_files=changed_files,
            unauthorized_files=unauthorized,
            lines_added=added,
            lines_deleted=deleted,
            modified_symbols=modified_symbols[:15],
            is_acceptable=is_acceptable,
            scope_violation_reason=violation_reason,
        )

    def run_regression_suite(
        self,
        expected_candidates: List[str],
        test_suite_cmd: str = "py -3.13 -m pytest -k \"not reproduce\"",
    ) -> RegressionReport:
        blast_radius = self.analyze_structural_blast_radius(expected_candidates)

        start_t = time.time()
        exec_res = self.sandbox.exec(test_suite_cmd, timeout=60)
        duration = time.time() - start_t

        output = exec_res.output
        total, passed, failed, skipped, errors = self._parse_pytest_counts(output, exec_res.exit_code)

        all_passed = (exec_res.exit_code == 0) and (failed == 0 and errors == 0)

        return RegressionReport(
            all_tests_passed=all_passed,
            total_tests=total,
            passed_count=passed,
            failed_count=failed,
            skipped_count=skipped,
            error_count=errors,
            execution_time_sec=round(duration, 2),
            raw_output=output,
            blast_radius=blast_radius,
        )

    def _parse_pytest_counts(self, output: str, exit_code: int) -> tuple[int, int, int, int, int]:
        passed = 0
        failed = 0
        skipped = 0
        errors = 0

        p_match = re.search(r"(\d+)\s+passed", output)
        if p_match:
            passed = int(p_match.group(1))

        f_match = re.search(r"(\d+)\s+failed", output)
        if f_match:
            failed = int(f_match.group(1))

        s_match = re.search(r"(\d+)\s+skipped", output)
        if s_match:
            skipped = int(s_match.group(1))

        e_match = re.search(r"(\d+)\s+errors?", output)
        if e_match:
            errors = int(e_match.group(1))

        total = passed + failed + skipped + errors
        if total == 0:
            if exit_code == 0:
                total = 1
                passed = 1
            else:
                total = 1
                failed = 1

        return total, passed, failed, skipped, errors
