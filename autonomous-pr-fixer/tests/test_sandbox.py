"""
Tests for Phase 1: Sandbox & Tooling.
Verifies container/environment lifecycle, command execution with timeouts,
file writes, reads, line-range editing, and grep searches.
"""
import os
import sys
import pytest

# Ensure autonomous-pr-fixer is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox


def test_sandbox_lifecycle_and_exec():
    with Sandbox(timeout_sec=5) as sb:
        assert sb.is_alive
        res = sb.exec("echo 'Phase 1 Sandbox Ready'")
        assert res.exit_code == 0
        assert "Phase 1 Sandbox Ready" in res.output
    # Sandbox must be destroyed cleanly
    assert not sb.is_alive


def test_sandbox_timeout_safety():
    with Sandbox(timeout_sec=2) as sb:
        # Run a sleep longer than timeout
        res = sb.exec(f"{sb.python_cmd} -c \"import time; time.sleep(10)\"", timeout=2)
        assert res.timed_out or res.exit_code in (124, 1)


def test_sandbox_path_traversal_protection():
    with Sandbox() as sb:
        with pytest.raises(ValueError) as exc:
            sb.write_file("../../outside.txt", "exploit")
        assert "Path traversal detected" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            sb.read_file("../../../etc/passwd")
        assert "Path traversal detected" in str(exc.value)


def test_sandbox_output_truncation():
    with Sandbox() as sb:
        # Generate output larger than 500KB
        res = sb.exec(f"{sb.python_cmd} -c \"print('A' * 600000)\"")
        assert len(res.stdout) <= 500500
        assert "truncated at 500KB" in res.stdout



# ---------------------------------------------------------------------------
# Docker isolation. Docker is not required to run these: the Docker CLI is
# replaced with a recorder, so they verify policy (fail closed, flags, phases,
# environment), not container behaviour.
# ---------------------------------------------------------------------------

import subprocess as _subprocess

from harness import docker_sandbox as ds


class _FakeDocker:
    def __init__(self, info_rc=0, image_rc=0, exec_rc=0):
        self.calls = []
        self.info_rc = info_rc
        self.image_rc = image_rc
        self.exec_rc = exec_rc

    def __call__(self, args, timeout=60):
        args = list(args)
        self.calls.append(args)
        rc, out = 0, ""
        if args[0] == "info":
            rc = self.info_rc
        elif args[:2] == ["image", "inspect"]:
            rc = self.image_rc
        elif args[0] == "run":
            out = "c0ffee0000000000"
        elif args[0] == "exec":
            rc = self.exec_rc
        return _subprocess.CompletedProcess(["docker", *args], rc, out, "")


@pytest.fixture
def docker_env(monkeypatch):
    monkeypatch.setenv("CERBERUS_SANDBOX", "docker")
    monkeypatch.setattr(ds.shutil, "which", lambda name: "/usr/bin/docker")
    fake = _FakeDocker()
    monkeypatch.setattr(ds.Sandbox, "_docker", lambda self, args, timeout=60: fake(args, timeout))
    return fake


def test_docker_is_the_default_isolation(monkeypatch):
    monkeypatch.delenv("CERBERUS_SANDBOX", raising=False)
    assert ds.resolve_isolation() == "docker"


def test_unknown_isolation_mode_is_rejected():
    with pytest.raises(ds.SandboxError):
        ds.resolve_isolation("chroot")


def test_missing_docker_fails_closed(monkeypatch):
    monkeypatch.setenv("CERBERUS_SANDBOX", "docker")
    monkeypatch.setattr(ds.shutil, "which", lambda name: None)
    with pytest.raises(ds.SandboxUnavailableError, match="Docker is required"):
        Sandbox()


