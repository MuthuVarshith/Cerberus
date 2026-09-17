"""
Patch sources: where candidate patches come from.

The verification gate does not care who wrote a patch. Every source implements

    generate_patch(request: PatchRequest) -> unified diff text

and the pipeline adapts it to the patch loop. Three sources exist:

  - DiffPatchSource: pre-written diffs (a human patch, a pull request's diff, a fixture).
  - LLMPatchSource: the built-in single-prompt model generator.
  - ExternalAgentPatchSource: an external coding agent (for example Claude Code or
    Codex) run headless in a scratch clone of the base commit. Its edits are
    turned into a diff by comparing file trees in Python, so no git command ever
    runs on a tree the agent touched.

External agents run on the host under their own permission model and
credentials, outside Cerberus's sandbox. Cerberus sandboxes only the
verification of what they produce. Configure agents without shell access where
possible (the Claude Code preset allows only file tools).
"""
from __future__ import annotations

import difflib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Protocol, Sequence

from harness.docker_sandbox import HARNESS_DIR, host_unsafe_environment

MAX_AGENT_FILE_BYTES = 2 * 1024 * 1024
_EXCLUDED_DIRS = {".git", HARNESS_DIR}

CLAUDE_CODE_PRESET = ["claude", "-p", "--allowedTools", "Read,Edit,Write,Glob,Grep", "--output-format", "text"]


class PatchSourceError(RuntimeError):
    """A patch source could not produce a verifiable diff."""


@dataclass
class PatchRequest:
    attempt: int
    feedback: str
    issue_number: int
    issue_title: str
    issue_body: str
    allowed_files: List[str] = field(default_factory=list)
    allowed_patterns: List[str] = field(default_factory=list)
    reproduction_test_code: str = ""
    source_repo_dir: Optional[str] = None
    base_commit: Optional[str] = None


class PatchSource(Protocol):
    name: str

    def generate_patch(self, request: PatchRequest) -> str: ...


def generator_for(source: PatchSource, template: PatchRequest) -> Callable[[int, str], str]:
    """Adapt a PatchSource to the patch loop's (attempt, feedback) -> diff callable."""
    def _generate(attempt: int, feedback: str) -> str:
        return source.generate_patch(replace(template, attempt=attempt, feedback=feedback))
    return _generate


class DiffPatchSource:
    """Pre-written diffs, returned in order; the last repeats once exhausted."""

    def __init__(self, diffs: Sequence[str], name: str = "diff"):
        if not diffs:
            raise PatchSourceError("DiffPatchSource requires at least one diff.")
        self.diffs = list(diffs)
        self.name = name

    def generate_patch(self, request: PatchRequest) -> str:
        return self.diffs[max(min(request.attempt, len(self.diffs)) - 1, 0)]


class LLMPatchSource:
    """The built-in model generator behind the PatchSource interface."""

    def __init__(self, generator):
        self.generator = generator
        self.name = f"llm:{generator.model}"

    @property
    def usage(self):
        return self.generator.usage

    def generate_patch(self, request: PatchRequest) -> str:
        return self.generator(request.attempt, request.feedback)


def build_agent_prompt(request: PatchRequest) -> str:
    scope = request.allowed_files + [f"(pattern) {p}" for p in request.allowed_patterns]
    parts = [
        f"Fix the following bug in this repository.\n\nIssue #{request.issue_number}: {request.issue_title}\n\n{request.issue_body}",
        "Rules:\n"
        "- Make the smallest change that fixes the bug.\n"
        "- Edit only these files: " + (", ".join(scope) if scope else "the files responsible for the bug") + ".\n"
        "- Do not modify, delete or add tests, CI configuration, or .cerberus.yml.\n"
        "- Do not commit. Leave your changes in the working tree.",
    ]
    if request.reproduction_test_code:
        parts.append(
            "This test currently fails and must pass after your change (it will be run by the verifier; "
            "you do not need to add it):\n\n```python\n" + request.reproduction_test_code.strip() + "\n```"
        )
    if request.attempt > 1 and request.feedback:
        parts.append(f"Your previous attempt was rejected by the verifier:\n\n```\n{request.feedback[-4000:]}\n```")
    return "\n\n".join(parts) + "\n"


