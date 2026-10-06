"""run_test: targeted pytest runs inside the agent's sandbox. Results are advisory; only the gate decides."""
from __future__ import annotations

import os
import re
from typing import Any, Dict

from pydantic import BaseModel, Field

from repair.tools.base import (
    INVALID_ARGUMENT, NOT_FOUND, REPRO_TEST_PATH, Tool, ToolContext, ToolError, bounded, check_readable,
)
from harness.scope_gate import is_test_path

# A pytest target: a .py path, optionally followed by ::node parts. No spaces, quotes or
# shell metacharacters can pass this, so the command string below cannot be extended.
_TARGET = re.compile(r"^[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_.\[\]-]+)*$")
_TEST_DIR = re.compile(r"^[A-Za-z0-9_./-]*(tests|test|testing)/?$")


class RunTestInput(BaseModel):
    target: str = Field(description=f"Pytest target: a test directory ('tests/'), a test file or a node id, "
                                    f"e.g. '{REPRO_TEST_PATH}' or 'tests/test_x.py::test_y'")
    timeout_seconds: int = Field(60, ge=5, le=300)


class RunTest(Tool):
    name = "run_test"
    description = ("Run one test file or test id with pytest inside the Cerberus sandbox (offline, resource-limited). "
                   "Advisory only: passing here does not mean Cerberus will admit the patch.")
    Input = RunTestInput
    runs_in_sandbox = True

    def run(self, ctx: ToolContext, args: RunTestInput) -> Dict[str, Any]:
        target = args.target.strip()
        if _TEST_DIR.match(target):
            # A test directory such as 'tests/' or 'pkg/tests'.
            path = check_readable(target.rstrip("/"))
            if path.split("/")[-1] not in ("tests", "test", "testing"):
                raise ToolError(INVALID_ARGUMENT, f"{path} is not a test directory")
            full = os.path.join(ctx.sandbox.workspace_dir, *path.split("/"))
            if os.path.islink(full) or not os.path.isdir(full):
                raise ToolError(NOT_FOUND, f"{path} is not a test directory in the repository")
            target = path
        else:
            if not _TARGET.match(target):
                raise ToolError(INVALID_ARGUMENT, "target must be a test directory, a .py test path or a node id "
                                                  "without spaces or shell characters")
            path = check_readable(target.split("::", 1)[0])
            if path != REPRO_TEST_PATH and not is_test_path(path):
                raise ToolError(INVALID_ARGUMENT, f"{path} is not a test file")
            try:
                ctx.sandbox.read_bytes(path, max_bytes=1)
            except FileNotFoundError:
                raise ToolError(NOT_FOUND, f"{path} does not exist") from None
        timeout = min(args.timeout_seconds, ctx.timeout_seconds)
        # -B: no bytecode files, so the agent's workspace diff stays clean.
        command = f"{ctx.sandbox.python_cmd} -B -m pytest -p no:cacheprovider -q -rfE {target}"
        res = ctx.sandbox.exec(command, timeout=timeout)
        if res.timed_out:
            status = "TIMEOUT"
        elif res.exit_code == 0:
            status = "PASSED"
        elif res.exit_code == 1:
            status = "FAILED"
        elif res.exit_code == 5:
            status = "NO_TESTS"
        else:
            status = "ERROR"
        ctx.note(ctx.tests_seen, path)
        if path == REPRO_TEST_PATH:
            ctx.reproduction_runs.append(status == "PASSED")
        return {
            "command": f"python -m pytest {target}",
            "status": status,
            "exit_code": res.exit_code,
            "timed_out": res.timed_out,
            "duration_seconds": round(res.duration_sec, 2),
            "stdout": bounded(res.stdout, 6000),
            "stderr": bounded(res.stderr, 2000),
        }
