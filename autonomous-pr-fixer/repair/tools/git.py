"""git_status and git_diff: read-only views of the agent's changes against the base commit.

Both reuse `DiffUtils.workspace_changes`, which runs fixed git commands inside the sandbox.
No other git operation is exposed to the model.
"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from harness.diff_utils import DiffUtils
from repair.tools.base import Tool, ToolContext, bounded


class NoInput(BaseModel):
    pass


class GitStatus(Tool):
    name = "git_status"
    description = "List files changed in the workspace relative to the base commit (new, modified, deleted) with line counts."
    Input = NoInput
    runs_in_sandbox = True

    def run(self, ctx: ToolContext, args: NoInput) -> Dict[str, Any]:
        ch = DiffUtils.workspace_changes(ctx.sandbox)
        return {
            "changed_files": ch.files,
            "new_files": ch.new_files,
            "deleted_files": ch.deleted_files,
            "lines_added": ch.lines_added,
            "lines_deleted": ch.lines_deleted,
        }


class GitDiff(Tool):
    name = "git_diff"
    description = "Show the unified diff of the workspace against the base commit. This is what submit_patch sends to Cerberus."
    Input = NoInput
    runs_in_sandbox = True

    def run(self, ctx: ToolContext, args: NoInput) -> Dict[str, Any]:
        ch = DiffUtils.workspace_changes(ctx.sandbox)
        return {"diff": bounded(ch.diff_text, 10_000, keep="head"), "changed_files": ch.files}
