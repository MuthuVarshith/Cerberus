"""
End-to-end tests for per-repository configuration, patch sources, and verifying
an existing change (base..head), all through the real pipeline.
"""
import json
import subprocess
import sys

import pytest

import main
from agents.patch_sources import DiffPatchSource, ExternalAgentPatchSource
from examples.demo import prepare_rate_calculator_demo
from harness.repo_config import RepoConfigError, parse_repo_config

FIX_BODY = (
    "def calculate_rate(amount: float, total: float) -> float:\n"
    '    """Calculate the rate as amount / total."""\n'
    "    if total == 0:\n"
    "        return 0.0\n"
    "    return amount / total\n"
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(repo, rel, content):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _artifact(artifacts_dir):
    runs = [p for p in artifacts_dir.iterdir() if p.is_dir()]
    assert len(runs) == 1
    return json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))


@pytest.fixture
def scenario(tmp_path):
    return prepare_rate_calculator_demo(str(tmp_path / "fixture"))


def _run(scenario, **overrides):
    kwargs = dict(
        repo_dir=scenario.repo_dir,
        issue_number=scenario.issue_number,
        issue_title=scenario.issue_title,
        issue_body=scenario.issue_body,
        repro_test_code=scenario.repro_test_code,
        patch_source=DiffPatchSource([scenario.patch_diff]),
    )
    kwargs.update(overrides)
    return main.run_pipeline(**kwargs)


# --------------------------------------------------------------- config file

def test_config_parsing_rejects_unknown_keys_and_bad_values():
    with pytest.raises(RepoConfigError, match="Unknown key"):
        parse_repo_config("test:\n  comand: pytest\n")
    with pytest.raises(RepoConfigError, match="between"):
        parse_repo_config("budgets:\n  red_runs: 0\n")
    with pytest.raises(RepoConfigError, match="relative path"):
        parse_repo_config("scope:\n  allowed_paths: ['../outside/*']\n")
    cfg = parse_repo_config("scope:\n  allowed_paths: ['src/*']\n  max_files: 5\nbudgets:\n  red_runs: 2\n")
    assert cfg.present and cfg.scope.allowed_paths == ["src/*"] and cfg.scope.max_files == 5 and cfg.budgets.red_runs == 2


def test_invalid_config_ends_in_error(scenario, _isolated_run_artifacts):
    from pathlib import Path

    _write(Path(scenario.repo_dir), ".cerberus.yml", "budgets:\n  red_runs: many\n")
    _commit_all(scenario.repo_dir, "bad config")
    assert _run(scenario) is False
    data = _artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ERROR"
    assert data["refusal"]["stage"] == "CONFIG"


def test_config_excludes_budgets_and_placeholder_command(scenario, _isolated_run_artifacts):
    from pathlib import Path

    repo = Path(scenario.repo_dir)
    _write(repo, "tests/test_network.py", "def test_needs_network():\n    raise ConnectionError('offline')\n")
    _write(repo, "run_tests.py", "import sys\nimport pytest\nsys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests', '--junitxml=' + sys.argv[1]]))\n")
    _write(repo, ".cerberus.yml", (
        "test:\n"
        "  command: python run_tests.py {junit_xml}\n"
        "  exclude:\n"
        "    - 'tests.test_network::*'\n"
        "budgets:\n"
        "  red_runs: 2\n"
        "  green_runs: 2\n"
    ))
    _commit_all(repo, "config with exclusions")

    assert _run(scenario) is True
    data = _artifact(_isolated_run_artifacts)
    assert data["repo_config"]["present"] is True
    assert data["baseline"]["test_command"] == "python run_tests.py {junit_xml}"
    assert data["baseline"]["total"] == 2  # the excluded network test is not counted
    assert data["red_gate"]["runs"] == 2


