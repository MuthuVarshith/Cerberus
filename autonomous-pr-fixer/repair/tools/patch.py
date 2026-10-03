"""Workspace editing: apply_patch (unified diff), edit_file (exact replacement), reset_workspace.

Edits land in the agent's own workspace only. They are proposals: the gate re-applies the
final diff to a fresh clone of the base commit and judges it there.
"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from harness.diff_utils import DiffUtils
from repair.tools.base import (
    INVALID_ARGUMENT, NOT_FOUND, PATCH_APPLY_FAILED, Tool, ToolContext, ToolError, check_writable,
)
from repair.tools.files import MAX_FILE_BYTES


def validate_diff_paths(diff: str) -> list:
    """Every path a diff touches must pass the write policy. Returns the target files."""
    forbidden = DiffUtils.forbidden_targets(diff)
    if forbidden:
        raise ToolError("PERMISSION_DENIED", f"Patch targets protected paths: {', '.join(forbidden)}")
    targets = DiffUtils.parse_targeted_files(diff)
    if not targets:
        raise ToolError(INVALID_ARGUMENT, "Not a unified diff: no '--- a/<path>' / '+++ b/<path>' headers found.")
    return [check_writable(t) for t in targets]


class ApplyPatchInput(BaseModel):
    diff: str = Field(min_length=10, max_length=100_000, description="Unified diff with a/ and b/ paths, applied with git apply")


class ApplyPatch(Tool):
    name = "apply_patch"
    description = ("Apply a unified diff to the workspace (git apply). Source files only: tests, .cerberus, .git, "
                   ".github and .cerberus.yml are refused. Prefer edit_file for small changes.")
    Input = ApplyPatchInput
    runs_in_sandbox = True

    def run(self, ctx: ToolContext, args: ApplyPatchInput) -> Dict[str, Any]:
        diff = DiffUtils.extract_diff_from_markdown(args.diff)
        targets = validate_diff_paths(diff)
        res = DiffUtils.apply_diff_to_sandbox(ctx.sandbox, diff)
        if not res.success:
            raise ToolError(PATCH_APPLY_FAILED, res.error[:1500])
        for t in targets:
            ctx.note(ctx.files_edited, t)
        ctx.invalidate_index()
        return {"applied": True, "files": targets}


class EditFileInput(BaseModel):
    path: str = Field(description="Repository-relative source file")
    old_text: str = Field(description="Exact text to replace; must occur exactly once. Empty string creates a new file.")
    new_text: str = Field(description="Replacement text")


class EditFile(Tool):
    name = "edit_file"
    description = ("Replace one exact, unique occurrence of old_text with new_text in a source file "
                   "(or create a new file when old_text is empty). Tests and infrastructure files are refused.")
    Input = EditFileInput

    def run(self, ctx: ToolContext, args: EditFileInput) -> Dict[str, Any]:
        path = check_writable(args.path)
        sb = ctx.sandbox
        try:
            data = sb.read_bytes(path, max_bytes=MAX_FILE_BYTES + 1)
            exists = True
        except FileNotFoundError:
            data, exists = b"", False
        if not exists:
            if args.old_text:
                raise ToolError(NOT_FOUND, f"{path} does not exist; pass an empty old_text to create it.")
            sb.write_file(path, args.new_text)
            ctx.note(ctx.files_edited, path)
            ctx.invalidate_index()
            return {"path": path, "created": True}
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(INVALID_ARGUMENT, f"{path} is too large to edit.")
        if not args.old_text:
            raise ToolError(INVALID_ARGUMENT, f"{path} exists; old_text must not be empty.")
        text = data.decode("utf-8")
        old, new = args.old_text, args.new_text
        if "\r\n" in text and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count != 1:
            raise ToolError(INVALID_ARGUMENT if count else NOT_FOUND,
                            f"old_text occurs {count} times in {path}; it must occur exactly once (include more context).")
        sb.write_file(path, text.replace(old, new, 1))
        ctx.note(ctx.files_edited, path)
        ctx.invalidate_index()
        return {"path": path, "replaced": True}


class NoInput(BaseModel):
    pass


class ResetWorkspace(Tool):
    name = "reset_workspace"
    description = "Discard all of your changes and return the workspace to the base commit (the reproduction test is kept)."
    Input = NoInput
    runs_in_sandbox = True

    def run(self, ctx: ToolContext, args: NoInput) -> Dict[str, Any]:
        DiffUtils.rollback(ctx.sandbox)
        ctx.invalidate_index()
        return {"reset": True}
