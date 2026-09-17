"""
Tests for patch sources: the interface, the tree diff that captures external
agent edits without running git, and the external agent adapter.
"""
import os
import subprocess
import sys

import pytest

from agents.patch_sources import (
    CLAUDE_CODE_PRESET,
    DiffPatchSource,
    ExternalAgentPatchSource,
    PatchRequest,
    PatchSourceError,
    build_agent_prompt,
    generator_for,
    tree_diff,
)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _request(**overrides):
    base = dict(attempt=1, feedback="", issue_number=7, issue_title="rate crashes", issue_body="rate(1, 0) raises")
    base.update(overrides)
    return PatchRequest(**base)


def test_diff_patch_source_and_generator_adapter():
    source = DiffPatchSource(["first", "second"], name="human")
    gen = generator_for(source, _request())
    assert [gen(1, ""), gen(2, "fb"), gen(3, "fb")] == ["first", "second", "second"]


def test_tree_diff_applies_with_git_for_every_change_kind(tmp_path):
    base = tmp_path / "base"
    work = tmp_path / "work"
    for d in (base, work):
        (d / "pkg").mkdir(parents=True)
    (base / "pkg" / "mod.py").write_bytes(b"def f():\n    return 1\n")
    (work / "pkg" / "mod.py").write_bytes(b"def f():\n    return 2\n")
    (base / "old.txt").write_bytes(b"gone\n")
    (work / "new.py").write_bytes(b"X = 1\n")
    (base / "tail.py").write_bytes(b"a = 1\n")
    (work / "tail.py").write_bytes(b"a = 2")  # no trailing newline
    (work / ".git").mkdir()
    (work / ".git" / "noise").write_bytes(b"ignored")

    diff = tree_diff(str(base), str(work))
    assert ".git" not in diff

    target = tmp_path / "target"
    target.mkdir()
    (target / "pkg").mkdir()
    (target / "pkg" / "mod.py").write_bytes(b"def f():\n    return 1\n")
    (target / "old.txt").write_bytes(b"gone\n")
    (target / "tail.py").write_bytes(b"a = 1\n")
    _git(target, "init", "-q")
    _git(target, "config", "core.autocrlf", "false")  # as in every Cerberus workspace clone
    (target / "change.patch").write_bytes(diff.encode("utf-8"))
    _git(target, "apply", "change.patch")

    assert (target / "pkg" / "mod.py").read_bytes() == b"def f():\n    return 2\n"
    assert (target / "new.py").read_bytes() == b"X = 1\n"
    assert not (target / "old.txt").exists()
    assert (target / "tail.py").read_bytes() == b"a = 2"


def test_tree_diff_refuses_binary_changes(tmp_path):
    base = tmp_path / "base"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    (work / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(PatchSourceError, match="Binary change"):
        tree_diff(str(base), str(work))


FAKE_AGENT = """
import sys, pathlib
prompt = sys.stdin.read()
assert "rate crashes" in prompt, prompt
pathlib.Path("rates.py").write_text("def rate(a, b):\\n    return 0.0 if b == 0 else a / b\\n", encoding="utf-8")
pathlib.Path(".cerberus").mkdir(exist_ok=True)
pathlib.Path(".cerberus/notes.txt").write_text("harness dir is ignored", encoding="utf-8")
print("agent done")
"""


def test_external_agent_edits_become_a_verifiable_diff(tmp_path):
    repo, base_commit = _repo(tmp_path, {"rates.py": "def rate(a, b):\n    return a / b\n"})
    agent_script = tmp_path / "fake_agent.py"
    agent_script.write_text(FAKE_AGENT, encoding="utf-8")

    source = ExternalAgentPatchSource([sys.executable, str(agent_script)], name="fake-agent", timeout=60)
    diff = source.generate_patch(_request(source_repo_dir=str(repo), base_commit=base_commit, allowed_files=["rates.py"]))

    assert "--- a/rates.py" in diff and "+    return 0.0 if b == 0 else a / b" in diff
    assert ".cerberus" not in diff
    assert source.transcripts[0]["exit_code"] == 0
    # The source repository itself is untouched.
    assert (repo / "rates.py").read_text(encoding="utf-8") == "def rate(a, b):\n    return a / b\n"


def test_external_agent_requires_repository_context():
    source = ExternalAgentPatchSource(["agent"])
    with pytest.raises(PatchSourceError, match="source repository"):
        source.generate_patch(_request())


def test_agent_prompt_includes_scope_repro_and_feedback():
    prompt = build_agent_prompt(_request(
        attempt=2, feedback="GREEN failed: assert 1 == 0", allowed_files=["rates.py"],
        reproduction_test_code="def test_x():\n    assert rate(1, 0) == 0.0\n",
    ))
    assert "rates.py" in prompt
    assert "assert rate(1, 0) == 0.0" in prompt
    assert "GREEN failed" in prompt
    assert "Do not modify, delete or add tests" in prompt


def test_claude_code_preset_grants_no_shell_tool():
    tools = CLAUDE_CODE_PRESET[CLAUDE_CODE_PRESET.index("--allowedTools") + 1].split(",")
    assert "Bash" not in tools
    assert set(tools) <= {"Read", "Edit", "Write", "Glob", "Grep"}
