"""
Tests for JUnit parsing and the baseline comparison behind the regression gate.
"""
import pytest

from agents.regression_agent import RegressionAgent, junit_test_command
from harness.junit import ERROR, FAILED, PASSED, SKIPPED, JUnitReportError, TestCaseResult, TestReport, parse_junit_xml

PYTEST_STYLE = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_a" name="test_ok" />
  <testcase classname="tests.test_a" name="test_assert"><failure message="assert 1 == 2">def test_assert():
&gt;       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:4: AssertionError</failure></testcase>
  <testcase classname="tests.test_a" name="test_repo_exc"><failure message="ZeroDivisionError: division by zero">E       ZeroDivisionError: division by zero

mod.py:2: ZeroDivisionError</failure></testcase>
  <testcase classname="tests.test_a" name="test_fixture"><error message="failed on setup with &quot;RuntimeError: boom&quot;">E       RuntimeError: boom

tests/test_a.py:9: RuntimeError</error></testcase>
  <testcase classname="tests.test_a" name="test_skip"><skipped type="pytest.skip" message="x">skip</skipped></testcase>
</testsuite></testsuites>
"""


def test_parses_outcomes_exception_types_and_locations():
    report = parse_junit_xml(PYTEST_STYLE)
    by_id = report.by_id
    assert by_id["tests.test_a::test_ok"].outcome == PASSED
    assert by_id["tests.test_a::test_assert"].outcome == FAILED
    assert by_id["tests.test_a::test_assert"].exception_type == "AssertionError"
    assert by_id["tests.test_a::test_repo_exc"].exception_type == "ZeroDivisionError"
    assert by_id["tests.test_a::test_repo_exc"].failure_location == "mod.py"
    assert by_id["tests.test_a::test_fixture"].outcome == ERROR
    assert by_id["tests.test_a::test_skip"].outcome == SKIPPED
    assert report.total == 5


@pytest.mark.parametrize(
    "payload",
    [
        b"not xml at all",
        b"<html><body>no</body></html>",
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><testsuites/>',
    ],
    ids=["garbage", "wrong-root", "entity-declaration"],
)
def test_untrustworthy_reports_are_rejected(payload):
    with pytest.raises(JUnitReportError):
        parse_junit_xml(payload)


def _report(**outcomes):
    return TestReport([TestCaseResult(test_id=tid.replace("__", "::"), outcome=o) for tid, o in outcomes.items()])


def test_comparison_separates_new_preexisting_and_missing():
    baseline = _report(t__a=PASSED, t__b=FAILED, t__c=PASSED, t__d=SKIPPED)
    after = _report(t__a=FAILED, t__b=FAILED, t__d=ERROR, t__new=FAILED)
    newly, missing, preexisting = RegressionAgent.compare(baseline, after)
    assert sorted(newly) == ["t::a", "t::d", "t::new"]
    assert preexisting == ["t::b"]
    assert missing == ["t::c"]


def test_non_pytest_commands_cannot_produce_evidence():
    assert junit_test_command("tox -e py311", ".cerberus/r.xml") is None
    assert junit_test_command("python -m pytest -q", ".cerberus/r.xml").endswith("--junitxml=.cerberus/r.xml")


def test_suite_with_unreadable_results_is_not_a_pass(tmp_path):
    """A command that writes no JUnit report yields results_parsed=False."""
    from harness.docker_sandbox import Sandbox

    with Sandbox() as sb:
        agent = RegressionAgent(sb, timeout=60)
        run = agent.run_suite(f"{sb.python_cmd} -c \"print('pytest-looking but no report')\"", "none.xml")
        assert run.report is None
        baseline = _report(t__a=PASSED)
        regression = agent.run_regression_suite(f"{sb.python_cmd} -c \"print('pytest')\"", baseline)
        assert regression.results_parsed is False
        assert regression.regression_free is False
