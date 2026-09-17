"""
Scope gate: what did the candidate patch actually change?

Measured against the base commit:
  - changed files, including newly created (untracked) and deleted files
  - lines added and deleted
  - for Python files, the innermost enclosing function/class of every changed
    line on both sides of the diff (Python `ast` spans); other file types report
    no symbol information rather than a guess

Policy enforced: existing test files may not be modified or deleted (unless the
repository allows it), every changed file must be inside the allowed scope, and
the number of files and changed lines must be within limits. Symbols are
recorded as evidence; they are not part of the policy.
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from harness.diff_utils import DiffUtils, WorkspaceChanges
from harness.docker_sandbox import HARNESS_DIR, Sandbox

_SYMBOL_SCRIPT = r'''
import ast, json, subprocess, sys

def spans(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out = []
    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append((start, getattr(child, "end_lineno", child.lineno), name))
                visit(child, name)
            else:
                visit(child, prefix)
    visit(tree, "")
    return out

def enclosing(symbol_spans, first, last):
    names = set()
    for line in range(first, last + 1):
        best = None
        for start, end, name in symbol_spans:
            if start <= line <= end and (best is None or (end - start) < (best[1] - best[0])):
                best = (start, end, name)
        names.add(best[2] if best else "<module>")
    return names

req = json.load(open(sys.argv[1], encoding="utf-8"))
result = {}
for path, sides in req["files"].items():
    symbols = set()
    parsed = True
    if sides["old"]:
        blob = subprocess.run(["git", "cat-file", "blob", req["base"] + ":" + path], capture_output=True)
        if blob.returncode == 0:
            sp = spans(blob.stdout.decode("utf-8", "replace"))
            if sp is None:
                parsed = False
            else:
                for first, last in sides["old"]:
                    symbols |= enclosing(sp, first, last)
    if sides["new"]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                sp = spans(f.read())
        except OSError:
            sp = None
        if sp is None:
            parsed = False
        else:
            for first, last in sides["new"]:
                symbols |= enclosing(sp, first, last)
    result[path] = {"symbols": sorted(symbols), "parsed": parsed}
print(json.dumps(result))
'''


@dataclass
class ScopeReport:
    allowed_files: List[str]
    changed_files: List[str]
    new_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    unauthorized_files: List[str] = field(default_factory=list)
    modified_test_files: List[str] = field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    changed_symbols: List[str] = field(default_factory=list)
    symbols_unavailable: List[str] = field(default_factory=list)
    is_acceptable: bool = True
    violation_reason: Optional[str] = None
    diff_text: str = ""

    @property
    def total_lines(self) -> int:
        return self.lines_added + self.lines_deleted

    def to_dict(self) -> Dict[str, object]:
        return {
            "allowed_files": self.allowed_files,
            "changed_files": self.changed_files,
            "new_files": self.new_files,
            "deleted_files": self.deleted_files,
            "unauthorized_files": self.unauthorized_files,
            "modified_test_files": self.modified_test_files,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "changed_symbols": self.changed_symbols,
            "symbols_unavailable": self.symbols_unavailable,
            "is_acceptable": self.is_acceptable,
            "violation_reason": self.violation_reason,
        }


def changed_symbols(sandbox: Sandbox, changes: WorkspaceChanges) -> Dict[str, Dict[str, object]]:
    """Map changed line ranges of Python files to enclosing symbols, inside the sandbox."""
    ranges = DiffUtils.changed_line_ranges(
        _zero_context_diff(sandbox) if changes.files else ""
    )
    request = {
        "base": getattr(sandbox, "base_commit", None) or "HEAD",
        "files": {
            path: {"old": sides.get("old", []), "new": sides.get("new", [])}
            for path, sides in ranges.items()
            if path.endswith(".py")
        },
    }
    if not request["files"]:
        return {}
    sandbox.ensure_harness_dir()
    sandbox.write_file(f"{HARNESS_DIR}/scope_symbols.py", _SYMBOL_SCRIPT)
    sandbox.write_file(f"{HARNESS_DIR}/scope_request.json", json.dumps(request))
    res = sandbox.exec(f"{sandbox.python_cmd} {HARNESS_DIR}/scope_symbols.py {HARNESS_DIR}/scope_request.json")
    if res.exit_code != 0:
        return {path: {"symbols": [], "parsed": False} for path in request["files"]}
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {path: {"symbols": [], "parsed": False} for path in request["files"]}


def _zero_context_diff(sandbox: Sandbox) -> str:
    """A -U0 diff against the base, new files included, for exact line ranges."""
    base = getattr(sandbox, "base_commit", None) or "HEAD"
    untracked = [p for p in sandbox.exec("git ls-files --others --exclude-standard -z").stdout.split("\0") if p]
    pathspec = f"{HARNESS_DIR}/untracked-u0.pathspec"
    if untracked:
        sandbox.write_file(pathspec, "\0".join(untracked) + "\0")
        sandbox.exec(f"git add --intent-to-add --pathspec-from-file={pathspec} --pathspec-file-nul")
    try:
        res = sandbox.exec(f"git diff --no-color --no-ext-diff --no-renames --no-textconv -U0 {base}")
    finally:
        if untracked:
            sandbox.exec(f"git reset -q --pathspec-from-file={pathspec} --pathspec-file-nul")
    return res.stdout if res.exit_code == 0 else ""


def is_test_path(path: str) -> bool:
    """Whether a repository path is part of the test suite (Python conventions)."""
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    name = parts[-1]
    return (
        any(p in ("tests", "test", "testing") for p in parts[:-1])
        or name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def _is_allowed(path: str, allowed_files: Sequence[str], allowed_patterns: Sequence[str]) -> bool:
    return path in allowed_files or any(fnmatch.fnmatchcase(path, pattern) for pattern in allowed_patterns)


def analyze_scope(
    sandbox: Sandbox,
    allowed_files: Sequence[str],
    max_files: int = 3,
    max_lines: int = 200,
    changes: Optional[WorkspaceChanges] = None,
    allowed_patterns: Sequence[str] = (),
    allow_test_modifications: bool = False,
) -> ScopeReport:
    """Measure the change and apply the scope policy.

    A file is in scope when it is one of `allowed_files` or matches a glob in
    `allowed_patterns` (fnmatch semantics: `*` also matches `/`). Independently of
    scope, modifying or deleting an *existing* test file is refused unless
    `allow_test_modifications` is set: a patch that edits the tests it is judged
    by can make the regression gate pass without fixing anything. New test files
    are not affected by that rule.
    """
    changes = changes or DiffUtils.workspace_changes(sandbox)
    allowed = sorted({f.replace("\\", "/") for f in allowed_files})
    patterns = [p.replace("\\", "/") for p in allowed_patterns]
    report = ScopeReport(
        allowed_files=allowed + [f"pattern:{p}" for p in patterns],
        changed_files=list(changes.files),
        new_files=list(changes.new_files),
        deleted_files=list(changes.deleted_files),
        unauthorized_files=[f for f in changes.files if not _is_allowed(f, allowed, patterns)],
        modified_test_files=[f for f in changes.files if f not in changes.new_files and is_test_path(f)],
        lines_added=changes.lines_added,
        lines_deleted=changes.lines_deleted,
        diff_text=changes.diff_text,
    )

    for path, info in changed_symbols(sandbox, changes).items():
        if info.get("parsed"):
            report.changed_symbols.extend(f"{path}::{name}" for name in info.get("symbols", []))
        else:
            report.symbols_unavailable.append(path)
    report.symbols_unavailable.extend(
        f for f in changes.files if not f.endswith(".py") and f not in report.symbols_unavailable
    )

    if report.modified_test_files and not allow_test_modifications:
        report.is_acceptable = False
        report.violation_reason = (
            f"Existing test files were modified or deleted: {', '.join(report.modified_test_files)} "
            "(set scope.allow_test_modifications in .cerberus.yml to permit this)"
        )
    elif report.unauthorized_files:
        report.is_acceptable = False
        report.violation_reason = f"Files changed outside the allowed scope: {', '.join(report.unauthorized_files)}"
    elif len(changes.files) > max_files:
        report.is_acceptable = False
        report.violation_reason = f"Changed {len(changes.files)} files (limit {max_files})"
    elif changes.total_lines > max_lines:
        report.is_acceptable = False
        report.violation_reason = f"Changed {changes.total_lines} lines (limit {max_lines})"
    return report
