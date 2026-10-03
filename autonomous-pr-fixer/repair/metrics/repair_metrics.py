"""
Repair metrics.

Definitions:
  successful repair    the Cerberus gate admitted the agent's patch (the only success there is)
  verified correct     admitted AND the independent hidden check passed
  false admission      admitted AND the independent hidden check failed;
                       NOT_MEASURED when the task has no hidden check (never reported as zero)
  agent-claimed success the agent's own last run of the reproduction test passed before a
                       submission, whatever Cerberus then decided
  localization success  the final patch touched every ground-truth bug file; NOT_MEASURED
                       when the task has no ground truth or no patch was produced
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel, Field

from repair.state import FinalStatus, GateStatus, RepairState

NOT_MEASURED = "NOT_MEASURED"
Measured = Union[bool, str, None]


class TaskResult(BaseModel):
    task_id: str
    final_status: str
    admitted: bool
    attempts: int
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Optional[float]
    tool_calls: int
    tool_usage: Dict[str, int] = Field(default_factory=dict)
    patch_files: List[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    gate_results: List[str] = Field(default_factory=list)      # per attempt: ADMITTED / REFUSED:CODE / INVALID_PATCH
    patch_apply_failures: int = 0
    failure_stage: Optional[str] = None
    failure_code: Optional[str] = None
    agent_claimed_success: bool = False
    patch_generated: bool = False
    hidden_check: Measured = NOT_MEASURED       # True/False when measured
    false_admission: Measured = NOT_MEASURED
    localization_success: Measured = NOT_MEASURED
    ground_truth_files: List[str] = Field(default_factory=list)
    model: str = ""
    run_id: str = ""
    note: str = ""


def task_result_from_state(task_id: str, state: RepairState, ground_truth_files: Sequence[str] = (),
                           hidden_check: Measured = NOT_MEASURED) -> TaskResult:
    usage: Dict[str, int] = {}
    for c in state.tool_calls:
        usage[c.tool] = usage.get(c.tool, 0) + 1
    last = state.attempts[-1] if state.attempts else None
    admitted = state.final_status == FinalStatus.ADMITTED
    gate_results = []
    for a in state.attempts:
        fb = a.feedback
        gate_results.append(fb.status.value if fb.status in (GateStatus.ADMITTED, GateStatus.INVALID_PATCH)
                            else f"{fb.status.value}:{fb.failure_code or '-'}")
    if admitted and isinstance(hidden_check, bool):
        false_admission: Measured = not hidden_check
    elif admitted:
        false_admission = NOT_MEASURED
    else:
        false_admission = False  # nothing was admitted, so nothing was falsely admitted
    patch_files = list(last.changed_files) if last else []
    if ground_truth_files and patch_files:
        localization: Measured = all(f in patch_files for f in ground_truth_files)
    else:
        localization = NOT_MEASURED
    return TaskResult(
        task_id=task_id,
        final_status=state.final_status.value if state.final_status else "UNKNOWN",
        admitted=admitted,
        attempts=state.current_iteration,
        duration_seconds=state.duration_seconds,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        total_tokens=state.total_tokens,
        cost_usd=state.estimated_cost,
        tool_calls=len(state.tool_calls),
        tool_usage=usage,
        patch_files=patch_files,
        lines_added=last.lines_added if last else 0,
        lines_deleted=last.lines_deleted if last else 0,
        gate_results=gate_results,
        patch_apply_failures=sum(1 for a in state.attempts if a.feedback.patch_applied is False),
        failure_stage=None if admitted or not last else last.feedback.stage,
        failure_code=None if admitted else (last.feedback.failure_code if last else state.final_status.value if state.final_status else None),
        agent_claimed_success=any(a.agent_tested_reproduction for a in state.attempts),
        patch_generated=any(a.changed_files for a in state.attempts),
        hidden_check=hidden_check if admitted else NOT_MEASURED,
        false_admission=false_admission,
        localization_success=localization,
        ground_truth_files=list(ground_truth_files),
        model=state.model,
        run_id=state.run_id,
    )


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


def _mean(xs: List[float]) -> Optional[float]:
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def aggregate(results: Sequence[TaskResult]) -> Dict[str, Any]:
    n = len(results)
    admitted = [r for r in results if r.admitted]
    measured_fa = [r for r in admitted if isinstance(r.false_admission, bool)]
    loc = [r for r in results if isinstance(r.localization_success, bool)]
    costs = [r.cost_usd for r in results]
    known_costs = [c for c in costs if c is not None]
    all_attempts = [a for r in results for a in r.gate_results]

    def code_count(code: str) -> int:
        return sum(1 for g in all_attempts if g.endswith(":" + code))

    total_cost = round(sum(known_costs), 4) if known_costs and len(known_costs) == n else None
    tool_usage: Dict[str, int] = {}
    for r in results:
        for k, v in r.tool_usage.items():
            tool_usage[k] = tool_usage.get(k, 0) + v
    return {
        "total_tasks": n,
        "successful_repairs": len(admitted),
        "failed_repairs": n - len(admitted),
        "admitted_patches": len(admitted),
        "rejected_patches": sum(1 for g in all_attempts if g != "ADMITTED"),
        "patch_submissions": len(all_attempts),
        "fix_rate": _rate(len(admitted), n),
        "verified_correct_fixes": sum(1 for r in admitted if r.hidden_check is True),
        "false_admissions": sum(1 for r in measured_fa if r.false_admission),
        "false_admission_rate": _rate(sum(1 for r in measured_fa if r.false_admission), len(measured_fa)) if measured_fa else NOT_MEASURED,
        "false_admission_measured_on": len(measured_fa),
        "agent_claimed_success": sum(1 for r in results if r.agent_claimed_success),
        "attempts_per_task": _mean([r.attempts for r in results]),
        "attempts_per_successful_fix": _mean([r.attempts for r in admitted]),
        "median_attempts": _median([r.attempts for r in results]),
        "average_repair_time": _mean([r.duration_seconds for r in results]),
        "median_repair_time": _median([r.duration_seconds for r in results]),
        "total_cost": total_cost,
        "average_cost": round(total_cost / n, 4) if total_cost is not None and n else None,
        "cost_per_successful_fix": round(total_cost / len(admitted), 4) if total_cost is not None and admitted else None,
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "total_tokens": sum(r.total_tokens for r in results),
        "tool_calls": sum(r.tool_calls for r in results),
        "tool_usage": dict(sorted(tool_usage.items(), key=lambda kv: -kv[1])),
        "invalid_patches": sum(1 for g in all_attempts if g == "INVALID_PATCH"),
        "patch_application_failures": sum(r.patch_apply_failures for r in results),
        "green_failures": code_count("GREEN_NOT_REACHED") + code_count("GREEN_NONDETERMINISTIC"),
        "regression_failures": code_count("REGRESSION") + code_count("REGRESSION_UNVERIFIABLE"),
        "scope_failures": code_count("SCOPE_VIOLATION"),
        "gate_errors": sum(1 for g in all_attempts if g.startswith("GATE_ERROR")),
        "localization_success": sum(1 for r in loc if r.localization_success),
        "localization_measured_on": len(loc),
        "patch_generation_success": sum(1 for r in results if r.patch_generated),
        "final_status_breakdown": {s: sum(1 for r in results if r.final_status == s)
                                   for s in sorted({r.final_status for r in results})},
        "llm_failures": sum(1 for r in results if r.final_status == FinalStatus.LLM_FAILURE.value),
    }
