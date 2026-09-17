"""
Tests for the admission controller: a strict conjunction of GREEN, baseline-aware
regression, scope, and a non-empty change.
"""
import pytest

from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport
from harness.admission_controller import AdmissionController
from harness.scope_gate import ScopeReport

REAL_DIFF = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    "-def calculate_rate(a, b): return a / b\n"
    "+def calculate_rate(a, b): return a / b if b != 0 else 0.0\n"
)


def _patch(green=True):
    return PatchLoopResult(reached_green=green, total_attempts=1, winning_diff=REAL_DIFF if green else "", history=[])


def _regression(**overrides):
    base = dict(results_parsed=True, total_tests=5, passed_count=5, baseline_total=5)
    base.update(overrides)
    return RegressionReport(**base)


def _scope(**overrides):
    base = dict(
        allowed_files=["calc.py"], changed_files=["calc.py"], lines_added=1, lines_deleted=1,
        is_acceptable=True, diff_text=REAL_DIFF,
    )
    base.update(overrides)
    return ScopeReport(**base)


def test_admission_approves_when_all_gates_pass():
    decision = AdmissionController.evaluate(_patch(), _regression(), _scope())
    assert decision.approved is True
    assert decision.gate_1_target_passed and decision.gate_2_regression_passed
    assert decision.gate_3_scope_passed and decision.gate_4_patch_changed
    assert decision.rejection_state == ""
    assert "APPROVED" in decision.summary_markdown()


def test_preexisting_failures_do_not_block_admission():
    regr = _regression(passed_count=4, failed_count=1, preexisting_failures=["tests.test_x::test_old"])
    decision = AdmissionController.evaluate(_patch(), regr, _scope())
    assert decision.approved is True
    assert "failing at baseline too" in decision.reasons[1]


def test_newly_failing_test_is_a_regression():
    regr = _regression(passed_count=4, failed_count=1, newly_failing=["tests.test_x::test_new"])
    decision = AdmissionController.evaluate(_patch(), regr, _scope())
    assert decision.approved is False
    assert decision.rejection_state == "REGRESSION"
    assert "tests.test_x::test_new" in decision.rejection_summary


def test_test_that_stops_running_is_a_regression():
    regr = _regression(missing_tests=["tests.test_x::test_gone"])
    decision = AdmissionController.evaluate(_patch(), regr, _scope())
    assert decision.approved is False
    assert "no longer run" in decision.rejection_summary


def test_unparseable_results_are_never_a_pass():
    regr = RegressionReport(results_parsed=False, error_message="JUnit report was not written")
    decision = AdmissionController.evaluate(_patch(), regr, _scope())
    assert decision.approved is False
    assert decision.gate_2_regression_passed is False
    assert decision.rejection_state == "REGRESSION_UNVERIFIABLE"


def test_admission_rejects_scope_violation():
    scope = _scope(changed_files=["calc.py", "vault.py"], unauthorized_files=["vault.py"],
                   is_acceptable=False, violation_reason="Files changed outside the allowed scope: vault.py")
    decision = AdmissionController.evaluate(_patch(), _regression(), scope)
    assert decision.approved is False
    assert decision.rejection_state == "SCOPE_VIOLATION"


def test_admission_rejects_when_green_not_reached():
    decision = AdmissionController.evaluate(_patch(green=False), _regression(), _scope())
    assert decision.approved is False
    assert decision.rejection_state == "GREEN_NOT_REACHED"


def test_all_failed_gates_are_reported():
    scope = _scope(changed_files=[], is_acceptable=False, violation_reason="x", diff_text="")
    regr = _regression(newly_failing=["t::a"])
    decision = AdmissionController.evaluate(_patch(green=False), regr, scope)
    for gate in ("GATE 1: FAIL", "GATE 2: FAIL", "GATE 3: FAIL", "GATE 4: FAIL"):
        assert gate in decision.rejection_summary
    # The first failing gate names the refusal.
    assert decision.rejection_state == "GREEN_NOT_REACHED"


@pytest.mark.parametrize(
    "diff_text,changed",
    [
        ("", []),
        ("--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-   \n+   \n", ["calc.py"]),
    ],
    ids=["empty", "whitespace-only"],
)
def test_admission_rejects_empty_or_whitespace_change(diff_text, changed):
    decision = AdmissionController.evaluate(_patch(), _regression(), _scope(changed_files=changed, diff_text=diff_text))
    assert decision.approved is False
    assert decision.gate_4_patch_changed is False
    assert decision.rejection_state == "EMPTY_PATCH"


def test_new_file_counts_as_a_change():
    new_file_diff = "diff --git a/helper.py b/helper.py\nnew file mode 100644\n--- /dev/null\n+++ b/helper.py\n@@ -0,0 +1 @@\n+X = 1\n"
    scope = _scope(changed_files=["helper.py"], new_files=["helper.py"], allowed_files=["helper.py"], diff_text=new_file_diff)
    decision = AdmissionController.evaluate(_patch(), _regression(), scope)
    assert decision.gate_4_patch_changed is True