def test_patch_may_not_edit_the_verification_policy(scenario, _isolated_run_artifacts):
    from pathlib import Path

    _write(Path(scenario.repo_dir), ".cerberus.yml", "scope:\n  max_files: 3\n")
    _commit_all(scenario.repo_dir, "config")
    loosen = scenario.patch_diff + (
        "--- a/.cerberus.yml\n+++ b/.cerberus.yml\n@@ -1,2 +1,2 @@\n scope:\n-  max_files: 3\n+  max_files: 100\n"
    )
    assert _run(scenario, patch_source=DiffPatchSource([loosen])) is False
    data = _artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "GREEN_NOT_REACHED"
    # The evidence says why: the patch never applied, rather than "the test did not pass".
    assert "APPLY_ERROR" in data["refusal"]["message"] and ".cerberus.yml" in data["refusal"]["message"]
    assert data["patch_attempts"] == 1
    assert [h["status"] for h in data["patch_history"]] == ["APPLY_ERROR", "ABORT_DUPLICATE_DIFF"]
    assert data["patch_history"][0]["applied"] is False


# ------------------------------------------------- verifying an existing change

def _branch_with_fix(scenario, extra=None):
    repo = scenario.repo_dir
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "fix-branch")
    from pathlib import Path

    _write(Path(repo), "rate_calculator.py", FIX_BODY)
    for rel, content in (extra or {}).items():
        _write(Path(repo), rel, content)
    head = _commit_all(repo, "fix")
    _git(repo, "checkout", "-q", base)
    return base, head


def _cli_verify(scenario, base, head, repro_path):
    return main.main([
        "--repo", scenario.repo_dir, "--issue", "101",
        "--title", scenario.issue_title, "--body", scenario.issue_body,
        "--repro-test", str(repro_path), "--base", base, "--head", head,
        "--unsafe-local-sandbox",
    ])


def test_verify_existing_change_is_admitted(scenario, tmp_path, _isolated_run_artifacts):
    base, head = _branch_with_fix(scenario, extra={"tests/test_zero.py": "from rate_calculator import calculate_rate\n\ndef test_zero():\n    assert calculate_rate(1, 0) == 0.0\n"})
    repro = tmp_path / "repro.py"
    repro.write_text(scenario.repro_test_code, encoding="utf-8")

    assert _cli_verify(scenario, base, head, repro) == 0
    data = _artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ADMITTED"
    assert data["reproducibility"]["base_commit_sha"] == base
    assert data["patch_source"]["name"].startswith("change ")
    # A new test file is allowed; existing tests were not touched.
    assert data["blast_radius"]["new_files"] == ["tests/test_zero.py"]
    assert data["blast_radius"]["modified_test_files"] == []


def test_change_that_weakens_an_existing_test_is_refused(scenario, tmp_path, _isolated_run_artifacts):
    weakened = "from rate_calculator import calculate_rate\n\n\ndef test_simple_rate():\n    assert True\n\n\ndef test_fractional_rate():\n    assert True\n"
    base, head = _branch_with_fix(scenario, extra={"tests/test_rate_calculator.py": weakened})
    repro = tmp_path / "repro.py"
    repro.write_text(scenario.repro_test_code, encoding="utf-8")

    assert _cli_verify(scenario, base, head, repro) == 1
    data = _artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "SCOPE_VIOLATION"
    assert data["blast_radius"]["modified_test_files"] == ["tests/test_rate_calculator.py"]


def _existing_tests(scenario):
    from pathlib import Path

    return (Path(scenario.repo_dir) / "tests" / "test_rate_calculator.py").read_text(encoding="utf-8")


def test_change_that_adds_a_test_to_an_existing_test_file_is_admitted(scenario, tmp_path, _isolated_run_artifacts):
    """The shape of most real fixes: the source change plus a new test appended to an existing file."""
    extended = _existing_tests(scenario) + "\n\ndef test_zero_total():\n    assert calculate_rate(3, 0) == 0.0\n"
    base, head = _branch_with_fix(scenario, extra={"tests/test_rate_calculator.py": extended})
    repro = tmp_path / "repro.py"
    repro.write_text(scenario.repro_test_code, encoding="utf-8")

    assert _cli_verify(scenario, base, head, repro) == 0
    data = _artifact(_isolated_run_artifacts)
    assert data["final_state"] == "ADMITTED"
    assert data["blast_radius"]["modified_test_files"] == []
    assert data["blast_radius"]["extended_test_files"] == ["tests/test_rate_calculator.py"]
    assert data["regression_results"]["test_files_run_at_base"] == ["tests/test_rate_calculator.py"]
    # The patch's version was restored after the regression run: scope still sees the added test.
    assert "tests/test_rate_calculator.py::test_zero_total" in data["blast_radius"]["changed_symbols"]


