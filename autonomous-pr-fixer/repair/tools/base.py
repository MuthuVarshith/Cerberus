"""
Tool framework: typed inputs and outputs, an explicit permission policy, timeouts and
structured errors.

Every tool works on the agent's own workspace (a Cerberus `Sandbox` cloned from the base
commit). Host-side tools only read workspace files through the sandbox's symlink-refusing
I/O; anything that executes repository code goes through `Sandbox.exec`, which in the
default isolation is the offline Docker container. There is no host shell path.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Type

from pydantic import BaseModel, ValidationError

from harness.docker_sandbox import HARNESS_DIR, Sandbox
from harness.repo_config import CONFIG_FILENAME
from harness.scope_gate import is_test_path
from repair.llm.provider import ToolSpec

logger = logging.getLogger("cerberus.repair.tools")

REPRO_TEST_PATH = f"{HARNESS_DIR}/test_reproduce.py"

# Error codes returned to the model (and recorded) instead of exceptions.
PERMISSION_DENIED = "PERMISSION_DENIED"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
TOOL_TIMEOUT = "TOOL_TIMEOUT"
TOOL_FAILURE = "TOOL_FAILURE"
PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"

_SENSITIVE_NAME = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key|.*\.p12|.*\.pfx|credentials(\.json)?)$",
    re.IGNORECASE,
)
_PROTECTED_DIRS = (".git", HARNESS_DIR, ".github")


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_repo_path(path: str) -> str:
    """Repository-relative POSIX path, or ToolError. Rejects absolute paths and traversal."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError(INVALID_ARGUMENT, "A repository-relative path is required.")
    raw = path.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("~"):
        raise ToolError(PERMISSION_DENIED, f"Absolute or home paths are not allowed: {path}")
    if any(part == ".." for part in raw.split("/")):
        raise ToolError(PERMISSION_DENIED, f"Path traversal is not allowed: {path}")
    norm = posixpath.normpath(raw)
    if norm in (".", ""):
        raise ToolError(INVALID_ARGUMENT, "A file path is required, not the repository root.")
    if _SENSITIVE_NAME.search(norm):
        raise ToolError(PERMISSION_DENIED, f"Access to credential-like files is not allowed: {norm}")
    return norm


def check_readable(path: str) -> str:
    norm = normalize_repo_path(path)
    if norm == REPRO_TEST_PATH:
        return norm
    if norm.split("/")[0] in _PROTECTED_DIRS:
        raise ToolError(PERMISSION_DENIED, f"{norm} is outside what the agent may read.")
    return norm


def check_writable(path: str) -> str:
    """The agent may change source files only: never tests, harness files, git, CI or policy."""
    norm = normalize_repo_path(path)
    if norm.split("/")[0] in _PROTECTED_DIRS or norm == CONFIG_FILENAME:
        raise ToolError(PERMISSION_DENIED, f"{norm} is verification or repository infrastructure; it may not be changed.")
    if is_test_path(norm):
        raise ToolError(PERMISSION_DENIED, f"{norm} is a test file; the agent may not modify or weaken tests.")
    return norm


@dataclass
class ToolContext:
    sandbox: Sandbox
    timeout_seconds: int = 120
    max_output_chars: int = 12_000
    files_read: List[str] = field(default_factory=list)
    files_edited: List[str] = field(default_factory=list)
    tests_seen: List[str] = field(default_factory=list)
    reproduction_runs: List[bool] = field(default_factory=list)  # outcomes of agent runs of the reproduction test
    retrieval_kind: Optional[str] = None  # None: retrieve_code is not offered (Phase 1 behaviour)
    _index: Any = None
    _retriever: Any = None

    def retriever(self) -> Any:
        from repair.retrieval.service import WorkspaceRetriever
        if self._retriever is None:
            self._retriever = WorkspaceRetriever(self.sandbox.workspace_dir, getattr(self.sandbox, "base_commit", None),
                                                 kind=self.retrieval_kind or "bm25")
            self._retriever.build()
        return self._retriever

    def note(self, bucket: List[str], item: str) -> None:
        if item not in bucket:
            bucket.append(item)

    def index(self):
        from retrieval.ast_index import ASTIndexer
        if self._index is None:
            idx = ASTIndexer(self.sandbox.workspace_dir)
            idx.index_repository()
            self._index = idx
        return self._index

    def invalidate_index(self) -> None:
        self._index = None


