"""
End-to-end tests of the pipeline against the rate_calculator example fixture.

The fixture supplies its reproduction test and patch as inputs; every gate runs
for real in the sandbox.
"""
import json
import os

import main
from agents.llm_patch_generator import ScriptedPatchGenerator
from examples.demo import prepare_rate_calculator_demo


def _single_artifact(artifacts_dir):
    runs = [p for p in artifacts_dir.iterdir() if p.is_dir()]
    assert len(runs) == 1, f"expected exactly one run artifact, found {runs}"
    with open(runs[0] / "run.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _run_demo(tmp_path, **overrides):
    scenario = prepare_rate_calculator_demo(str(tmp_path / "fixture"))
    kwargs = dict(
        repo_dir=scenario.repo_dir,
        issue_number=scenario.issue_number,
        issue_title=scenario.issue_title,
        issue_body=scenario.issue_body,
        dry_run=True,
        mode="local",
        repro_test_code=scenario.repro_test_code,
        patch_generator=ScriptedPatchGenerator([scenario.patch_diff]),
    )
    kwargs.update(overrides)
    return main.run_pipeline(**kwargs), scenario


def test_demo_fixture_is_admitted_through_every_gate(tmp_path, _isolated_run_artifacts):
    admitted, scenario = _run_demo(tmp_path)
    assert admitted is True

    data = _single_artifact(_isolated_run_artifacts)
    history = data["pipeline_state_history"]
    for state in ("REPRODUCED_RED", "PATCH_GREEN", "REGRESSION_CLEAN", "BLAST_RADIUS_ACCEPTABLE"):
        assert state in history
    assert data["final_state"] == "ADMITTED"
    assert data["blast_radius"]["is_acceptable"] is True
    assert data["diff_files"] == ["rate_calculator.py"]
    assert data["diff_lines_added"] == 2
    assert data["regression_results"]["failed"] == 0
    # The recorded base is the fixture's real commit, not a placeholder.
    assert len(data["reproducibility"]["base_commit_sha"]) == 40
    assert "if total == 0" in data["evidence"]["diff"]
    assert "calculate_rate(10, 0)" in data["evidence"]["reproduction_test_code"]


def test_reproduction_that_passes_on_buggy_code_is_refused(tmp_path, _isolated_run_artifacts):
    """A test that does not fail proves nothing; the run must stop at RED."""
    passing_test = "from rate_calculator import calculate_rate\n\ndef test_ok():\n    assert calculate_rate(10, 2) == 5.0\n"
    admitted, _ = _run_demo(tmp_path, repro_test_code=passing_test)
    assert admitted is False

    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "REJECTED_NON_REPRODUCIBLE"
    assert "PATCH_PENDING" not in data["pipeline_state_history"]
    assert data["pr_created"] is False


def test_failed_reproduction_with_file_reference_is_still_refused(tmp_path, _isolated_run_artifacts):
    """The removed static-analysis mode used to proceed here without evidence."""
    passing_test = "def test_nothing():\n    assert True\n"
    admitted, _ = _run_demo(
        tmp_path,
        repro_test_code=passing_test,
        issue_body="calculate_rate(10, 0) raises at rate_calculator.py:3",
    )
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["final_state"] == "REJECTED_NON_REPRODUCIBLE"


def test_run_without_patch_source_is_refused(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path, patch_generator=None)
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "REJECTED_PATCH_FAILED"
    assert "No patch source" in data["admission_rejection_reason"]


def test_run_without_reproduction_test_is_refused(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path, repro_test_code=None)
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["final_state"] == "REJECTED_NON_REPRODUCIBLE"


def test_patch_that_does_not_fix_the_bug_is_refused(tmp_path, _isolated_run_artifacts):
    useless = (
        "--- a/rate_calculator.py\n"
        "+++ b/rate_calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def calculate_rate(amount: float, total: float) -> float:\n"
        '-    """Calculate the rate as amount / total."""\n'
        '+    """Calculate the rate."""\n'
        "     return amount / total\n"
    )
    admitted, _ = _run_demo(tmp_path, patch_generator=ScriptedPatchGenerator([useless]))
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["final_state"] == "REJECTED_PATCH_FAILED"


def test_patch_that_breaks_regression_is_refused(tmp_path, _isolated_run_artifacts):
    breaking = (
        "--- a/rate_calculator.py\n"
        "+++ b/rate_calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def calculate_rate(amount: float, total: float) -> float:\n"
        '     """Calculate the rate as amount / total."""\n'
        "-    return amount / total\n"
        "+    return 0.0\n"
    )
    admitted, _ = _run_demo(tmp_path, patch_generator=ScriptedPatchGenerator([breaking]))
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "REJECTED_REGRESSION"
    assert "REGRESSION_CLEAN" not in data["pipeline_state_history"]


def test_non_git_directory_ends_in_error_state(tmp_path, _isolated_run_artifacts):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mod.py").write_text("x = 1\n", encoding="utf-8")
    admitted = main.run_pipeline(
        repo_dir=str(plain),
        issue_number=1,
        issue_title="t",
        issue_body="b",
        repro_test_code="def test_x():\n    assert False\n",
    )
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ERROR"
    assert data["reproducibility"]["base_commit_sha"] == "unknown"


def test_cli_demo_exits_zero(capsys):
    assert main.main(["--demo"]) == 0
    assert "Status: ADMITTED" in capsys.readouterr().out
