"""
LangGraph orchestration of the bounded repair workflow (Phase 3).

  START -> analyze_issue -> localize_bug -> retrieve_context -> generate_patch -> verify_patch
        -> verification_router -> human_review | localize_bug | diagnose_failure | diagnose_regression
                                  | generate_patch | END
  diagnose_* -> generate_patch;  human_review -> create_pr -> END (approve) | generate_patch | END

One model; nodes are capabilities. verify_patch calls RepairAgent._submit, i.e. the unchanged
CerberusGate.verify: the graph never decides admission. Budgets are RepairAgent._limit_reached plus
the same per-call tool limit, checked before every model and tool call. State is checkpointed in
SQLite (thread id = run id) so a failed or interrupted run resumes with `resume=True`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from harness.diff_utils import DiffUtils
from repair.agent import RepairAgent, _log, _safe
from repair.llm.provider import LLMError, Message, ModelResponse, ToolCall, ToolResult, estimate_cost
from repair.orchestration import prompts as P
from repair.orchestration.state import GraphRepairState, GraphState, NodeTransition
from repair.state import FinalStatus, GateFeedback, GateStatus, LLMCallRecord, ToolCallRecord
from repair.tools import SUBMIT_PATCH, Tool, ToolRegistry, default_tools
from repair.tools.base import REPRO_TEST_PATH, ToolError, check_writable

ANALYZE_TURNS, LOCALIZE_TURNS, DIAGNOSE_TURNS = 3, 6, 4
CONTEXT_CHARS = 8000
LOCALIZE_CODES = {"NOT_LOCALIZED", "LOCALIZATION_FAILURE", "SCOPE_VIOLATION"}
TEST_CODES = {"GREEN_NOT_REACHED", "GREEN_NONDETERMINISTIC", "REPRODUCTION_TEST_TAMPERED", "REGRESSION_UNVERIFIABLE"}
PATCH_CODES = {"INVALID_PATCH", "EMPTY_PATCH", "PATCH_APPLY_FAILED"}
Reviewer = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


# ------------------------------------------------------------- record tools

class _AnalysisIn(BaseModel):
    summary: str = Field(min_length=1, max_length=2000, description="Expected vs observed behaviour")
    search_terms: List[str] = Field(default_factory=list, description="Identifiers / strings to search for")


class _LocalizationIn(BaseModel):
    files: List[str] = Field(min_length=1, max_length=5, description="Source files to change (not tests)")
    rationale: str = Field("", max_length=2000)


class _DiagnosisIn(BaseModel):
    root_cause: str = Field(min_length=1, max_length=2000)
    plan: str = Field("", max_length=2000)


def _record_tool(name: str, desc: str, model: type) -> Tool:
    class _T(Tool):
        Input = model

        def run(self, ctx, args):
            return args.model_dump()
    t = _T()
    t.name, t.description = name, desc
    return t


RECORD_ANALYSIS = _record_tool("record_analysis", "Record the issue analysis (ends this step).", _AnalysisIn)
RECORD_LOCALIZATION = _record_tool("record_localization", "Record the files to change (ends this step).", _LocalizationIn)
RECORD_DIAGNOSIS = _record_tool("record_diagnosis", "Record the failure diagnosis (ends this step).", _DiagnosisIn)


def route_after_verification(fb: GateFeedback) -> str:
    """Pure routing on the gate's feedback (attempt/budget checks happen before this)."""
    if fb.status == GateStatus.ADMITTED:
        return "human_review"
    code = fb.failure_code or ""
    if fb.status == GateStatus.INVALID_PATCH or fb.patch_applied is False or code in PATCH_CODES:
        return "generate_patch"
    if code in LOCALIZE_CODES:
        return "localize_bug"
    if code == "REGRESSION":
        return "diagnose_regression"
    return "diagnose_failure"   # TEST_CODES, GATE_ERROR and any other refusal


