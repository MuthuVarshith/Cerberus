"""
The repair agent: one model, controlled tools, a bounded loop, and the existing gate as judge.

    analyze -> search/read/test (tools) -> edit -> submit_patch -> Cerberus gate
        ADMITTED                 -> stop
        REFUSED / INVALID_PATCH  -> structured feedback -> revise -> submit again (bounded)

Every limit (attempts, tool calls, tokens, cost, wall time) ends the run with a
machine-readable FinalStatus. The run ends ADMITTED only when a gate run.json says so.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from harness.diff_utils import DiffUtils
from repair.config import RepairConfig
from repair.llm.provider import ChatModel, LLMError, Message, ToolResult, estimate_cost
from repair.observability.langfuse import NoopTracer, Tracer
from repair.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_prompt
from repair.state import (
    AttemptRecord, FinalStatus, GateFeedback, GateStatus, LLMCallRecord, RepairState, ToolCallRecord,
)
from repair.tools import SUBMIT_PATCH, ToolContext, ToolRegistry, build_registry
from repair.tools.base import REPRO_TEST_PATH, ToolError, check_writable
from repair.verification.adapter import CerberusGate

logger = logging.getLogger("cerberus.repair")

Printer = Callable[[str], None]


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Tracing must never change the outcome of a repair run."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - depends on the tracing backend
        logger.warning("tracer error: %s", exc)


def _log(state: RepairState, event: str, **fields: Any) -> None:
    logger.info(json.dumps({
        "event": event, "run_id": state.run_id, "repository": state.repository, "commit_sha": state.commit_sha,
        "issue_id": state.issue_id, "agent_iteration": state.current_iteration, "model": state.model,
        "total_tokens": state.total_tokens, "cost": state.estimated_cost,
        "timestamp": datetime.now(timezone.utc).isoformat(), **fields,
    }, default=str))


class RepairAgent:
    def __init__(self, model: ChatModel, gate: CerberusGate, sandbox: Any, config: RepairConfig,
                 tracer: Optional[Tracer] = None, registry: Optional[ToolRegistry] = None,
                 printer: Optional[Printer] = None, out_dir: Optional[str] = None):
        self.model = model
        self.gate = gate
        self.sandbox = sandbox
        self.config = config
        self.tracer = tracer or NoopTracer()
        self.registry = registry or build_registry()
        self.print = printer or (lambda _line: None)
        self.out_dir = out_dir
        self.ctx = ToolContext(sandbox=sandbox, timeout_seconds=config.tool_timeout_seconds,
                               max_output_chars=config.max_tool_output_chars)

    # ------------------------------------------------------------------ limits

    def _limit_reached(self, state: RepairState, started: float) -> Optional[FinalStatus]:
        if time.monotonic() - started > self.config.max_run_seconds:
            return FinalStatus.TIMEOUT
        if state.total_tokens >= self.config.max_agent_tokens:
            return FinalStatus.TOKEN_LIMIT_EXCEEDED
        if state.estimated_cost is not None and state.estimated_cost >= self.config.max_cost_per_task:
            return FinalStatus.COST_LIMIT_EXCEEDED
        if len(state.tool_calls) >= self.config.max_tool_calls:
            return FinalStatus.TOOL_LIMIT_EXCEEDED
        return None

    # ------------------------------------------------------------------ submit

    def _submit(self, state: RepairState, reasoning: str) -> GateFeedback:
        state.current_iteration += 1
        attempt = state.current_iteration
        self.print(f"\nAttempt {attempt}/{state.max_iterations}: submitting patch to Cerberus")
        changes = DiffUtils.workspace_changes(self.sandbox)
        problem = ""
        if changes.is_empty:
            problem = "The workspace has no changes; edit the source before submitting."
        else:
            try:
                for f in changes.files:
                    check_writable(f)
            except ToolError as exc:
                problem = exc.message
        if problem:
            fb = GateFeedback(attempt=attempt, status=GateStatus.INVALID_PATCH, failure_code="INVALID_PATCH",
                              message=problem, changed_files=changes.files,
                              suggested_next_action="Fix the workspace (git_status, reset_workspace, edit_file) and submit again.")
        else:
            self.print(f"  Patch: {', '.join(changes.files)} (+{changes.lines_added}/-{changes.lines_deleted})")
            self.print("  Cerberus verification: running gate (RED, BASELINE, GREEN, REGRESSION, SCOPE, ADMISSION)...")
            fb = self.gate.verify(changes.diff_text, attempt, run_id=f"{state.run_id}-g{attempt}")
        agent_tested = self.ctx.reproduction_runs[-1] if self.ctx.reproduction_runs else None
        state.attempts.append(AttemptRecord(
            attempt=attempt, diff=changes.diff_text, changed_files=changes.files,
            lines_added=changes.lines_added, lines_deleted=changes.lines_deleted,
            reasoning=reasoning[:2000], agent_tested_reproduction=agent_tested, feedback=fb,
        ))
        state.generated_patch = changes.diff_text
        state.changed_files = changes.files
        state.verification_result = fb
        state.verification_status = fb.status
        state.failure_code = fb.failure_code
        state.failure_feedback = fb.message
        verdict = "PASS" if fb.status == GateStatus.ADMITTED else "FAIL"
        self.print(f"  Cerberus verdict: {verdict} ({fb.status.value}{', ' + fb.stage if fb.stage else ''}"
                   f"{', ' + fb.failure_code if fb.failure_code else ''})")
        if fb.failed_tests:
            self.print(f"  Failing tests: {', '.join(fb.failed_tests[:5])}")
        _safe(self.tracer.verification, attempt, fb.status.value, fb.failure_code, fb.duration_seconds,
              {"stage": fb.stage, "changed_files": fb.changed_files, "lines_added": changes.lines_added,
               "lines_deleted": changes.lines_deleted, "gate_run_id": fb.gate_run_id})
        _log(state, "verification", verification_status=fb.status.value, failure_code=fb.failure_code)
        return fb

    # -------------------------------------------------------------------- run

    def run(self, state: RepairState, reproduction_test: str) -> RepairState:
        cfg = self.config
        state.max_iterations = cfg.max_attempts
        state.model = getattr(self.model, "model", cfg.model)
        started = time.monotonic()
        _safe(self.tracer.start_run, state.run_id, {
            "run_id": state.run_id, "repository": state.repository, "commit_sha": state.commit_sha,
            "issue_id": state.issue_id, "issue_title": state.issue_title, "model": state.model,
            "prompt_version": PROMPT_VERSION, "max_attempts": cfg.max_attempts,
        })
        state.trace_id = getattr(self.tracer, "trace_id", None)
        self.print(f"Run ID: {state.run_id}\nModel: {state.model}\nIssue: {state.issue_id} {state.issue_title}")

        messages: List[Message] = [Message("user", task_prompt(
            state.issue_id, state.issue_title, state.issue_description, reproduction_test, REPRO_TEST_PATH,
            cfg.max_attempts, cfg.max_tool_calls))]
        specs = self.registry.specs()
        text_only_turns = 0
        status: Optional[FinalStatus] = None
        message = ""
        try:
            while status is None:
                status = self._limit_reached(state, started)
                if status:
                    message = f"Limit reached before the next model call: {status.value}"
                    break
                try:
                    resp = self.model.complete(SYSTEM_PROMPT, messages, specs, cfg.max_output_tokens)
                except LLMError as exc:
                    _safe(self.tracer.generation, "llm-call", state.model, 0, 0, None, 0.0,
                          {"iteration": state.current_iteration}, error=str(exc))
                    status, message = FinalStatus.LLM_FAILURE, str(exc)[:500]
                    break
                cost = estimate_cost(resp.model or state.model, resp.usage)
                state.input_tokens += resp.usage.input_tokens
                state.output_tokens += resp.usage.output_tokens
                state.total_tokens = state.input_tokens + state.output_tokens
                if cost is None or state.estimated_cost is None:
                    state.estimated_cost = None
                else:
                    state.estimated_cost = round(state.estimated_cost + cost, 6)
                state.agent_messages += 1
                state.llm_calls.append(LLMCallRecord(
                    iteration=state.current_iteration, model=resp.model or state.model,
                    input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                    latency_seconds=round(resp.latency_seconds, 3), cost_usd=cost, stop_reason=resp.stop_reason,
                    tool_calls=[c.name for c in resp.tool_calls],
                ))
                _safe(self.tracer.generation, "llm-call", resp.model or state.model, resp.usage.input_tokens,
                      resp.usage.output_tokens, cost, resp.latency_seconds,
                      {"iteration": state.current_iteration, "stop_reason": resp.stop_reason,
                       "tool_calls": [c.name for c in resp.tool_calls]})
                messages.append(Message("assistant", text=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))

                if not resp.tool_calls:
                    text_only_turns += 1
                    if text_only_turns >= cfg.max_text_only_turns:
                        status = FinalStatus.GAVE_UP if state.attempts else FinalStatus.NO_PATCH
                        message = "The agent stopped calling tools."
                        break
                    messages.append(Message("user", "Continue with tool calls. Only Cerberus decides success: "
                                                    "call submit_patch when your fix is ready."))
                    continue
                text_only_turns = 0

                results: List[ToolResult] = []
                for call in resp.tool_calls:
                    if status is not None:
                        results.append(ToolResult(call.id, json.dumps({"skipped": "run ended"}), is_error=True))
                        continue
                    if len(state.tool_calls) >= cfg.max_tool_calls:
                        status, message = FinalStatus.TOOL_LIMIT_EXCEEDED, f"{cfg.max_tool_calls} tool calls used."
                        results.append(ToolResult(call.id, json.dumps({"error": "TOOL_LIMIT_EXCEEDED"}), is_error=True))
                        continue
                    if call.name == SUBMIT_PATCH:
                        reasoning = str((call.arguments or {}).get("reasoning", ""))
                        state.tool_calls.append(ToolCallRecord(
                            iteration=state.current_iteration + 1, tool=SUBMIT_PATCH, arguments={"reasoning_chars": len(reasoning)},
                            ok=True, duration_seconds=0.0, output_chars=0))
                        fb = self._submit(state, reasoning)
                        if fb.status == GateStatus.ADMITTED:
                            status, message = FinalStatus.ADMITTED, "Cerberus admitted the patch."
                            results.append(ToolResult(call.id, json.dumps(fb.for_model())))
                        elif state.current_iteration >= cfg.max_attempts:
                            status = FinalStatus.MAX_ATTEMPTS_EXCEEDED
                            message = f"No patch admitted in {cfg.max_attempts} attempt(s); last: {fb.failure_code}."
                            results.append(ToolResult(call.id, json.dumps(fb.for_model()), is_error=True))
                        else:
                            payload = fb.for_model()
                            payload["attempts_remaining"] = cfg.max_attempts - state.current_iteration
                            self.print("  Agent retry: feedback returned to the agent")
                            results.append(ToolResult(call.id, json.dumps(payload), is_error=True))
                        continue

                    outcome = self.registry.execute(self.ctx, call.name, call.arguments)
                    text = outcome.to_text(cfg.max_tool_output_chars)
                    safe_args = {k: (v if not isinstance(v, str) or len(v) <= 200 else f"<{len(v)} chars>")
                                 for k, v in (call.arguments or {}).items()}
                    state.tool_calls.append(ToolCallRecord(
                        iteration=state.current_iteration + 1, tool=call.name, arguments=safe_args, ok=outcome.ok,
                        error_code=outcome.error_code, duration_seconds=outcome.duration_seconds, output_chars=len(text)))
                    _safe(self.tracer.tool, call.name, safe_args, outcome.ok, outcome.duration_seconds,
                          {"error_code": outcome.error_code, "output_chars": len(text)})
                    _log(state, "tool", tool_name=call.name, tool_duration=outcome.duration_seconds,
                         ok=outcome.ok, failure_code=outcome.error_code)
                    self.print(f"  tool {call.name}{'' if outcome.ok else ' -> ' + str(outcome.error_code)}")
                    results.append(ToolResult(call.id, text, is_error=not outcome.ok))
                messages.append(Message("user", tool_results=results))
        except Exception as exc:
            status, message = FinalStatus.ERROR, f"{type(exc).__name__}: {str(exc)[:500]}"
            logger.exception("repair run failed")

        state.final_status = status
        state.final_message = message
        state.candidate_files = list(dict.fromkeys(self.ctx.files_read + self.ctx.files_edited))
        state.relevant_tests = list(self.ctx.tests_seen)
        state.completed_at = datetime.now(timezone.utc).isoformat()
        state.duration_seconds = round(time.monotonic() - started, 2)
        _safe(self.tracer.end_run, status.value, {
            "final_status": status.value, "attempts": state.current_iteration, "tool_calls": len(state.tool_calls),
            "input_tokens": state.input_tokens, "output_tokens": state.output_tokens,
            "estimated_cost": state.estimated_cost, "duration_seconds": state.duration_seconds,
            "failure_code": state.failure_code,
        })
        _log(state, "run_end", final_status=status.value, failure_code=state.failure_code)
        self.print(f"\nFinal status: {status.value}" + (f" ({message})" if message and status != FinalStatus.ADMITTED else ""))
        self.print(f"Attempts: {state.current_iteration} | Tool calls: {len(state.tool_calls)} | "
                   f"Tokens: {state.total_tokens} | Cost: {'n/a' if state.estimated_cost is None else f'${state.estimated_cost:.4f}'}")
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(os.path.join(self.out_dir, "agent_run.json"), "w", encoding="utf-8") as f:
                f.write(state.model_dump_json(indent=2))
        return state
