"""
End-to-end tests of the pipeline against the rate_calculator example fixture.

The fixture supplies its reproduction test and patch as inputs; every gate runs
for real in the (explicitly host-unsafe) test sandbox.
"""
import json
import subprocess

import pytest

import main
from agents.llm_patch_generator import ScriptedPatchGenerator
from examples.demo import prepare_rate_calculator_demo


def _single_artifact(artifacts_dir):
    runs = [p for p in artifacts_dir.iterdir() if p.is_dir()]
    assert len(runs) == 1, f"expected exactly one run artifact, found {runs}"
    with open(runs[0] / "run.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _run_demo(tmp_path, scenario=None, **overrides):
    scenario = scenario or prepare_rate_calculator_demo(str(tmp_path / "fixture"))
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


def _commit(repo_dir, message):
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, check=True, capture_output=True)


def _diff(old_body, new_body):
    return (
        "--- a/rate_calculator.py\n"
        "+++ b/rate_calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def calculate_rate(amount: float, total: float) -> float:\n"
        '     """Calculate the rate as amount / total."""\n'
        f"-    {old_body}\n"
        f"+    {new_body}\n"
    )


def test_demo_fixture_is_admitted_through_every_gate(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path)
    assert admitted is True

    data = _single_artifact(_isolated_run_artifacts)
    history = data["pipeline_state_history"]
    for state in ("REPRODUCED_RED", "BASELINE_RECORDED", "PATCH_GREEN", "REGRESSION_CLEAN", "SCOPE_ACCEPTABLE", "ADMITTED"):
        assert state in history
    assert data["final_state"] == "ADMITTED"
    assert data["refusal"] is None
    assert len(data["reproducibility"]["base_commit_sha"]) == 40
    assert data["sandbox"]["isolation"] == "host-unsafe"

    red = data["red_gate"]
    assert red["runs"] == 3
    assert list(red["failure_types"].values()) == ["ZeroDivisionError"]

    assert data["baseline"]["total"] == 2
    assert data["baseline"]["failing"] == []
    assert data["regression_results"]["newly_failing"] == []

    scope = data["blast_radius"]
    assert scope["is_acceptable"] is True
    assert scope["changed_files"] == ["rate_calculator.py"]
    assert scope["changed_symbols"] == ["rate_calculator.py::calculate_rate"]
    assert data["diff_lines_added"] == 2
    assert "if total == 0" in data["evidence"]["diff"]
    # Harness files never appear in the verified diff.
    assert ".cerberus" not in data["evidence"]["diff"]


def test_reproduction_that_passes_on_buggy_code_is_refused(tmp_path, _isolated_run_artifacts):
    passing_test = "from rate_calculator import calculate_rate\n\ndef test_ok():\n    assert calculate_rate(10, 2) == 5.0\n"
    admitted, _ = _run_demo(tmp_path, repro_test_code=passing_test)
    assert admitted is False

    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "REFUSED"
    assert data["refusal"]["code"] == "RED_NOT_FAILING"
    assert "PATCH_PENDING" not in data["pipeline_state_history"]
    assert data["pr_created"] is False


def test_unrelated_reproduction_is_refused_even_with_file_reference(tmp_path, _isolated_run_artifacts):
    """The removed static-analysis mode used to proceed here without evidence."""
    admitted, _ = _run_demo(
        tmp_path,
        repro_test_code="def test_nothing():\n    assert False\n",
        issue_body="calculate_rate(10, 0) raises at rate_calculator.py:3",
    )
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["refusal"]["code"] == "RED_UNRELATED_TEST"


def test_run_without_patch_source_is_refused(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path, patch_generator=None)
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "REFUSED"
    assert data["refusal"]["code"] == "NO_PATCH_SOURCE"


def test_run_without_reproduction_test_is_refused(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path, repro_test_code=None)
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["refusal"]["code"] == "NO_REPRODUCTION_TEST"


def test_patch_that_does_not_fix_the_bug_is_refused(tmp_path, _isolated_run_artifacts):
    useless = _diff("return amount / total", "return (amount / total)")
    admitted, _ = _run_demo(tmp_path, patch_generator=ScriptedPatchGenerator([useless]))
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["refusal"]["code"] == "GREEN_NOT_REACHED"