@pytest.mark.parametrize("info_rc,image_rc,message", [(1, 0, "not reachable"), (0, 1, "is not available")])
def test_unusable_docker_fails_closed(monkeypatch, info_rc, image_rc, message):
    monkeypatch.setenv("CERBERUS_SANDBOX", "docker")
    monkeypatch.setattr(ds.shutil, "which", lambda name: "/usr/bin/docker")
    fake = _FakeDocker(info_rc=info_rc, image_rc=image_rc)
    monkeypatch.setattr(ds.Sandbox, "_docker", lambda self, args, timeout=60: fake(args, timeout))
    with pytest.raises(ds.SandboxUnavailableError, match=message):
        Sandbox()


def test_repair_container_is_isolated_and_carries_no_host_secrets(docker_env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "z" * 36)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    with Sandbox(timeout_sec=7) as sb:
        sb.exec("python -m pytest -q")
    run = next(c for c in docker_env.calls if c[0] == "run")
    joined = " ".join(run)
    for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges:true",
                 "--memory 2g", "--pids-limit 256", "--cpus 2"):
        assert flag in joined
    assert "ghp_" not in joined and "sk-ant" not in joined
    env_values = [run[i + 1] for i, a in enumerate(run) if a == "-e"]
    assert all(v.split("=")[0] in {"HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "GIT_TERMINAL_PROMPT"} for v in env_values)

    exec_call = next(c for c in docker_env.calls if c[0] == "exec")
    assert exec_call[exec_call.index("timeout"):exec_call.index("timeout") + 4] == ["timeout", "-s", "KILL", "7"]
    assert any(c[:2] == ["rm", "-f"] for c in docker_env.calls)


def test_setup_runs_networked_then_repair_runs_offline(docker_env):
    with Sandbox() as sb:
        results = sb.run_setup(["python -m pip install -e ."])
        assert [r.exit_code for r in results] == [0]
        sb.exec("python -m pytest -q")
    runs = [c for c in docker_env.calls if c[0] == "run"]
    assert runs[0][runs[0].index("--network") + 1] == "bridge"
    assert runs[1][runs[1].index("--network") + 1] == "none"
    commit = next(c for c in docker_env.calls if c[0] == "commit")
    # The offline repair container starts from the committed setup image.
    assert commit[2] in runs[1]
    assert any(c[:2] == ["rmi", "-f"] and c[2] == commit[2] for c in docker_env.calls)


def test_setup_after_repair_started_is_refused(docker_env):
    with Sandbox() as sb:
        sb.exec("true")
        with pytest.raises(ds.SandboxError, match="before any repair-phase command"):
            sb.run_setup(["pip install x"])


def test_host_unsafe_environment_drops_credentials():
    env = ds.host_unsafe_environment({
        "PATH": "/bin", "HOME": "/home/u", "GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "k",
        "AWS_SECRET_ACCESS_KEY": "s", "SSH_AUTH_SOCK": "/tmp/agent",
    })
    assert env["PATH"] == "/bin" and env["HOME"] == "/home/u"
    for leaked in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"):
        assert leaked not in env


def test_host_unsafe_exec_does_not_see_host_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "y" * 36)
    with Sandbox() as sb:
        res = sb.exec(f"{sb.python_cmd} -c \"import os; print(os.environ.get('GITHUB_TOKEN', 'absent'))\"")
    assert res.stdout.strip() == "absent"


def test_host_unsafe_skips_dependency_installation():
    with Sandbox() as sb:
        assert sb.run_setup(["pip install requests"]) == []
        assert sb.setup_skipped is True


def test_workspace_is_a_clone_of_committed_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
        _subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 2  # uncommitted\n", encoding="utf-8")

    with Sandbox(base_dir=str(repo)) as sb:
        assert sb.source_dirty is True
        assert len(sb.base_commit) == 40
        assert sb.read_file("a.py").strip() == "x = 1"
        with open(os.path.join(sb.workspace_dir, ".git", "info", "exclude"), encoding="utf-8") as f:
            assert "/.cerberus/" in f.read()


def test_non_git_source_is_refused(tmp_path):
    with pytest.raises(ds.WorkspaceError, match="not a git repository"):
        Sandbox(base_dir=str(tmp_path))