def test_lines_added_to_an_existing_test_cannot_hide_a_regression(scenario, tmp_path, _isolated_run_artifacts):
    """An early return added to a test the patch breaks is additions-only; the base version still runs."""
    breaking_fix = FIX_BODY.replace("    return amount / total\n", "    return float(round(amount / total))\n")
    original = _existing_tests(scenario)
    assert "def test_fractional_rate():\n" in original
    hidden = original.replace("def test_fractional_rate():\n", "def test_fractional_rate():\n    return\n")
    base, head = _branch_with_fix(scenario, extra={"rate_calculator.py": breaking_fix, "tests/test_rate_calculator.py": hidden})
    repro = tmp_path / "repro.py"
    repro.write_text(scenario.repro_test_code, encoding="utf-8")

    assert _cli_verify(scenario, base, head, repro) == 1
    data = _artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "REGRESSION"
    assert data["regression_results"]["newly_failing"] == ["tests.test_rate_calculator::test_fractional_rate"]
    assert data["regression_results"]["test_files_run_at_base"] == ["tests/test_rate_calculator.py"]


def test_split_test_changes_only_treats_pure_additions_as_extensions():
    from harness.diff_utils import WorkspaceChanges
    from harness.scope_gate import split_test_changes

    changes = WorkspaceChanges(
        diff_text="",
        files=["tests/test_a.py", "tests/test_b.py", "tests/conftest.py", "tests/test_gone.py",
               "tests/test_new.py", "tests/data.bin", "src/mod.py"],
        new_files=["tests/test_new.py"],
        deleted_files=["tests/test_gone.py"],
        per_file_numstat={
            "tests/test_a.py": (5, 0),        # additions only
            "tests/test_b.py": (5, 1),        # one existing line changed
            "tests/conftest.py": (3, 0),      # additions, but conftest changes every test beside it
            "tests/test_gone.py": (0, 12),
            "tests/test_new.py": (8, 0),
            "tests/data.bin": (0, 0),         # binary
            "src/mod.py": (2, 2),
        },
    )
    modified, extended = split_test_changes(changes)
    assert extended == ["tests/test_a.py"]
    assert modified == ["tests/test_b.py", "tests/conftest.py", "tests/test_gone.py", "tests/data.bin"]


def test_configured_allowed_paths_restrict_an_existing_change(scenario, tmp_path, _isolated_run_artifacts):
    from pathlib import Path

    _write(Path(scenario.repo_dir), ".cerberus.yml", "scope:\n  allowed_paths: ['lib/*']\n")
    _commit_all(scenario.repo_dir, "restrict scope")
    base, head = _branch_with_fix(scenario)
    repro = tmp_path / "repro.py"
    repro.write_text(scenario.repro_test_code, encoding="utf-8")

    assert _cli_verify(scenario, base, head, repro) == 1
    data = _artifact(_isolated_run_artifacts)
    assert data["refusal"]["code"] == "SCOPE_VIOLATION"
    assert data["blast_radius"]["unauthorized_files"] == ["rate_calculator.py"]


def test_cli_rejects_multiple_patch_sources(scenario, capsys):
    rc = main.main(["--repo", scenario.repo_dir, "--title", "t", "--patch", "x.diff", "--agent", "claude-code"])
    assert rc == 2
    assert "Choose one patch source" in capsys.readouterr().out


# ------------------------------------------------------- external agent source

FAKE_AGENT = (
    "import sys, pathlib\n"
    "prompt = sys.stdin.read()\n"
    "assert 'must pass after your change' in prompt\n"
    "pathlib.Path('rate_calculator.py').write_text(" + repr(FIX_BODY) + ", encoding='utf-8')\n"
)


def test_external_agent_patch_is_verified_like_any_other(scenario, tmp_path, _isolated_run_artifacts):
    agent = tmp_path / "agent.py"
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    source = ExternalAgentPatchSource([sys.executable, str(agent)], name="fake-agent", timeout=60)

    assert _run(scenario, patch_source=source) is True
    data = _artifact(_isolated_run_artifacts)
    assert data["patch_source"]["name"] == "fake-agent"
    assert data["patch_source"]["transcripts"][0]["exit_code"] == 0
    assert "if total == 0" in data["evidence"]["diff"]
