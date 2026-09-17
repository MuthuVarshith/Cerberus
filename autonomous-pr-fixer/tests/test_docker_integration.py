"""
Integration tests against a real Docker engine.

The rest of the suite mocks the Docker CLI. These tests check, inside real
containers, the isolation properties the sandbox claims: no network in the repair
phase, no host environment, no capabilities, resource limits, an in-container
kill timeout, a networked setup phase whose filesystem carries into the offline
container, and cleanup. They are skipped when Docker or the sandbox image is not
available; run `docker build -t cerberus-sandbox:py3.11 sandbox/` first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from harness.docker_sandbox import DEFAULT_IMAGE, Sandbox, SandboxError, SandboxUnavailableError


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        image = subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and image.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason=f"Docker engine or image {DEFAULT_IMAGE} not available")


def _git_repo(path) -> str:
    repo = str(path / "repo")
    os.makedirs(repo)
    with open(os.path.join(repo, "mod.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write("def value():\n    return 1\n")
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t.invalid"],
                 ["config", "core.autocrlf", "false"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _container_exists(name_fragment: str) -> bool:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name_fragment}", "--format", "{{.Names}}"],
                         capture_output=True, text=True, timeout=30)
    return bool(out.stdout.strip())


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    os.environ["CERBERUS_PROBE_SECRET_TOKEN"] = "host-secret-must-not-cross"
    try:
        sb = Sandbox(base_dir=_git_repo(tmp_path_factory.mktemp("docker")), isolation="docker", timeout_sec=60)
    finally:
        os.environ.pop("CERBERUS_PROBE_SECRET_TOKEN", None)
    yield sb
    sb.destroy()


def test_repair_container_has_no_network(sandbox):
    interfaces = sandbox.exec("ls /sys/class/net")
    assert interfaces.stdout.split() == ["lo"]
    connect = sandbox.exec(
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    )
    assert connect.exit_code != 0


def test_host_environment_does_not_cross_into_container(sandbox):
    env = sandbox.exec("env").stdout
    assert "host-secret-must-not-cross" not in env
    assert "CERBERUS_PROBE_SECRET_TOKEN" not in env
    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "USERPROFILE", "APPDATA"):
        assert f"{name}=" not in env


def test_no_capabilities_and_no_new_privileges(sandbox):
    status = sandbox.exec("cat /proc/self/status").stdout
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert fields["CapEff"].strip() == "0000000000000000"
    assert fields["CapBnd"].strip() == "0000000000000000"
    assert fields["NoNewPrivs"].strip() == "1"


def test_memory_and_pid_limits_are_applied(sandbox):
    memory = sandbox.exec("cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes")
    assert memory.stdout.strip() == str(2 * 1024 ** 3)
    pids = sandbox.exec("cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max")
    assert pids.stdout.strip() == "256"


def test_pid_limit_stops_process_explosion(sandbox):
    script = (
        "import subprocess\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(400):\n"
        "        procs.append(subprocess.Popen(['sleep', '30']))\n"
        "    print('spawned', len(procs))\n"
        "except OSError:\n"
        "    print('limited', len(procs))\n"
        "for p in procs:\n"
        "    p.kill()\n"
    )
    sandbox.write_file(".cerberus/spawn.py", script)
    res = sandbox.exec("python .cerberus/spawn.py", timeout=60)
    assert res.stdout.startswith("limited"), res.output
    assert int(res.stdout.split()[1]) < 256


def test_timeout_kills_the_process_inside_the_container(sandbox):
    res = sandbox.exec("sleep 45", timeout=3)
    assert res.timed_out and res.exit_code == 124
    assert res.duration_sec < 20
    leftover = sandbox.exec("cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' ' '").stdout
    assert "sleep 45" not in leftover


_SLEEP_STATES = (
    "for s in /proc/[0-9]*/status; do "
    "[ \"$(sed -n 's/^Name:\\t//p' $s 2>/dev/null)\" = sleep ] && sed -n 's/^State:\\t\\(.\\).*/\\1/p' $s; "
    "done; true"
)


def test_background_processes_do_not_outlive_their_command(sandbox):
    started = sandbox.exec("(sleep 300 &); (setsid sleep 301 &); nohup sleep 302 >/dev/null 2>&1 & echo started")
    assert started.exit_code == 0 and "started" in started.stdout
    # Killed after the command returned, and reaped by PID 1 rather than left as zombies.
    deadline = time.time() + 10
    states = sandbox.exec(_SLEEP_STATES).stdout.split()
    while states and time.time() < deadline:
        time.sleep(0.5)
        states = sandbox.exec(_SLEEP_STATES).stdout.split()
    assert states == []
    assert sandbox.exec("exit 7").exit_code == 7  # the wrapper preserves the command's status


def test_symlinks_planted_in_the_container_are_not_followed_by_the_host(sandbox):
    planted = sandbox.exec("ln -s /etc .cerberus/planted_dir && ln -s /etc/hostname .cerberus/planted_file")
    assert planted.exit_code == 0
    with pytest.raises((SandboxError, OSError)):
        sandbox.write_file(".cerberus/planted_dir/cerberus_escape.txt", "must not leave the workspace")
    with pytest.raises((SandboxError, OSError)):
        sandbox.write_file(".cerberus/planted_file", "must not leave the workspace")
    sandbox.remove_file(".cerberus/planted_dir")
    sandbox.remove_file(".cerberus/planted_file")
    assert sandbox.exec("ls /etc/cerberus_escape.txt").exit_code != 0
    assert "must not leave" not in sandbox.exec("cat /etc/hostname").stdout


def test_container_sees_the_workspace_and_not_the_host(sandbox):
    head = sandbox.exec("git rev-parse HEAD")
    assert head.exit_code == 0 and head.stdout.strip() == sandbox.base_commit
    assert sandbox.exec("git status --porcelain").stdout.strip() == ""
    sandbox.exec("echo written-in-container > from_container.txt")
    assert sandbox.read_file("from_container.txt").strip() == "written-in-container"
    host_paths = sandbox.exec("ls -d /mnt/c /mnt/host /host_mnt /Users /home/* 2>/dev/null")
    assert host_paths.stdout.strip() == ""


def test_setup_is_networked_and_its_filesystem_carries_into_the_offline_container(tmp_path):
    sb = Sandbox(base_dir=_git_repo(tmp_path), isolation="docker", timeout_sec=60)
    run_id = sb._run_id
    try:
        results = sb.run_setup([
            "ls /sys/class/net > /tmp/setup_net.txt && grep -qv '^lo$' /tmp/setup_net.txt",
            "echo 'MARK = 42' > /usr/local/lib/python3.11/site-packages/cerberus_setup_marker.py",
        ])
        assert [r.exit_code for r in results] == [0, 0], [r.output for r in results]
        assert sb.setup_image == f"cerberus-setup-{run_id}"
        marker = sb.exec("python -c \"import cerberus_setup_marker as m; print(m.MARK)\"")
        assert marker.stdout.strip() == "42"
        assert sb.exec("ls /sys/class/net").stdout.split() == ["lo"]
        with pytest.raises(Exception):
            sb.run_setup(["true"])  # setup after a repair command is refused
    finally:
        sb.destroy()
    assert not _container_exists(f"cerberus-{run_id}")
    image = subprocess.run(["docker", "image", "inspect", f"cerberus-setup-{run_id}"], capture_output=True, timeout=30)
    assert image.returncode != 0
    assert not os.path.exists(sb._root_dir)


def test_missing_image_fails_closed(tmp_path):
    with pytest.raises(SandboxUnavailableError):
        Sandbox(base_dir=_git_repo(tmp_path), isolation="docker", image="cerberus-sandbox:does-not-exist")


def _run_record(artifacts, run_id):
    with open(os.path.join(str(artifacts), run_id, "run.json"), encoding="utf-8") as f:
        return json.load(f)


def test_demo_pipeline_admits_and_refuses_inside_docker(tmp_path, monkeypatch, _isolated_run_artifacts):
    from agents.patch_sources import DiffPatchSource
    from examples.demo import prepare_rate_calculator_demo
    from main import run_pipeline

    monkeypatch.delenv("CERBERUS_SANDBOX", raising=False)  # default isolation: Docker
    scenario = prepare_rate_calculator_demo(str(tmp_path / "admit"))
    common = dict(issue_number=scenario.issue_number, issue_title=scenario.issue_title,
                  issue_body=scenario.issue_body, dry_run=True, mode="local",
                  repro_test_code=scenario.repro_test_code)
    assert run_pipeline(repo_dir=scenario.repo_dir, patch_source=DiffPatchSource([scenario.patch_diff]),
                        run_id="docker_admit", **common)
    admitted = _run_record(_isolated_run_artifacts, "docker_admit")
    assert admitted["final_state"] == "ADMITTED"
    assert admitted["sandbox"]["isolation"] == "docker"

    # A patch that makes the reproduction pass by breaking the rest of the module.
    breaking = (
        "--- a/rate_calculator.py\n+++ b/rate_calculator.py\n@@ -1,3 +1,3 @@\n"
        " def calculate_rate(amount: float, total: float) -> float:\n"
        "     \"\"\"Calculate the rate as amount / total.\"\"\"\n"
        "-    return amount / total\n"
        "+    return 0.0\n"
    )
    refuse = prepare_rate_calculator_demo(str(tmp_path / "refuse"))
    assert not run_pipeline(repo_dir=refuse.repo_dir, patch_source=DiffPatchSource([breaking]),
                            run_id="docker_refuse", **common)
    refused = _run_record(_isolated_run_artifacts, "docker_refuse")
    assert refused["final_state"] == "REFUSED"
    assert refused["refusal"]["code"] == "REGRESSION"