class NodeFailure(RuntimeError):
    def __init__(self, status: FinalStatus, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ runtime

class GraphRunner:
    """Holds the live run (model, gate, sandbox, accounting) and builds the graph over it."""

    def __init__(self, agent: RepairAgent, checkpoint_db: str):
        self.agent, self.cfg, self.ctx = agent, agent.config, agent.ctx
        self.checkpoint_db = checkpoint_db
        self.state: GraphRepairState = None  # type: ignore[assignment]
        self.started = time.monotonic()
        self.patch_messages: Optional[List[Message]] = None
        self.pending: List[ToolResult] = []
        self.submit_index: Optional[int] = None
        retrieval = bool(self.cfg.retrieval)
        ro = {"search_code", "read_file", "find_symbol", "find_references", "read_test"} | ({"retrieve_code"} if retrieval else set())
        tools = default_tools(retrieval)
        self.reg_analyze = ToolRegistry(tools + [RECORD_ANALYSIS], allowed={"read_test", "search_code", "record_analysis"})
        self.reg_localize = ToolRegistry(tools + [RECORD_LOCALIZATION], allowed=ro | {"record_localization"})
        self.reg_diagnose = ToolRegistry(tools + [RECORD_DIAGNOSIS], allowed=ro | {"run_test", "git_diff", "record_diagnosis"})
        self.reg_patch = agent.registry

    # ---------------------------------------------------------- accounting

    def _call_model(self, node: str, system: str, messages: List[Message], specs) -> ModelResponse:
        """Same accounting as RepairAgent.run (tokens, cost, llm_calls, tracing)."""
        st, cfg, tracer = self.state, self.cfg, self.agent.tracer
        try:
            resp = self.agent.model.complete(system, messages, specs, cfg.max_output_tokens)
        except LLMError as exc:
            _safe(tracer.generation, "llm-call", st.model, 0, 0, None, 0.0,
                  {"iteration": st.current_iteration, "node": node}, error=str(exc))
            raise
        if cfg.input_price_per_mtok is not None and cfg.output_price_per_mtok is not None:
            cost = round(resp.usage.input_tokens * cfg.input_price_per_mtok / 1e6
                         + resp.usage.output_tokens * cfg.output_price_per_mtok / 1e6, 6)
        else:
            cost = estimate_cost(resp.model or st.model, resp.usage)
        st.input_tokens += resp.usage.input_tokens
        st.output_tokens += resp.usage.output_tokens
        st.total_tokens = st.input_tokens + st.output_tokens
        st.estimated_cost = None if cost is None or st.estimated_cost is None else round(st.estimated_cost + cost, 6)
        st.agent_messages += 1
        st.llm_calls.append(LLMCallRecord(
            iteration=st.current_iteration, model=resp.model or st.model, input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens, latency_seconds=round(resp.latency_seconds, 3), cost_usd=cost,
            stop_reason=resp.stop_reason, tool_calls=[c.name for c in resp.tool_calls]))
        _safe(tracer.generation, "llm-call", resp.model or st.model, resp.usage.input_tokens, resp.usage.output_tokens,
              cost, resp.latency_seconds, {"iteration": st.current_iteration, "node": node,
                                            "stop_reason": resp.stop_reason, "tool_calls": [c.name for c in resp.tool_calls]})
        return resp

    def _tool(self, call: ToolCall, registry: ToolRegistry) -> Tuple[ToolResult, Any]:
        st, cfg = self.state, self.cfg
        outcome = registry.execute(self.ctx, call.name, call.arguments)
        text = outcome.to_text(cfg.max_tool_output_chars)
        safe_args = {k: (v if not isinstance(v, str) or len(v) <= 200 else f"<{len(v)} chars>")
                     for k, v in (call.arguments or {}).items()}
        st.tool_calls.append(ToolCallRecord(iteration=st.current_iteration + 1, tool=call.name, arguments=safe_args,
                                            ok=outcome.ok, error_code=outcome.error_code,
                                            duration_seconds=outcome.duration_seconds, output_chars=len(text)))
        _safe(self.agent.tracer.tool, call.name, safe_args, outcome.ok, outcome.duration_seconds,
              {"error_code": outcome.error_code, "output_chars": len(text)})
        if call.name == "retrieve_code" and outcome.ok:
            _safe(self.agent.tracer.retrieval, str((call.arguments or {}).get("query", "")),
                  list(outcome.payload.get("files", [])), float(outcome.payload.get("latency_seconds", 0.0)),
                  {"retriever": outcome.payload.get("retriever")})
        self.agent.print(f"  tool {call.name}{'' if outcome.ok else ' -> ' + str(outcome.error_code)}")
        return ToolResult(call.id, text, is_error=not outcome.ok), outcome

    def _limit(self) -> Optional[FinalStatus]:
        return self.agent._limit_reached(self.state, self.started)

    @staticmethod
    def _end(status: FinalStatus, message: str) -> Dict[str, Any]:
        return {"route": END, "final_status": status.value, "final_message": message}

    def _record_loop(self, node: str, prompt: str, registry: ToolRegistry, record: str,
                     max_turns: int) -> Tuple[Optional[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
        """Bounded read-only sub-loop that ends when the model calls `record`. Returns (payload, text, terminal)."""
        msgs, specs, text = [Message("user", prompt)], registry.specs(), ""
        for turn in range(max_turns):
            limit = self._limit()
            if limit:
                return None, text, self._end(limit, f"Limit reached before the next model call: {limit.value}")
            resp = self._call_model(node, P.NODE_SYSTEM, msgs, specs)
            msgs.append(Message("assistant", text=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))
            text = resp.text or text
            if not resp.tool_calls:
                return None, text, None
            results, recorded = [], None
            for call in resp.tool_calls:
                if recorded is not None:
                    results.append(ToolResult(call.id, json.dumps({"skipped": "step ended"}), is_error=True))
                    continue
                if len(self.state.tool_calls) >= self.cfg.max_tool_calls:
                    return None, text, self._end(FinalStatus.TOOL_LIMIT_EXCEEDED, f"{self.cfg.max_tool_calls} tool calls used.")
                res, outcome = self._tool(call, registry)
                if call.name == record and outcome.ok:
                    recorded = outcome.payload
                results.append(res)
            if recorded is not None:
                return recorded, text, None
            hint = f"Call {record} now." if turn == max_turns - 2 else ""
            msgs.append(Message("user", text=hint, tool_results=results))
        return None, text, None

    # ------------------------------------------------------------- nodes

    def analyze_issue(self, s: GraphState) -> Dict[str, Any]:
        rec, text, end = self._record_loop("analyze_issue", P.analyze_prompt(s["task"], REPRO_TEST_PATH),
                                           self.reg_analyze, "record_analysis", ANALYZE_TURNS)
        if end:
            return end
        return {"route": "localize_bug", "analysis": (rec or {}).get("summary") or text or s["task"]["title"],
                "search_terms": (rec or {}).get("search_terms", [])}

    def localize_bug(self, s: GraphState) -> Dict[str, Any]:
        fb = s.get("last_feedback")
        allowed = list(s.get("allowed_files") or [])
        if fb and fb.get("allowed_files"):
            allowed = list(fb["allowed_files"])
        rec, _text, end = self._record_loop(
            "localize_bug", P.localize_prompt(s["task"], REPRO_TEST_PATH, s.get("analysis", ""), allowed, fb),
            self.reg_localize, "record_localization", LOCALIZE_TURNS)
        if end:
            return end
        files: List[str] = []
        for f in (rec or {}).get("files", []) or list(self.ctx.files_read):
            try:
                files.append(check_writable(f))
            except ToolError:
                continue
        if allowed:
            files = [f for f in files if f in allowed] or allowed
        files = list(dict.fromkeys(files))[:5]
        self.state.suspect_files = files
        return {"route": "retrieve_context", "suspect_files": files, "allowed_files": allowed}

    def retrieve_context(self, s: GraphState) -> Dict[str, Any]:
        """No model call: WorkspaceRetriever (when configured) plus the localized files, via the same tools."""
        chunks: List[str] = []
        calls: List[Tuple[ToolCall, ToolRegistry]] = []
        if self.cfg.retrieval:
            query = (s.get("analysis", "") + " " + " ".join(s.get("search_terms", [])))[:1000].strip() or s["task"]["title"]
            calls.append((ToolCall("ctx_retrieve", "retrieve_code", {"query": query, "k": 8}), self.reg_localize))
        for f in (s.get("suspect_files") or [])[:3]:
            calls.append((ToolCall(f"ctx_{f}", "read_file", {"path": f, "start_line": 1, "end_line": 200}), self.reg_localize))
        for call, reg in calls:
            if len(self.state.tool_calls) >= self.cfg.max_tool_calls:
                return self._end(FinalStatus.TOOL_LIMIT_EXCEEDED, f"{self.cfg.max_tool_calls} tool calls used.")
            _res, out = self._tool(call, reg)
            if not out.ok:
                continue
            if call.name == "retrieve_code":
                chunks += [f"{r['path']}:{r['start_line']}-{r['end_line']} {r.get('symbol') or ''}\n{r['snippet']}"
                           for r in out.payload.get("results", [])[:5]]
            else:
                chunks.append(f"{out.payload['path']} (lines {out.payload['start_line']}-{out.payload['end_line']})\n"
                              f"<<<REPOSITORY_DATA\n{out.payload['content']}\nREPOSITORY_DATA>>>")
        return {"route": "generate_patch", "context_text": "\n\n".join(chunks)[:CONTEXT_CHARS]}

    def generate_patch(self, s: GraphState) -> Dict[str, Any]:
        cfg, st = self.cfg, self.state
        if self.patch_messages is None:
            self.patch_messages = [Message("user", P.patch_prompt(dict(s), REPRO_TEST_PATH, cfg.max_attempts, cfg.max_tool_calls))]
        else:
            self.patch_messages.append(Message("user", text=P.revision_guidance(dict(s)) or P.NUDGE, tool_results=self.pending))
        self.pending, self.submit_index = [], None
        msgs, specs, text_only = self.patch_messages, self.reg_patch.specs(), 0
        while True:
            limit = self._limit()
            if limit:
                return self._end(limit, f"Limit reached before the next model call: {limit.value}")
            resp = self._call_model("generate_patch", P.NODE_SYSTEM, msgs, specs)
            msgs.append(Message("assistant", text=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))
            if not resp.tool_calls:
                text_only += 1
                if text_only >= cfg.max_text_only_turns:
                    return self._end(FinalStatus.GAVE_UP if st.attempts else FinalStatus.NO_PATCH, "The agent stopped calling tools.")
                msgs.append(Message("user", P.NUDGE))
                continue
            text_only = 0
            results: List[ToolResult] = []
            reasoning: Optional[str] = None
            for call in resp.tool_calls:
                if reasoning is not None:
                    results.append(ToolResult(call.id, json.dumps({"skipped": "submit_patch ends the turn"}), is_error=True))
                    continue
                if len(st.tool_calls) >= cfg.max_tool_calls:
                    return self._end(FinalStatus.TOOL_LIMIT_EXCEEDED, f"{cfg.max_tool_calls} tool calls used.")
                if call.name == SUBMIT_PATCH:
                    reasoning = str((call.arguments or {}).get("reasoning", ""))
                    st.tool_calls.append(ToolCallRecord(iteration=st.current_iteration + 1, tool=SUBMIT_PATCH,
                                                        arguments={"reasoning_chars": len(reasoning)}, ok=True,
                                                        duration_seconds=0.0, output_chars=0))
                    self.submit_index = len(results)
                    results.append(ToolResult(call.id, "", is_error=True))  # filled by verify_patch
                    continue
                results.append(self._tool(call, self.reg_patch)[0])
            if reasoning is not None:
                self.pending = results
                return {"route": "verify_patch", "pending_reasoning": reasoning}
            msgs.append(Message("user", tool_results=results))

    def verify_patch(self, s: GraphState) -> Dict[str, Any]:
        fb = self.agent._submit(self.state, s.get("pending_reasoning", ""))   # CerberusGate.verify (unchanged)
        payload = fb.for_model()
        payload["attempts_remaining"] = self.cfg.max_attempts - self.state.current_iteration
        if self.submit_index is not None and self.submit_index < len(self.pending):
            old = self.pending[self.submit_index]
            self.pending[self.submit_index] = ToolResult(old.tool_call_id, json.dumps(payload),
                                                         is_error=fb.status != GateStatus.ADMITTED)
        return {"route": "verification_router", "last_feedback": fb.model_dump(mode="json"), "diagnosis": ""}

    def verification_router(self, s: GraphState) -> Dict[str, Any]:
        fb = self.state.verification_result
        if fb is None:
            return self._end(FinalStatus.ERROR, "verification_router reached without a gate result.")
        if fb.status == GateStatus.ADMITTED:
            return {"route": "human_review"}
        if self.state.current_iteration >= self.cfg.max_attempts:
            return self._end(FinalStatus.MAX_ATTEMPTS_EXCEEDED,
                             f"No patch admitted in {self.cfg.max_attempts} attempt(s); last: {fb.failure_code}.")
        limit = self._limit()
        if limit:
            return self._end(limit, f"Limit reached after attempt {self.state.current_iteration}: {limit.value}")
        self.agent.print("  Graph retry: routing gate feedback")
        return {"route": route_after_verification(fb)}

    def _diagnose(self, s: GraphState, kind: str) -> Dict[str, Any]:
        fb = self.state.verification_result
        rec, text, end = self._record_loop(f"diagnose_{kind}", P.diagnose_prompt(kind, fb.for_model() if fb else {},
                                                                                 self.state.generated_patch),
                                           self.reg_diagnose, "record_diagnosis", DIAGNOSE_TURNS)
        if end:
            return end
        diag = (f"{rec['root_cause']} Plan: {rec.get('plan', '')}" if rec else text) or (fb.suggested_next_action if fb else "")
        return {"route": "generate_patch", "diagnosis": diag[:2000]}

    def diagnose_failure(self, s: GraphState) -> Dict[str, Any]:
        return self._diagnose(s, "failure")

    def diagnose_regression(self, s: GraphState) -> Dict[str, Any]:
        return self._diagnose(s, "regression")

    def _gate_admitted(self) -> bool:
        fb = self.state.verification_result
        if fb is None or fb.status != GateStatus.ADMITTED or not fb.run_json_path:
            return False
        try:
            with open(fb.run_json_path, encoding="utf-8") as f:
                return json.load(f).get("final_state") == "ADMITTED"
        except (OSError, ValueError):
            return False

    def human_review(self, s: GraphState) -> Dict[str, Any]:
        if not self._gate_admitted():   # a human decision can never override the gate
            return self._end(FinalStatus.ERROR, "human_review reached without a gate admission.")
        if not self.cfg.review:
            self.state.review_decision = "pending"
            return {**self._end(FinalStatus.ADMITTED, "Cerberus admitted the patch; awaiting human review (no PR created)."),
                    "review_decision": "pending"}
        fb = self.state.verification_result
        raw = interrupt({"run_id": self.state.run_id, "changed_files": self.state.changed_files,
                         "patch": self.state.generated_patch, "gate_run_json": fb.run_json_path if fb else None,
                         "options": ["approve", "request_changes", "reject"]})
        d = raw if isinstance(raw, dict) else {"decision": str(raw)}
        decision = str(d.get("decision", "reject")).lower()
        decision = decision if decision in ("approve", "request_changes", "reject") else "reject"
        comment = str(d.get("comment", ""))[:2000]
        self.state.review_decision, self.state.review_comment = decision, comment
        upd = {"review_decision": decision, "review_comment": comment}
        if decision == "approve":
            return {**upd, "route": "create_pr"}
        if decision == "request_changes" and self.state.current_iteration < self.cfg.max_attempts and not self._limit():
            return {**upd, "route": "generate_patch"}
        why = "rejected by review" if decision == "reject" else "changes requested but no attempts/budget left"
        return {**upd, **self._end(FinalStatus.ADMITTED, f"Cerberus admitted the patch; {why}; no PR created.")}

    def create_pr(self, s: GraphState) -> Dict[str, Any]:
        st = self.state
        if st.review_decision != "approve" or not self._gate_admitted():
            return self._end(FinalStatus.ERROR, "create_pr requires an approved, gate-admitted patch.")
        fb = st.verification_result
        out = os.path.join(self.agent.out_dir or ".", "pr")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "patch.diff"), "w", encoding="utf-8", newline="\n") as f:
            f.write(st.generated_patch)
        reasoning = st.attempts[-1].reasoning if st.attempts else ""
        body = (f"# Fix {st.issue_id}: {st.issue_title}\n\n{reasoning}\n\n## Verification\n\n"
                f"Admitted by Cerberus (gate run `{fb.gate_run_id}`, attempt {fb.attempt}): RED x3, BASELINE, GREEN x3, "
                f"REGRESSION, SCOPE, ADMISSION.\n\nChanged files: {', '.join(st.changed_files)}\n\n"
                f"Approved by human review{': ' + st.review_comment if st.review_comment else ''}.\n\n"
                f"_Dry run: not published._\n")
        with open(os.path.join(out, "PR_BODY.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        with open(os.path.join(out, "pr.json"), "w", encoding="utf-8") as f:
            json.dump({"dry_run": True, "title": f"Fix {st.issue_id}: {st.issue_title}", "base_commit": st.commit_sha,
                       "changed_files": st.changed_files, "gate_run_id": fb.gate_run_id,
                       "gate_run_json": fb.run_json_path, "run_id": st.run_id}, f, indent=2)
        st.pr_artifact = out
        self.agent.print(f"  PR (dry run) written to {out}")
        return self._end(FinalStatus.ADMITTED, "Cerberus admitted the patch; approved; PR written (dry run).")

    # ------------------------------------------------------------ wiring

    def _snapshot(self) -> Dict[str, Any]:
        return {"record": self.state.model_dump(mode="json"),
                "workspace_diff": DiffUtils.workspace_changes(self.agent.sandbox).diff_text,
                "tool_ctx": {"files_read": list(self.ctx.files_read), "files_edited": list(self.ctx.files_edited),
                             "tests_seen": list(self.ctx.tests_seen), "reproduction_runs": list(self.ctx.reproduction_runs)}}

    def _transition(self, name: str, nxt: str, t0: float, started_at: str, note: str = "") -> None:
        st = self.state
        st.node_history.append(NodeTransition(node=name, next=nxt, started_at=started_at,
                                              duration_seconds=round(time.monotonic() - t0, 3), attempt=st.current_iteration,
                                              total_tokens=st.total_tokens, tool_calls=len(st.tool_calls), note=note))
        _log(st, "node", node=name, next=nxt)
        self.agent.print(f"[graph] {name} -> {nxt}")

    def _wrap(self, name: str, fn: Callable[[GraphState], Dict[str, Any]]):
        def node(s: GraphState) -> Dict[str, Any]:
            tracer = self.agent.tracer
            span = tracer.node(name, {"attempt": self.state.current_iteration}) if hasattr(tracer, "node") else None
            t0, started_at = time.monotonic(), _now()
            try:
                update = fn(s) or {}
            except GraphBubbleUp:   # human_review interrupt: pause, checkpoint, resume later
                self._transition(name, "interrupt", t0, started_at, "awaiting human decision")
                if span:
                    _safe(span.end, next="interrupt")
                raise
            except Exception as exc:
                status = FinalStatus.LLM_FAILURE if isinstance(exc, LLMError) else FinalStatus.ERROR
                msg = str(exc)[:500] if isinstance(exc, LLMError) else f"{type(exc).__name__}: {str(exc)[:500]}"
                self._transition(name, "failed", t0, started_at, msg[:200])
                if span:
                    _safe(span.end, next="failed", error=msg[:200])
                raise NodeFailure(status, msg) from exc
            nxt = str(update.get("route", END))
            self._transition(name, nxt, t0, started_at)
            if span:
                _safe(span.end, next=nxt, final_status=update.get("final_status"))
            update.update(self._snapshot())
            return update
        return node

    def build(self, checkpointer: Any):
        g = StateGraph(GraphState)
        edges = {
            "analyze_issue": ["localize_bug"], "localize_bug": ["retrieve_context"],
            "retrieve_context": ["generate_patch"], "generate_patch": ["verify_patch"],
            "verify_patch": ["verification_router"],
            "verification_router": ["human_review", "localize_bug", "diagnose_failure", "diagnose_regression", "generate_patch"],
            "diagnose_failure": ["generate_patch"], "diagnose_regression": ["generate_patch"],
            "human_review": ["create_pr", "generate_patch"], "create_pr": [],
        }
        for name in edges:
            g.add_node(name, self._wrap(name, getattr(self, name)))
        g.add_edge(START, "analyze_issue")
        for name, targets in edges.items():
            g.add_conditional_edges(name, lambda s: s.get("route", END), {**{t: t for t in targets}, END: END})
        return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------- entry

def open_checkpointer(path: str) -> Tuple[Any, Optional[sqlite3.Connection]]:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:   # langgraph-checkpoint-sqlite missing: in-process only, not resumable
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn), conn


def checkpointed_task(run_id: str, checkpoint_db: str) -> Optional[Dict[str, Any]]:
    """The task stored with a run's checkpoints (for `agent resume RUN_ID`)."""
    saver, conn = open_checkpointer(checkpoint_db)
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": run_id}})
        return (tup.checkpoint.get("channel_values", {}) or {}).get("task") if tup else None
    finally:
        if conn:
            conn.close()


def run_graph(agent: RepairAgent, state, task: Dict[str, Any], checkpoint_db: str, resume: bool = False,
              decision: Optional[Dict[str, Any]] = None, reviewer: Optional[Reviewer] = None) -> GraphRepairState:
    from repair.orchestration.prompts import GRAPH_PROMPT_VERSION
    from repair.prompts import PROMPT_VERSION

    runner = GraphRunner(agent, checkpoint_db)
    saver, conn = open_checkpointer(checkpoint_db)
    cfg = {"configurable": {"thread_id": state.run_id}, "recursion_limit": 40 + 12 * agent.config.max_attempts}
    graph = runner.build(saver)
    st = GraphRepairState(**state.model_dump())
    inp: Any = {"run_id": state.run_id, "task": task}
    if resume:
        snap = graph.get_state(cfg)
        if not snap.values:
            raise ValueError(f"No checkpoint for run {state.run_id} in {checkpoint_db}")
        rec = GraphRepairState(**snap.values["record"]) if snap.values.get("record") else st
        saved = os.path.join(agent.out_dir or "", "agent_run.json")
        if os.path.exists(saved):   # the failed segment's record may hold spend the checkpoint does not
            with open(saved, encoding="utf-8") as f:
                prev = GraphRepairState(**json.load(f))
            if len(prev.llm_calls) + len(prev.tool_calls) > len(rec.llm_calls) + len(rec.tool_calls):
                rec = prev
        st = rec
        st.resumed += 1
        st.final_status, st.final_message = None, ""
        diff = snap.values.get("workspace_diff") or ""
        if diff and not DiffUtils.apply_diff_to_sandbox(agent.sandbox, diff).success:
            raise RuntimeError("Could not restore the checkpointed workspace diff.")
        for k, v in (snap.values.get("tool_ctx") or {}).items():
            getattr(runner.ctx, k).extend(v)
        inp = Command(resume=decision) if (snap.interrupts and decision) else None
        if not snap.next:
            inp = "done"
    runner.state = st
    st.max_iterations = agent.config.max_attempts
    st.model = getattr(agent.model, "model", agent.config.model)
    runner.started = time.monotonic() - st.duration_seconds   # wall time accumulates across resumes
    _safe(agent.tracer.start_run, st.run_id, {
        "run_id": st.run_id, "repository": st.repository, "commit_sha": st.commit_sha, "issue_id": st.issue_id,
        "issue_title": st.issue_title, "model": st.model, "prompt_version": PROMPT_VERSION,
        "graph_prompt_version": GRAPH_PROMPT_VERSION, "orchestrator": "langgraph", "max_attempts": agent.config.max_attempts,
        "resumed": st.resumed})
    st.trace_id = getattr(agent.tracer, "trace_id", None) or st.trace_id
    agent.print(f"Run ID: {st.run_id}\nModel: {st.model}\nOrchestrator: langgraph{' (resumed)' if resume else ''}")
    status: Optional[FinalStatus] = None
    message = ""
    try:
        if inp != "done":
            graph.invoke(inp, cfg)
            while reviewer is not None:
                snap = graph.get_state(cfg)
                if not snap.interrupts:
                    break
                d = reviewer(snap.interrupts[0].value)
                if d is None:
                    break
                graph.invoke(Command(resume=d), cfg)
        snap = graph.get_state(cfg)
        vals = snap.values
        if snap.interrupts:
            status = FinalStatus.ADMITTED
            message = f"Cerberus admitted the patch; awaiting human review (resume: agent resume {st.run_id} --decision ...)."
            st.review_decision = "pending"
        else:
            status = FinalStatus(vals.get("final_status") or "ERROR")
            message = vals.get("final_message", "")
    except NodeFailure as exc:
        status, message = exc.status, exc.message + f" (resumable: agent resume {st.run_id})"
    except Exception as exc:
        status, message = FinalStatus.ERROR, f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        if conn:
            conn.close()
    # Invariant: ADMITTED only with a gate run.json that says ADMITTED.
    if status == FinalStatus.ADMITTED and not runner._gate_admitted():
        status, message = FinalStatus.ERROR, "Graph ended ADMITTED without a gate admission; refused."
    if agent.ctx._retriever is not None:
        _safe(agent.ctx._retriever.close)
    st.final_status, st.final_message = status, message
    st.candidate_files = list(dict.fromkeys(agent.ctx.files_read + agent.ctx.files_edited))
    st.relevant_tests = list(agent.ctx.tests_seen)
    st.completed_at = _now()
    st.duration_seconds = round(time.monotonic() - runner.started, 2)
    _safe(agent.tracer.end_run, status.value, {
        "final_status": status.value, "attempts": st.current_iteration, "tool_calls": len(st.tool_calls),
        "input_tokens": st.input_tokens, "output_tokens": st.output_tokens, "estimated_cost": st.estimated_cost,
        "duration_seconds": st.duration_seconds, "failure_code": st.failure_code, "nodes": len(st.node_history)})
    _log(st, "run_end", final_status=status.value, failure_code=st.failure_code)
    agent.print(f"\nFinal status: {status.value}" + (f" ({message})" if message else ""))
    agent.print(f"Attempts: {st.current_iteration} | Tool calls: {len(st.tool_calls)} | Nodes: {len(st.node_history)} | "
                f"Tokens: {st.total_tokens} | Cost: {'n/a' if st.estimated_cost is None else f'${st.estimated_cost:.4f}'}")
    if agent.out_dir:
        os.makedirs(agent.out_dir, exist_ok=True)
        with open(os.path.join(agent.out_dir, "agent_run.json"), "w", encoding="utf-8") as f:
            f.write(st.model_dump_json(indent=2))
    return st
