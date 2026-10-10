"""
FROZEN INTERFACE (v1.0) for running one repair. Every component (orchestrators, GitHub App,
SWE-bench harness, evaluation) builds against this module only.

    from repair.interface import RepairTask, RepairConfig, RepairResult, run_repair
    result = run_repair(task, config)

Do not change the signature or remove/rename RepairResult fields. Additive, optional fields need a
version bump (INTERFACE_VERSION) and a note in docs/plan-changes.md.

Semantics:
- `result.admitted` is True only when a Cerberus gate run (main.run_pipeline, unchanged) admitted the
  patch; nothing in the agent or orchestrator can set it otherwise.
- The orchestrator (Phase 1 loop or LangGraph) is chosen by `config` (see RepairConfig); this function
  is the same for both.
- Budgets (attempts, tool calls, tokens, cost, wall time) come from `config` and are enforced inside.
- Full detail stays in `result.agent_run_json` (RepairState) and the gate's run.json files.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from repair.config import RepairConfig
from repair.runner import DEFAULT_OUT, RepairTask
from repair.runner import run_repair as _run_repair_state
from repair.state import FinalStatus, GateStatus, RepairState

INTERFACE_VERSION = "1.0"

__all__ = ["INTERFACE_VERSION", "RepairConfig", "RepairResult", "RepairTask", "run_repair"]


class RepairResult(BaseModel):
    interface_version: str = INTERFACE_VERSION
    run_id: str
    final_status: str = Field(description="FinalStatus value, e.g. ADMITTED, MAX_ATTEMPTS_EXCEEDED, LLM_FAILURE")
    admitted: bool
    message: str = ""
    repository: str
    commit_sha: str = ""
    issue_id: str
    model: str = ""
    orchestrator: str = "loop"
    retrieval: str | None = None
    attempts: int = 0
    patch: str = Field("", description="Unified diff of the last submitted patch ('' if none)")
    changed_files: list[str] = Field(default_factory=list)
    gate_status: str | None = Field(None, description="Last gate verdict: ADMITTED / REFUSED / INVALID_PATCH / GATE_ERROR")
    failure_code: str | None = None
    failure_stage: str | None = None
    failed_tests: list[str] = Field(default_factory=list)
    gate_run_json: str | None = Field(None, description="Path to the last gate run.json")
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None
    tool_calls: int = 0
    duration_seconds: float = 0.0
    agent_run_json: str | None = Field(None, description="Path to the full RepairState record")
    trace_id: str | None = None

    @classmethod
    def from_state(cls, state: RepairState, config: RepairConfig, out_root: str) -> RepairResult:
        fb = state.verification_result
        path = os.path.join(out_root, state.run_id, "agent_run.json")
        return cls(
            run_id=state.run_id,
            final_status=state.final_status.value if state.final_status else "ERROR",
            admitted=state.final_status == FinalStatus.ADMITTED and fb is not None and fb.status == GateStatus.ADMITTED,
            message=state.final_message,
            repository=state.repository,
            commit_sha=state.commit_sha,
            issue_id=state.issue_id,
            model=state.model,
            orchestrator=str(getattr(config, "orchestrator", "loop") or "loop"),
            retrieval=config.retrieval,
            attempts=state.current_iteration,
            patch=state.generated_patch,
            changed_files=list(state.changed_files),
            gate_status=fb.status.value if fb else None,
            failure_code=fb.failure_code if fb else None,
            failure_stage=fb.stage if fb else None,
            failed_tests=list(fb.failed_tests) if fb else [],
            gate_run_json=fb.run_json_path if fb else None,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            total_tokens=state.total_tokens,
            estimated_cost=state.estimated_cost,
            tool_calls=len(state.tool_calls),
            duration_seconds=state.duration_seconds,
            agent_run_json=path if os.path.exists(path) else None,
            trace_id=state.trace_id,
        )


def run_repair(task: RepairTask, config: RepairConfig, **options: Any) -> RepairResult:
    """Run one repair and return its result.

    Optional keyword `options` (for tests and harnesses; not part of the frozen contract's required
    surface): model (ChatModel), tracer (Tracer), run_id (str), out_root (str), printer (callable).
    """
    out_root = options.pop("out_root", None) or DEFAULT_OUT
    state = _run_repair_state(task, config, out_root=out_root, **options)
    return RepairResult.from_state(state, config, out_root)
