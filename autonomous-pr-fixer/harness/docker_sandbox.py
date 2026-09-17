"""
Execution sandbox for repository code.

Principle: untrusted repository code never runs with the host's credentials.

Isolation modes:
  - ``docker`` (default): commands run in a container with no network, dropped
    capabilities, no-new-privileges, memory/CPU/PID limits, a tmpfs /tmp, an
    in-container kill timeout, and an environment containing no host variables.
    Dependency installation, which needs network, runs first in a separate
    networked setup container whose filesystem is committed to an image; the
    repair container then starts from that image with networking disabled.
    If Docker or the sandbox image is unavailable, construction fails. There is
    no fallback.
  - ``host-unsafe``: commands run on the host as the current user. It must be
    requested explicitly, prints a warning, passes only an allowlisted set of
    environment variables (no tokens or API keys), and does not run dependency
    installation. It is for trusted local fixtures and tests only; the pipeline
    never publishes from it.

Workspaces are fresh clones of the source repository's HEAD commit, so
uncommitted changes in the source are never part of what is verified.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from harness.py_interpreter import CONTAINER_PYTHON, HOST_PYTHON

MAX_OUTPUT_CHARS = 500_000

ISOLATION_DOCKER = "docker"
ISOLATION_HOST_UNSAFE = "host-unsafe"
ISOLATION_MODES = (ISOLATION_DOCKER, ISOLATION_HOST_UNSAFE)

DEFAULT_IMAGE = "cerberus-sandbox:py3.11"
HARNESS_DIR = ".cerberus"

CONTAINER_WORKDIR = "/workspace"

#: Host variables passed through in host-unsafe mode. Everything else, including
#: GITHUB_TOKEN and model API keys, is dropped.
HOST_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "LC_CTYPE",
    "USER", "USERNAME", "SHELL", "TERM",
)
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")


class SandboxError(RuntimeError):
    """The sandbox cannot provide the isolation or workspace a run requires."""


class SandboxUnavailableError(SandboxError):
    """Safe isolation is not available on this machine."""


class WorkspaceError(SandboxError):
    """The source repository cannot be turned into a verifiable workspace."""


def _bound_output(text: Optional[str]) -> str:
    """Bound output to MAX_OUTPUT_CHARS to prevent memory exhaustion."""
    if text is None:
        return ""
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


def _remove_tree(path: str) -> None:
    """Delete a directory tree, including read-only files git creates on Windows."""
    def _retry(func, target, *_):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)


def resolve_isolation(explicit: Optional[str] = None) -> str:
    """Explicit argument, then CERBERUS_SANDBOX, then Docker."""
    mode = (explicit or os.environ.get("CERBERUS_SANDBOX", "") or ISOLATION_DOCKER).strip().lower()
    if mode not in ISOLATION_MODES:
        raise SandboxError(f"Unknown sandbox isolation mode {mode!r}; expected one of {ISOLATION_MODES}.")
    return mode


def host_unsafe_environment(source: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for host-unsafe commands: allowlisted, and never a secret."""
    source = os.environ if source is None else source
    upper = {k.upper(): k for k in source}
    env: Dict[str, str] = {}
    for name in HOST_ENV_ALLOWLIST:
        key = upper.get(name)
        if key is None:
            continue
        if any(marker in name for marker in _SECRET_MARKERS):
            continue
        env[key] = source[key]
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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


def _run_host_git(args: Sequence[str], cwd: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=host_unsafe_environment(),
    )


