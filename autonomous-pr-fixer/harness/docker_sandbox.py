"""
Docker and Isolated Process Sandbox for autonomous code execution.
Follows the mini-swe-agent philosophy: minimalist, robust command execution
with timeout safety, clean environment isolation, and resource destruction.

Hardening enhancements:
- Process tree kill on timeout (no leaked orphan processes)
- Bounded stdout/stderr (max 500KB) to prevent OOM / memory exhaustion
- Path traversal guard (prevents escaping workspace_dir)
- Network isolation parameter (defaults to network disabled in Docker)
- Docker resource limits (--memory=2g, --cpus=2.0, --pids-limit=100)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from harness.py_interpreter import CONTAINER_PYTHON, HOST_PYTHON

MAX_OUTPUT_CHARS = 500_000


def _bound_output(text: str) -> str:
    """Bound output to MAX_OUTPUT_CHARS to prevent memory exhaustion."""
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n[... output truncated at 500KB to prevent memory exhaustion ...]"
    return text


def _kill_process_tree(pid: int) -> None:
    """Terminates a process and all its children across OS platforms."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool = False

    @property
    def output(self) -> str:
        combined = []
        if self.stdout:
            combined.append(self.stdout)
        if self.stderr:
            combined.append(self.stderr)
        return "\n".join(combined).strip()


class Sandbox:
    """
    Execution Sandbox with automatic fallback:
    - If Docker CLI and daemon are available: launches an isolated container with
      resource caps, non-root execution, and optional network isolation.
    - Otherwise: provisions an isolated temporary filesystem environment
      with strict process isolation, timeouts, process tree kills, and clean teardown.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        image: str = "python:3.11-slim",
        timeout_sec: int = 60,
        env: Optional[Dict[str, str]] = None,
        network_disabled: bool = True,
    ):
        self.requested_image = image
        self.timeout_sec = timeout_sec
        self.env = env or {}
        self.network_disabled = network_disabled
        self.container_id: Optional[str] = None
        self.is_docker = False
        self.is_alive = False

        self.has_docker = self._probe_docker()

        if base_dir and os.path.exists(base_dir):
            self.workspace_dir = tempfile.mkdtemp(prefix="swe_sandbox_")
            shutil.copytree(base_dir, self.workspace_dir, dirs_exist_ok=True)
            self._cleanup_workspace = True
        elif base_dir:
            os.makedirs(base_dir, exist_ok=True)
            self.workspace_dir = base_dir
            self._cleanup_workspace = False
        else:
            self.workspace_dir = tempfile.mkdtemp(prefix="swe_sandbox_")
            self._cleanup_workspace = True

        self._start()

    def _probe_docker(self) -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            res = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            return res.returncode == 0
        except Exception:
            return False

    def _start(self) -> None:
        if self.has_docker:
            try:
                cmd = [
                    "docker",
                    "run",
                    "-d",
                    "-i",
                    "--memory=2g",
                    "--cpus=2.0",
                    "--pids-limit=100",
                    "--user",
                    "1000:1000",
                    "--security-opt=no-new-privileges:true",
                    "-v",
                    f"{os.path.abspath(self.workspace_dir)}:/workspace",
                    "-w",
                    "/workspace",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "-e",
                    "PYTHONUNBUFFERED=1",
                ]
                if self.network_disabled:
                    cmd.extend(["--network", "none"])
                cmd.extend([self.requested_image, "tail", "-f", "/dev/null"])
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if proc.returncode == 0 and proc.stdout.strip():
                    container_id = proc.stdout.strip()[:12]
                    runtime_check = subprocess.run(
                        [
                            "docker",
                            "exec",
                            container_id,
                            "sh",
                            "-c",
                            "python -c 'import pytest' && git --version",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if runtime_check.returncode == 0:
                        self.container_id = container_id
                        self.is_docker = True
                        self.is_alive = True
                        return
                    subprocess.run(
                        ["docker", "rm", "-f", container_id],
                        capture_output=True,
                        timeout=10,
                    )
            except Exception:
                pass

        self.is_docker = False
        self.is_alive = True

    @property
    def python_cmd(self) -> str:
        """Interpreter token valid for commands executed inside this sandbox.

        Docker mode runs in the image, where a host interpreter path does not
        exist; fallback mode runs on the host, where a bare name may not resolve.
        """
        return CONTAINER_PYTHON if self.is_docker else HOST_PYTHON

    def _safe_resolve(self, rel_path: str) -> str:
        """Ensure rel_path does not escape workspace_dir (prevent path traversal)."""
        abs_workspace = os.path.abspath(self.workspace_dir)
        target = os.path.abspath(os.path.join(self.workspace_dir, rel_path))
        if os.path.commonpath([abs_workspace, target]) != abs_workspace:
            raise ValueError(f"Path traversal detected: '{rel_path}' escapes sandbox workspace.")
        return target

    def exec(self, cmd: str, timeout: Optional[int] = None) -> ExecResult:
        """Executes a shell command inside the sandbox with timeout and process tree killing safety."""
        if not self.is_alive:
            raise RuntimeError("Sandbox is not active or has been destroyed.")

        effective_timeout = timeout or self.timeout_sec
        start_time = time.time()

        if self.is_docker and self.container_id:
            docker_cmd = ["docker", "exec", self.container_id, "sh", "-c", cmd]
            try:
                proc = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                )
                return ExecResult(
                    exit_code=proc.returncode,
                    stdout=_bound_output(proc.stdout),
                    stderr=_bound_output(proc.stderr),
                    duration_sec=time.time() - start_time,
                )
            except subprocess.TimeoutExpired:
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Timed out after {effective_timeout}s.",
                    duration_sec=time.time() - start_time,
                    timed_out=True,
                )
        else:
            merged_env = os.environ.copy()
            merged_env.update(self.env)
            merged_env["PYTHONUNBUFFERED"] = "1"
            merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=self.workspace_dir,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=merged_env,
                    start_new_session=(os.name != "nt"),
                )
                stdout, stderr = proc.communicate(timeout=effective_timeout)
                return ExecResult(
                    exit_code=proc.returncode,
                    stdout=_bound_output(stdout),
                    stderr=_bound_output(stderr),
                    duration_sec=time.time() - start_time,
                )
            except subprocess.TimeoutExpired:
                if proc is not None:
                    _kill_process_tree(proc.pid)
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Timed out after {effective_timeout}s.",
                    duration_sec=time.time() - start_time,
                    timed_out=True,
                )

    def write_file(self, rel_path: str, content: str) -> None:
        target = self._safe_resolve(rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    def read_file(self, rel_path: str) -> str:
        target = self._safe_resolve(rel_path)
        if not os.path.exists(target):
            raise FileNotFoundError(f"{rel_path} does not exist in sandbox.")
        with open(target, "r", encoding="utf-8", newline="", errors="replace") as f:
            return f.read()

    def destroy(self) -> None:
        if not self.is_alive:
            return
        if self.is_docker and self.container_id:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container_id],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
            self.container_id = None

        if self._cleanup_workspace and os.path.exists(self.workspace_dir):
            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception:
                pass
        self.is_alive = False

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.destroy()
