"""
Regression agent: baseline-aware regression gate.

Process:
  1. Run the repository's test command on the unpatched base with a JUnit report.
  2. Apply the candidate patch (elsewhere) and run the same command again.
  3. A regression is a test that passed (or did not exist) at baseline and now
     fails or errors, or a test that passed at baseline and no longer runs.
  4. Failures that already existed at baseline are reported, not counted.
  5. Suspected regressions are re-checked on the base code (the patch is stashed
     and restored). A test that also fails there is reported as flaky, not as a
     regression.

Results come only from JUnit XML. A test command whose results cannot be read
as JUnit XML produces no evidence, and no evidence is never a pass.
"""
from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from harness.docker_sandbox import HARNESS_DIR, ExecResult, Sandbox
from harness.junit import ERROR, FAILED, PASSED, JUnitReportError, TestReport, read_junit_report

_FAILING = (FAILED, ERROR)


JUNIT_PLACEHOLDER = "{junit_xml}"


def junit_test_command(test_command: str, report_rel_path: str, configured_report: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """Return (command, report path) for a run that yields JUnit XML, or None if it cannot.

    - A command containing {junit_xml} gets the harness report path substituted.
    - A configured report path is read after running the command unchanged.
    - A pytest command gets --junitxml appended.
    """
    if JUNIT_PLACEHOLDER in test_command:
        return test_command.replace(JUNIT_PLACEHOLDER, report_rel_path), report_rel_path
    if configured_report:
        return test_command, configured_report
    if "pytest" in test_command:
        return f"{test_command} -p no:cacheprovider --junitxml={report_rel_path}", report_rel_path
    return None


def _without_excluded(report: TestReport, patterns: Sequence[str]) -> TestReport:
    if not patterns:
        return report
    return TestReport([c for c in report.cases if not any(fnmatch.fnmatchcase(c.test_id, p) for p in patterns)])


@dataclass
class TestRun:
    report: Optional[TestReport]
    exec_result: ExecResult
    error: str = ""


@dataclass
class RegressionReport:
    results_parsed: bool
    total_tests: int = 0
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    newly_failing: List[str] = field(default_factory=list)
    missing_tests: List[str] = field(default_factory=list)
    preexisting_failures: List[str] = field(default_factory=list)
    flaky_tests: List[str] = field(default_factory=list)
    baseline_total: int = 0
    execution_time_sec: float = 0.0
    raw_output: str = ""
    error_message: str = ""

    @property
    def regression_free(self) -> bool:
        return self.results_parsed and not self.newly_failing and not self.missing_tests

    @property
    def all_tests_passed(self) -> bool:
        """Kept for callers that read the old name; means 'no regressions', not 'no failures'."""
        return self.regression_free

    def to_dict(self) -> dict:
        return {
            "results_parsed": self.results_parsed,
            "total": self.total_tests,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "baseline_total": self.baseline_total,
            "newly_failing": self.newly_failing,
            "missing_tests": self.missing_tests,
            "preexisting_failures": self.preexisting_failures,
            "flaky_tests": self.flaky_tests,
            "execution_time_sec": self.execution_time_sec,
            "error_message": self.error_message,
        }


class RegressionAgent:
    """Runs the repository test suite before and after a patch and compares results."""

    def __init__(
        self,
        sandbox: Sandbox,
        timeout: int = 900,
        exclude: Sequence[str] = (),
        configured_report: Optional[str] = None,
    ):
        self.sandbox = sandbox
        self.timeout = timeout
        self.exclude = list(exclude)
        self.configured_report = configured_report

    def run_suite(self, test_command: str, report_name: str) -> TestRun:
        self.sandbox.ensure_harness_dir()
        planned = junit_test_command(test_command, f"{HARNESS_DIR}/{report_name}", self.configured_report)
        if planned is None:
            empty = ExecResult(exit_code=-1, stdout="", stderr="", duration_sec=0.0)
            return TestRun(
                None, empty,
                f"cannot collect JUnit results from test command {test_command!r}; use a pytest command, "
                f"a {JUNIT_PLACEHOLDER} placeholder, or test.report in .cerberus.yml",
            )
        cmd, report_rel = planned
        report_path = self.sandbox._safe_resolve(report_rel)
        if os.path.exists(report_path):
            os.remove(report_path)
        res = self.sandbox.exec(cmd, timeout=self.timeout)
        if res.timed_out:
            return TestRun(None, res, f"test suite timed out after {self.timeout}s")
        try:
            report = _without_excluded(read_junit_report(report_path), self.exclude)
        except JUnitReportError as exc:
            return TestRun(None, res, str(exc))
        if report.total == 0:
            return TestRun(None, res, "the test command collected no tests")
        return TestRun(report, res)

    def record_baseline(self, test_command: str) -> TestRun:
        return self.run_suite(test_command, "baseline.xml")

    @staticmethod
    def compare(baseline: TestReport, after: TestReport) -> Tuple[List[str], List[str], List[str]]:
        """Return (newly_failing, missing_tests, preexisting_failures)."""
        base = baseline.by_id
        newly_failing, preexisting = [], []
        for case in after.cases:
            if case.outcome not in _FAILING:
                continue
            before = base.get(case.test_id)
            if before is not None and before.outcome in _FAILING:
                preexisting.append(case.test_id)
            else:
                newly_failing.append(case.test_id)
        after_ids = set(after.by_id)
        missing = [c.test_id for c in baseline.cases if c.outcome == PASSED and c.test_id not in after_ids]
        return newly_failing, missing, preexisting

    def _recheck_on_base(self, test_command: str) -> Optional[TestReport]:
        """Run the suite with the patch stashed, then restore it. None if that is not possible."""
        stash = self.sandbox.exec("git stash push --include-untracked -q -m cerberus-recheck")
        if stash.exit_code != 0:
            return None
        try:
            run = self.run_suite(test_command, "baseline-recheck.xml")
        finally:
            pop = self.sandbox.exec("git stash pop -q")
            if pop.exit_code != 0:
                raise RuntimeError(f"Could not restore the candidate patch after the baseline re-check: {pop.output}")
        return run.report

    def run_regression_suite(self, test_command: str, baseline: TestReport) -> RegressionReport:
        start = time.time()
        run = self.run_suite(test_command, "regression.xml")
        if run.report is None:
            return RegressionReport(
                results_parsed=False,
                execution_time_sec=round(time.time() - start, 2),
                raw_output=run.exec_result.output,
                error_message=run.error,
                baseline_total=baseline.total,
            )

        after = run.report
        newly_failing, missing, preexisting = self.compare(baseline, after)
        flaky: List[str] = []
        if newly_failing:
            recheck = self._recheck_on_base(test_command)
            if recheck is not None:
                recheck_by_id = recheck.by_id
                for test_id in list(newly_failing):
                    case = recheck_by_id.get(test_id)
                    if case is not None and case.outcome in _FAILING:
                        newly_failing.remove(test_id)
                        flaky.append(test_id)

        return RegressionReport(
            results_parsed=True,
            total_tests=after.total,
            passed_count=after.count(PASSED),
            failed_count=after.count(FAILED),
            skipped_count=after.count("skipped"),
            error_count=after.count(ERROR),
            newly_failing=newly_failing,
            missing_tests=missing,
            preexisting_failures=preexisting,
            flaky_tests=flaky,
            baseline_total=baseline.total,
            execution_time_sec=round(time.time() - start, 2),
            raw_output=run.exec_result.output,
        )