class Sandbox:
    """A cloned workspace plus an isolated place to run commands against it."""

    def __init__(
        self,
        base_dir: Optional[str] = None,
        image: Optional[str] = None,
        timeout_sec: int = 60,
        isolation: Optional[str] = None,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        base_ref: Optional[str] = None,
    ):
        self.isolation = resolve_isolation(isolation)
        self.image = image or os.environ.get("CERBERUS_SANDBOX_IMAGE", "") or DEFAULT_IMAGE
        self.timeout_sec = timeout_sec
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.is_alive = False
        self.container_id: Optional[str] = None
        self.setup_image: Optional[str] = None
        self.setup_skipped = False
        self.base_commit: Optional[str] = None
        self.source_dirty = False
        self._repair_started = False
        self._run_id = uuid.uuid4().hex[:12]

        if self.is_docker:
            self._require_docker()
        else:
            print(
                "[UNSAFE] host-unsafe sandbox: repository code runs on this machine as the current user, "
                "without container isolation. Use only for trusted local fixtures.",
                file=sys.stderr,
            )

        # The workspace is a subdirectory of a private (0700) temp root so that it
        # can be made world-writable for the container user on POSIX hosts
        # without exposing it to other local users.
        self._root_dir = tempfile.mkdtemp(prefix="cerberus_ws_")
        self.workspace_dir = os.path.join(self._root_dir, "ws")
        try:
            if base_dir is not None:
                self._clone_source(base_dir, base_ref)
            else:
                os.makedirs(self.workspace_dir)
        except Exception:
            _remove_tree(self._root_dir)
            raise
        self.is_alive = True

    # ------------------------------------------------------------------ setup

    @property
    def is_docker(self) -> bool:
        return self.isolation == ISOLATION_DOCKER

    @property
    def python_cmd(self) -> str:
        """Interpreter token valid for commands executed inside this sandbox."""
        return CONTAINER_PYTHON if self.is_docker else HOST_PYTHON

    def _clone_source(self, base_dir: str, base_ref: Optional[str] = None) -> None:
        if not os.path.isdir(base_dir):
            raise WorkspaceError(f"Repository path does not exist: {base_dir}")
        ref = base_ref or "HEAD"
        if ref.startswith("-"):
            raise WorkspaceError(f"Invalid base revision {ref!r}.")
        head = _run_host_git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=base_dir)
        if head.returncode != 0:
            if base_ref:
                raise WorkspaceError(f"Base revision {base_ref!r} does not name a commit in {base_dir}.")
            raise WorkspaceError(
                f"{base_dir} is not a git repository with at least one commit; "
                "there is no base revision to verify against."
            )
        self.base_commit = head.stdout.strip()
        status = _run_host_git(["status", "--porcelain"], cwd=base_dir)
        self.source_dirty = base_ref is None and bool(status.stdout.strip())

        # core.autocrlf=false before checkout: host git and container git must see
        # identical bytes, or every line reads as modified.
        clone = _run_host_git(
            ["clone", "--quiet", "--no-checkout", "--no-hardlinks", "-c", "core.autocrlf=false",
             os.path.abspath(base_dir), self.workspace_dir],
            timeout=600,
        )
        if clone.returncode != 0:
            raise WorkspaceError(f"git clone failed: {clone.stderr.strip()}")
        if _run_host_git(["cat-file", "-e", f"{self.base_commit}^{{commit}}"], cwd=self.workspace_dir).returncode != 0:
            fetch = _run_host_git(["fetch", "--quiet", os.path.abspath(base_dir), self.base_commit], cwd=self.workspace_dir)
            if fetch.returncode != 0:
                raise WorkspaceError(f"Could not fetch base commit {self.base_commit}: {fetch.stderr.strip()}")
        checkout = _run_host_git(["checkout", "--quiet", "--detach", self.base_commit], cwd=self.workspace_dir)
        if checkout.returncode != 0:
            raise WorkspaceError(f"git checkout of {self.base_commit} failed: {checkout.stderr.strip()}")
        _run_host_git(["config", "user.name", "Cerberus"], cwd=self.workspace_dir)
        _run_host_git(["config", "user.email", "cerberus@localhost"], cwd=self.workspace_dir)
        self.ensure_harness_dir()

    def ensure_harness_dir(self) -> str:
        """Create `.cerberus/` for harness files and hide it from git.

        Files written there (reproduction tests, JUnit reports) never appear in a
        diff, are never collected by a normal pytest run (hidden directory), and
        are never committed.
        """
        path = os.path.join(self.workspace_dir, HARNESS_DIR)
        os.makedirs(path, exist_ok=True)
        info_dir = os.path.join(self.workspace_dir, ".git", "info")
        if os.path.isdir(os.path.join(self.workspace_dir, ".git")):
            os.makedirs(info_dir, exist_ok=True)
            exclude = os.path.join(info_dir, "exclude")
            existing = ""
            if os.path.exists(exclude):
                with open(exclude, "r", encoding="utf-8") as f:
                    existing = f.read()
            if f"/{HARNESS_DIR}/" not in existing.splitlines():
                with open(exclude, "a", encoding="utf-8") as f:
                    f.write(f"\n/{HARNESS_DIR}/\n")
        return path

    def run_setup(self, commands: Sequence[str], timeout: Optional[int] = None) -> List[ExecResult]:
        """Run dependency installation in a networked phase, before any repair command.

        Docker: a setup container with network access runs the commands; its
        filesystem is committed to an image, and later commands run in a fresh
        container from that image with networking disabled.
        Host-unsafe: installation is skipped, so nothing is installed into the
        host interpreter; `setup_skipped` records that.
        """
        if not self.is_alive:
            raise SandboxError("Sandbox is not active or has been destroyed.")
        if self._repair_started:
            raise SandboxError("Setup must run before any repair-phase command.")
        if not commands:
            return []
        if not self.is_docker:
            self.setup_skipped = True
            print(
                "[UNSAFE] host-unsafe sandbox: skipping dependency installation "
                f"({len(commands)} command(s)) to avoid modifying the host interpreter.",
                file=sys.stderr,
            )
            return []

        effective_timeout = timeout or self.timeout_sec
        self._prepare_mount()
        setup_container = self._start_container(self.image, network="bridge")
        results: List[ExecResult] = []
        try:
            for cmd in commands:
                res = self._docker_exec(setup_container, cmd, effective_timeout)
                results.append(res)
                if res.exit_code != 0:
                    return results
            image_tag = f"cerberus-setup-{self._run_id}"
            commit = self._docker(["commit", setup_container, image_tag], timeout=300)
            if commit.returncode != 0:
                raise SandboxError(f"docker commit failed: {commit.stderr.strip()}")
            self.setup_image = image_tag
            return results
        finally:
            self._docker(["rm", "-f", setup_container], timeout=60)

    # --------------------------------------------------------------- docker

    def _docker(self, args: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """Single choke point for Docker CLI calls (and for tests to intercept)."""
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def _require_docker(self) -> None:
        if shutil.which("docker") is None:
            raise SandboxUnavailableError(
                "Docker is required for sandboxed execution and was not found. Install Docker, or pass "
                "--unsafe-local-sandbox for trusted local fixtures only."
            )
        try:
            info = self._docker(["info"], timeout=15)
        except Exception as exc:
            raise SandboxUnavailableError(f"Docker is installed but not usable: {exc}") from exc
        if info.returncode != 0:
            raise SandboxUnavailableError(f"Docker daemon is not reachable: {info.stderr.strip()[:300]}")
        inspect = self._docker(["image", "inspect", self.image], timeout=30)
        if inspect.returncode != 0:
            raise SandboxUnavailableError(
                f"Sandbox image {self.image!r} is not available. Build it with: "
                f"docker build -t {self.image} sandbox/"
            )

    def container_args(self, image: str, network: str, name: str) -> List[str]:
        """`docker run` arguments for a sandbox container."""
        return [
            "run", "-d",
            "--name", name,
            "--network", network,
            "--memory", self.memory,
            "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,size=512m",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "GIT_TERMINAL_PROMPT=0",
            "-v", f"{os.path.abspath(self.workspace_dir)}:{CONTAINER_WORKDIR}",
            "-w", CONTAINER_WORKDIR,
            image,
            "sleep", "infinity",
        ]

    def _prepare_mount(self) -> None:
        if os.name != "nt":
            # Container root has no CAP_DAC_OVERRIDE, so it can only write where
            # "other" may. The private temp root keeps this from other host users.
            for dirpath, dirnames, filenames in os.walk(self.workspace_dir):
                os.chmod(dirpath, 0o777)
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    if not os.path.islink(full):
                        os.chmod(full, os.stat(full).st_mode | 0o666)

    def _start_container(self, image: str, network: str) -> str:
        name = f"cerberus-{self._run_id}-{network}"
        res = self._docker(self.container_args(image, network, name), timeout=120)
        if res.returncode != 0 or not res.stdout.strip():
            raise SandboxUnavailableError(f"docker run failed: {res.stderr.strip()[:500]}")
        return res.stdout.strip()[:12]

    def _ensure_repair_container(self) -> str:
        if self.container_id is None:
            self._prepare_mount()
            self.container_id = self._start_container(self.setup_image or self.image, network="none")
        self._repair_started = True
        return self.container_id

    def _docker_exec(self, container: str, cmd: str, timeout: int) -> ExecResult:
        start = time.time()
        # The kill timer runs inside the container: timing out the docker CLI on
        # the host would leave the process running in the container.
        args = [
            "exec", "-w", CONTAINER_WORKDIR, container,
            "timeout", "-s", "KILL", str(int(timeout)),
            "sh", "-c", f"umask 0000; {cmd}",
        ]
        try:
            proc = self._docker(args, timeout=int(timeout) + 30)
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"Timed out after {timeout}s.", time.time() - start, timed_out=True)
        duration = time.time() - start
        timed_out = proc.returncode == 137 and duration >= timeout - 1
        return ExecResult(
            exit_code=124 if timed_out else proc.returncode,
            stdout=_bound_output(proc.stdout),
            stderr=_bound_output(proc.stderr) + (f"\nTimed out after {timeout}s." if timed_out else ""),
            duration_sec=duration,
            timed_out=timed_out,
        )

    # ------------------------------------------------------------- commands

    def _safe_resolve(self, rel_path: str) -> str:
        """Ensure rel_path does not escape workspace_dir (prevent path traversal)."""
        abs_workspace = os.path.abspath(self.workspace_dir)
        target = os.path.abspath(os.path.join(self.workspace_dir, rel_path))
        if os.path.commonpath([abs_workspace, target]) != abs_workspace:
            raise ValueError(f"Path traversal detected: '{rel_path}' escapes sandbox workspace.")
        return target

    def exec(self, cmd: str, timeout: Optional[int] = None) -> ExecResult:
        """Execute a shell command against the workspace under the sandbox's isolation."""
        if not self.is_alive:
            raise RuntimeError("Sandbox is not active or has been destroyed.")
        effective_timeout = timeout or self.timeout_sec
        if self.is_docker:
            return self._docker_exec(self._ensure_repair_container(), cmd, effective_timeout)
        self._repair_started = True
        return self._host_exec(cmd, effective_timeout)

    def _host_exec(self, cmd: str, timeout: int) -> ExecResult:
        start_time = time.time()
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=host_unsafe_environment(),
                start_new_session=(os.name != "nt"),
            )
            stdout, stderr = proc.communicate(timeout=timeout)
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
                stderr=f"Timed out after {timeout}s.",
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
        if self.is_docker:
            if self.container_id:
                try:
                    self._docker(["rm", "-f", self.container_id], timeout=60)
                except Exception:
                    pass
                self.container_id = None
            if self.setup_image:
                try:
                    self._docker(["rmi", "-f", self.setup_image], timeout=120)
                except Exception:
                    pass
        if os.path.exists(self._root_dir):
            _remove_tree(self._root_dir)
        self.is_alive = False

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.destroy()