def test_patch_that_breaks_regression_is_refused(tmp_path, _isolated_run_artifacts):
    breaking = _diff("return amount / total", "return 0.0")
    admitted, _ = _run_demo(tmp_path, patch_generator=ScriptedPatchGenerator([breaking]))
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "REGRESSION"
    assert "REGRESSION_CLEAN" not in data["pipeline_state_history"]
    assert "tests.test_rate_calculator::test_simple_rate" in data["regression_results"]["newly_failing"]


def test_preexisting_failure_does_not_block_a_good_patch(tmp_path, _isolated_run_artifacts):
    scenario = prepare_rate_calculator_demo(str(tmp_path / "fixture"))
    with open(f"{scenario.repo_dir}/tests/test_known_broken.py", "w", encoding="utf-8") as f:
        f.write("def test_already_broken():\n    assert 1 == 2\n")
    _commit(scenario.repo_dir, "add a test that was already failing")

    admitted, _ = _run_demo(tmp_path, scenario=scenario)
    assert admitted is True
    data = _single_artifact(_isolated_run_artifacts)
    assert data["baseline"]["failing"] == ["tests.test_known_broken::test_already_broken"]
    assert data["regression_results"]["preexisting_failures"] == ["tests.test_known_broken::test_already_broken"]


def test_patch_creating_a_new_file_outside_scope_is_refused(tmp_path, _isolated_run_artifacts):
    scenario = prepare_rate_calculator_demo(str(tmp_path / "fixture"))
    sneaky = scenario.patch_diff + (
        "--- /dev/null\n"
        "+++ b/backdoor.py\n"
        "@@ -0,0 +1 @@\n"
        "+TOKEN = 'exfiltrate'\n"
    )
    admitted, _ = _run_demo(tmp_path, scenario=scenario, patch_generator=ScriptedPatchGenerator([sneaky]))
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "SCOPE_VIOLATION"
    assert "backdoor.py" in data["blast_radius"]["new_files"]
    assert "backdoor.py" in data["blast_radius"]["unauthorized_files"]


def test_patch_targeting_harness_files_is_refused(tmp_path, _isolated_run_artifacts):
    cheat = (
        "--- a/.cerberus/test_reproduce.py\n"
        "+++ b/.cerberus/test_reproduce.py\n"
        "@@ -1 +1 @@\n"
        "-from rate_calculator import calculate_rate\n"
        "+import pytest; pytest.skip('nope', allow_module_level=True)\n"
    )
    admitted, _ = _run_demo(tmp_path, patch_generator=ScriptedPatchGenerator([cheat]))
    assert admitted is False
    assert _single_artifact(_isolated_run_artifacts)["refusal"]["code"] == "GREEN_NOT_REACHED"


def test_non_git_directory_ends_in_error_state(tmp_path, _isolated_run_artifacts):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mod.py").write_text("x = 1\n", encoding="utf-8")
    admitted = main.run_pipeline(
        repo_dir=str(plain), issue_number=1, issue_title="t", issue_body="b",
        repro_test_code="def test_x():\n    assert False\n",
    )
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ERROR"
    assert data["refusal"]["stage"] == "SANDBOX"
    assert data["reproducibility"]["base_commit_sha"] == "unknown"


def test_github_mode_refuses_host_sandbox(tmp_path, _isolated_run_artifacts):
    admitted, _ = _run_demo(tmp_path, mode="github", dry_run=False)
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ERROR"
    assert "requires the Docker sandbox" in data["refusal"]["message"]


def test_docker_default_fails_closed_without_docker(tmp_path, monkeypatch, _isolated_run_artifacts):
    monkeypatch.setenv("CERBERUS_SANDBOX", "docker")
    monkeypatch.setattr("harness.docker_sandbox.shutil.which", lambda name: None)
    admitted, _ = _run_demo(tmp_path)
    assert admitted is False
    data = _single_artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ERROR"
    assert "Docker is required" in data["refusal"]["message"]


def test_cli_demo_exits_zero(capsys):
    assert main.main(["--demo", "--unsafe-local-sandbox"]) == 0
    assert "Final state: ADMITTED" in capsys.readouterr().out


@pytest.mark.parametrize("body", ["crash at ../../etc/passwd:1", "crash at /etc/passwd:1"])
def test_issue_paths_outside_the_workspace_are_ignored(tmp_path, body):
    (tmp_path / "inside.py").write_text("x = 1\n", encoding="utf-8")
    assert main._parse_referenced_file(body, str(tmp_path)) is None
    assert main._parse_referenced_file("crash at inside.py:1", str(tmp_path)) == "inside.py"
