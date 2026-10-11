"""Typed state for the LangGraph orchestrator.

`GraphState` is the LangGraph channel state: JSON-compatible values only, so the SQLite
checkpointer can persist it and a run can be resumed by run id. `GraphRepairState` extends the
Phase 1 `RepairState` (agent_run.json) additively, so repair/metrics and evaluation work unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field

from repair.state import RepairState

GRAPH_VERSION = "g1.0"


class GraphState(TypedDict, total=False):
    run_id: str
    task: Dict[str, Any]               # repo_dir, issue_id, title, body, reproduction_test, base_ref, issue_number
    route: str                         # successor chosen by the node that just ran
    analysis: str
    search_terms: List[str]
    suspect_files: List[str]
    allowed_files: List[str]           # from the gate's scope feedback
    context_text: str                  # retrieved code, inside untrusted-data delimiters
    pending_reasoning: str
    last_feedback: Optional[Dict[str, Any]]   # GateFeedback of the latest gate run (only CerberusGate sets it)
    diagnosis: str
    review_decision: Optional[str]
    review_comment: str
    final_status: Optional[str]
    final_message: str
    workspace_diff: str                # workspace at the end of the node (restored on resume)
    tool_ctx: Dict[str, List[Any]]
    record: Dict[str, Any]             # GraphRepairState snapshot (budgets / accounting)


class NodeTransition(BaseModel):
    node: str
    next: str
    started_at: str
    duration_seconds: float
    attempt: int
    total_tokens: int
    tool_calls: int
    note: str = ""


class GraphRepairState(RepairState):
    orchestrator: str = "langgraph"
    graph_version: str = GRAPH_VERSION
    node_history: List[NodeTransition] = Field(default_factory=list)
    suspect_files: List[str] = Field(default_factory=list)
    review_decision: Optional[str] = None   # pending | approve | request_changes | reject
    review_comment: str = ""
    pr_artifact: Optional[str] = None       # dry-run PR directory (create_pr)
    resumed: int = 0
