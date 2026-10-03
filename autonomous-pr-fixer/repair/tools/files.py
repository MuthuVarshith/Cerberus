"""read_file and read_test: bounded, repository-relative reads through the sandbox's symlink-safe I/O."""
from __future__ import annotations

import ast
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from harness.scope_gate import is_test_path
from repair.tools.base import (
    INVALID_ARGUMENT, NOT_FOUND, REPRO_TEST_PATH, Tool, ToolContext, ToolError, check_readable,
)

MAX_FILE_BYTES = 1024 * 1024
MAX_LINES_PER_READ = 400


def read_lines(ctx: ToolContext, path: str) -> List[str]:
    try:
        data = ctx.sandbox.read_bytes(path, max_bytes=MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise ToolError(NOT_FOUND, f"{path} does not exist.") from None
    except IsADirectoryError:
        raise ToolError(INVALID_ARGUMENT, f"{path} is a directory.") from None
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(INVALID_ARGUMENT, f"{path} is larger than {MAX_FILE_BYTES} bytes.")
    if b"\0" in data[:8192]:
        raise ToolError(INVALID_ARGUMENT, f"{path} is a binary file.")
    return data.decode("utf-8", errors="replace").splitlines()


class ReadFileInput(BaseModel):
    path: str = Field(description="Repository-relative path, e.g. 'pkg/module.py'")
    start_line: int = Field(1, ge=1, description="First line to return (1-based)")
    end_line: Optional[int] = Field(None, ge=1, description=f"Last line to return; at most {MAX_LINES_PER_READ} lines per call")


class ReadFile(Tool):
    name = "read_file"
    description = (f"Read a bounded line range of a repository file (at most {MAX_LINES_PER_READ} lines). "
                   f"Paths are repository-relative; '{REPRO_TEST_PATH}' is the reproduction test.")
    Input = ReadFileInput

    def run(self, ctx: ToolContext, args: ReadFileInput) -> Dict[str, Any]:
        path = check_readable(args.path)
        lines = read_lines(ctx, path)
        start = min(args.start_line, max(len(lines), 1))
        end = min(args.end_line or start + MAX_LINES_PER_READ - 1, start + MAX_LINES_PER_READ - 1, len(lines))
        ctx.note(ctx.tests_seen if is_test_path(path) or path == REPRO_TEST_PATH else ctx.files_read, path)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        return {"path": path, "start_line": start, "end_line": end, "total_lines": len(lines), "content": body}


def _test_names(source: str) -> List[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names += [f"{node.name}::{n.name}" for n in node.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    return names


def list_test_files(ctx: ToolContext, limit: int = 200) -> List[str]:
    root = ctx.sandbox.workspace_dir
    found: List[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", "env", "node_modules")]
        for f in files:
            full = os.path.join(dirpath, f)
            if not f.endswith(".py") or os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            if is_test_path(rel):
                found.append(rel)
                if len(found) >= limit:
                    return sorted(found)
    return sorted(found)


class ReadTestInput(BaseModel):
    path: Optional[str] = Field(None, description="Test file to read. Omit to list the repository's test files.")


class ReadTest(Tool):
    name = "read_test"
    description = ("List test files (no path), or read one test file with its test names. "
                   f"The reproduction test that Cerberus will hold the patch to is '{REPRO_TEST_PATH}'.")
    Input = ReadTestInput

    def run(self, ctx: ToolContext, args: ReadTestInput) -> Dict[str, Any]:
        if not args.path:
            return {"reproduction_test": REPRO_TEST_PATH, "test_files": list_test_files(ctx)}
        path = check_readable(args.path)
        if path != REPRO_TEST_PATH and not is_test_path(path):
            raise ToolError(INVALID_ARGUMENT, f"{path} is not a test file; use read_file.")
        lines = read_lines(ctx, path)
        ctx.note(ctx.tests_seen, path)
        shown = lines[:MAX_LINES_PER_READ]
        return {
            "path": path,
            "tests": _test_names("\n".join(lines)),
            "total_lines": len(lines),
            "content": "\n".join(f"{n:>5}  {line}" for n, line in enumerate(shown, 1)),
        }