def _is_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _walk_files(root: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if not (rel_dir == "." and d in _EXCLUDED_DIRS)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise PatchSourceError(f"Symbolic link in agent output cannot be verified: {os.path.relpath(full, root)}")
            rel = os.path.relpath(full, root).replace("\\", "/")
            files[rel] = full
    return files


def _diff_lines(old_text: str, new_text: str, from_name: str, to_name: str) -> List[str]:
    out: List[str] = []
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    for line in difflib.unified_diff(old_lines, new_lines, fromfile=from_name, tofile=to_name, n=3):
        if line.startswith(("---", "+++", "@@")):
            out.append(line if line.endswith("\n") else line + "\n")
        elif line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append("\\ No newline at end of file\n")
    return out


def tree_diff(base_dir: str, work_dir: str) -> str:
    """Unified diff turning base_dir into work_dir, computed without running git.

    `.git/` and `.cerberus/` at the top level are ignored. Binary files and
    symbolic links cannot be expressed or verified, so they raise.
    """
    base_files = _walk_files(base_dir)
    work_files = _walk_files(work_dir)
    chunks: List[str] = []
    for rel in sorted(set(base_files) | set(work_files)):
        old = b""
        new = b""
        if rel in base_files:
            with open(base_files[rel], "rb") as f:
                old = f.read(MAX_AGENT_FILE_BYTES + 1)
        if rel in work_files:
            with open(work_files[rel], "rb") as f:
                new = f.read(MAX_AGENT_FILE_BYTES + 1)
        if rel in base_files and rel in work_files and old == new:
            continue
        if len(old) > MAX_AGENT_FILE_BYTES or len(new) > MAX_AGENT_FILE_BYTES:
            raise PatchSourceError(f"Changed file is too large to verify: {rel}")
        if _is_binary(old) or _is_binary(new):
            raise PatchSourceError(f"Binary change cannot be verified: {rel}")
        from_name = f"a/{rel}" if rel in base_files else "/dev/null"
        to_name = f"b/{rel}" if rel in work_files else "/dev/null"
        chunks.extend(_diff_lines(old.decode("utf-8"), new.decode("utf-8"), from_name, to_name))
    return "".join(chunks)


def _host_git(args: Sequence[str], cwd: Optional[str] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, timeout=timeout, env=host_unsafe_environment(),
    )


def export_commit(source_repo_dir: str, commit: str, dest_dir: str) -> None:
    """Write the tree of `commit` from the (trusted) source repository into dest_dir."""
    res = _host_git(["-c", "core.autocrlf=false", "archive", "--format=tar", commit], cwd=source_repo_dir)
    if res.returncode != 0:
        raise PatchSourceError(f"git archive failed: {res.stderr.decode('utf-8', 'replace').strip()}")
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(res.stdout)) as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(dest_dir, filter="data")
        else:  # pragma: no cover
            tar.extractall(dest_dir)


def _remove_tree(path: str) -> None:
    def _retry(func, target, *_):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=_retry)


class ExternalAgentPatchSource:
    """Runs an external coding agent in a scratch copy of the base commit and returns its diff."""

    def __init__(
        self,
        command: Sequence[str],
        name: str = "external-agent",
        timeout: int = 1800,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        if not command:
            raise PatchSourceError("An external agent command is required.")
        self.command = list(command)
        self.name = name
        self.timeout = timeout
        self._run = runner or subprocess.run
        self.transcripts: List[Dict[str, object]] = []

    def generate_patch(self, request: PatchRequest) -> str:
        if not request.source_repo_dir or not request.base_commit:
            raise PatchSourceError("External agents need the source repository and base commit.")
        root = tempfile.mkdtemp(prefix="cerberus_agent_")
        try:
            pristine = os.path.join(root, "base")
            scratch = os.path.join(root, "work")
            export_commit(request.source_repo_dir, request.base_commit, pristine)
            export_commit(request.source_repo_dir, request.base_commit, scratch)
            prompt = build_agent_prompt(request)
            prompt_path = os.path.join(root, "prompt.md")
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(prompt)
            argv = [token.replace("{prompt_file}", prompt_path).replace("{workdir}", scratch) for token in self.command]
            try:
                proc = self._run(
                    argv,
                    cwd=scratch,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                self.transcripts.append({"attempt": request.attempt, "error": f"timed out after {self.timeout}s"})
                return ""
            except OSError as exc:
                raise PatchSourceError(f"Could not start external agent {argv[0]!r}: {exc}") from exc
            self.transcripts.append({
                "attempt": request.attempt,
                "exit_code": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-4000:],
                "stderr_tail": (proc.stderr or "")[-2000:],
            })
            return tree_diff(pristine, scratch)
        finally:
            _remove_tree(root)
