"""Typed state for one repair run. Persisted as agent_run.json next to the gate runs it caused."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FinalStatus(str, Enum):
    ADMITTED = "ADMITTED"                         # a Cerberus gate run admitted the patch
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    NO_PATCH = "NO_PATCH"                         # the agent stopped without submitting a patch
    GAVE_UP = "GAVE_UP"                           # the agent stopped after a refusal, attempts left
    COST_LIMIT_EXCEEDED = "COST_LIMIT_EXCEEDED"
    TOKEN_LIMIT_EXCEEDED = "TOKEN_LIMIT_EXCEEDED"
    TOOL_LIMIT_EXCEEDED = "TOOL_LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    LLM_FAILURE = "LLM_FAILURE"
    SETUP_FAILURE = "SETUP_FAILURE"               # the agent's workspace could not be prepared
    ERROR = "ERROR"


class GateStatus(str, Enum):
    ADMITTED = "ADMITTED"
    REFUSED = "REFUSED"
    INVALID_PATCH = "INVALID_PATCH"   # rejected before the gate: no gate run was spent
    GATE_ERROR = "GATE_ERROR"         # the gate ended in ERROR (sandbox, setup, crash)


class GateFeedback(BaseModel):
    """What the agent learns from one verification. Built only from run.json."""
    attempt: int
    status: GateStatus
    stage: Optional[str] = None
    failure_code: Optional[str] = None
    message: str = ""
    failed_tests: List[str] = Field(default_factory=list)
    changed_files: List[str] = Field(default_factory=list)
    allowed_files: List[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    detail: str = ""
    patch_applied: Optional[bool] = None   # False when git apply failed inside the gate
    suggested_next_action: str = ""
    gate_run_id: Optional[str] = None
    run_json_path: Optional[str] = None
    duration_seconds: float = 0.0

    def for_model(self) -> Dict[str, Any]:
        return self.model_dump(exclude={"run_json_path", "duration_seconds"}, exclude_defaults=True)


class ToolCallRecord(BaseModel):
    iteration: int
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    error_code: Optional[str] = None
    duration_seconds: float
    output_chars: int


class LLMCallRecord(BaseModel):
    iteration: int
    model: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    cost_usd: Optional[float]
    stop_reason: str = ""
    tool_calls: List[str] = Field(default_factory=list)


class AttemptRecord(BaseModel):
    attempt: int
    diff: str
    changed_files: List[str]
    lines_added: int
    lines_deleted: int
    reasoning: str = ""
    agent_tested_reproduction: Optional[bool] = None  # agent's own run_test of the reproduction test
    feedback: GateFeedback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RepairState(BaseModel):
    run_id: str
    repository: str
    commit_sha: str = ""
    issue_id: str
    issue_title: str
    issue_description: str

    current_iteration: int = 0
    max_iterations: int = 3   # gate submissions allowed (MAX_ATTEMPTS)

    candidate_files: List[str] = Field(default_factory=list)   # files the agent read or edited
    relevant_tests: List[str] = Field(default_factory=list)    # tests the agent read or ran

    agent_messages: int = 0
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    llm_calls: List[LLMCallRecord] = Field(default_factory=list)
    attempts: List[AttemptRecord] = Field(default_factory=list)

    generated_patch: str = ""
    changed_files: List[str] = Field(default_factory=list)

    verification_result: Optional[GateFeedback] = None
    verification_status: Optional[GateStatus] = None
    failure_code: Optional[str] = None
    failure_feedback: str = ""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: Optional[float] = 0.0   # None once any call had no known price

    started_at: str = Field(default_factory=_now)
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0

    final_status: Optional[FinalStatus] = None
    final_message: str = ""
    trace_id: Optional[str] = None