class Tool:
    name: str = ""
    description: str = ""
    Input: Type[BaseModel]
    runs_in_sandbox: bool = False  # executes repository code via Sandbox.exec

    def run(self, ctx: ToolContext, args: BaseModel) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def spec(self) -> ToolSpec:
        schema = self.Input.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)


@dataclass
class ToolOutcome:
    ok: bool
    payload: Dict[str, Any]
    error_code: Optional[str]
    duration_seconds: float

    def to_text(self, limit: int) -> str:
        text = json.dumps(self.payload, ensure_ascii=False, default=str)
        if len(text) > limit:
            text = text[:limit] + f'... [truncated {len(text) - limit} chars]'
        return text


def bounded(text: str, limit: int, keep: str = "tail") -> str:
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return (f"[{cut} chars omitted]\n" + text[-limit:]) if keep == "tail" else (text[:limit] + f"\n[{cut} chars omitted]")


class ToolRegistry:
    def __init__(self, tools: List[Tool], allowed: Optional[Set[str]] = None):
        self.tools: Dict[str, Tool] = {t.name: t for t in tools}
        self.allowed = set(allowed) if allowed is not None else set(self.tools)

    def specs(self) -> List[ToolSpec]:
        return [t.spec() for name, t in self.tools.items() if name in self.allowed]

    def execute(self, ctx: ToolContext, name: str, raw_args: Dict[str, Any]) -> ToolOutcome:
        started = time.monotonic()

        def done(ok: bool, payload: Dict[str, Any], code: Optional[str]) -> ToolOutcome:
            outcome = ToolOutcome(ok, payload, code, round(time.monotonic() - started, 3))
            logger.info(json.dumps({"event": "tool", "tool": name, "ok": ok, "error_code": code,
                                    "duration": outcome.duration_seconds}))
            return outcome

        tool = self.tools.get(name)
        if tool is None or name not in self.allowed:
            return done(False, {"error": PERMISSION_DENIED, "message": f"Tool '{name}' is not available."}, PERMISSION_DENIED)
        try:
            args = tool.Input.model_validate(raw_args or {})
        except ValidationError as exc:
            return done(False, {"error": INVALID_ARGUMENT, "message": str(exc)[:800]}, INVALID_ARGUMENT)

        result: Dict[str, Any] = {}
        failure: List[BaseException] = []

        def target() -> None:
            try:
                result.update(tool.run(ctx, args))
            except BaseException as exc:  # reported to the model as a structured error
                failure.append(exc)

        # Sandbox tools enforce their own timeout inside the container; host-side tools are
        # bounded here so a pathological read cannot stall the run.
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(ctx.timeout_seconds + (15 if tool.runs_in_sandbox else 0))
        if worker.is_alive():
            return done(False, {"error": TOOL_TIMEOUT, "message": f"{name} exceeded {ctx.timeout_seconds}s."}, TOOL_TIMEOUT)
        if failure:
            exc = failure[0]
            if isinstance(exc, ToolError):
                return done(False, {"error": exc.code, "message": exc.message}, exc.code)
            if isinstance(exc, FileNotFoundError):
                return done(False, {"error": NOT_FOUND, "message": str(exc)[:300]}, NOT_FOUND)
            return done(False, {"error": TOOL_FAILURE, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}, TOOL_FAILURE)
        if result.get("timed_out"):
            return done(False, result, TOOL_TIMEOUT)
        return done(True, result, None)

