"""The agent's tool set. `submit_patch` is declared here but handled by the agent loop."""
from __future__ import annotations

from typing import List, Optional, Set

from pydantic import BaseModel, Field

from repair.tools.base import Tool, ToolContext, ToolRegistry  # noqa: F401
from repair.tools.files import ReadFile, ReadTest
from repair.tools.git import GitDiff, GitStatus
from repair.tools.patch import ApplyPatch, EditFile, ResetWorkspace
from repair.tools.search import FindReferences, FindSymbol, SearchCode
from repair.tools.tests import RunTest

SUBMIT_PATCH = "submit_patch"


class SubmitPatchInput(BaseModel):
    reasoning: str = Field(min_length=1, max_length=4000,
                           description="Root cause and why the change fixes it, in a few sentences")


class SubmitPatch(Tool):
    name = SUBMIT_PATCH
    description = ("Submit the current workspace diff (see git_diff) to the Cerberus verification gate. "
                   "Cerberus re-runs the reproduction test, the full regression suite and the scope check on a "
                   "fresh copy of the base commit and returns ADMITTED or a refusal with structured feedback. "
                   "Each call uses one of your limited attempts.")
    Input = SubmitPatchInput

    def run(self, ctx, args):  # pragma: no cover - intercepted by RepairAgent
        raise RuntimeError("submit_patch is handled by the agent loop")


def default_tools() -> List[Tool]:
    return [SearchCode(), ReadFile(), FindSymbol(), FindReferences(), ReadTest(), RunTest(),
            GitStatus(), GitDiff(), EditFile(), ApplyPatch(), ResetWorkspace(), SubmitPatch()]


def build_registry(allowed: Optional[Set[str]] = None) -> ToolRegistry:
    return ToolRegistry(default_tools(), allowed=allowed)
