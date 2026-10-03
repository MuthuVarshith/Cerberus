"""Repair agent: tools, policy, limits, feedback, metrics, tracing. Fake model only; no network, no cost."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from harness.docker_sandbox import ISOLATION_HOST_UNSAFE, Sandbox
from repair.agent import RepairAgent
from repair.llm.anthropic import AnthropicChatModel
from repair.llm.fake import FakeChatModel, call, respond
from repair.llm.provider import LLMError, Message, ToolCall, ToolResult, ToolSpec, Usage, estimate_cost
from repair.metrics import NOT_MEASURED, aggregate, task_result_from_state
from repair.observability.langfuse import LangfuseTracer, RecordingTracer, redact
from repair.state import FinalStatus, GateFeedback, GateStatus, RepairState
from repair.tools import build_registry
from repair.tools.base import (
    PERMISSION_DENIED, REPRO_TEST_PATH, TOOL_TIMEOUT, ToolContext, ToolError, check_readable, check_writable,
)
from repair.verification.adapter import feedback_from_run_json
from tests import repair_fixtures as fx


@pytest.fixture
def sandbox(tmp_path):
    repo = fx.make_repo(str(tmp_path))
    sb = Sandbox(base_dir=repo, isolation=ISOLATION_HOST_UNSAFE, timeout_sec=60)
    sb.ensure_harness_dir()
    sb.write_file(REPRO_TEST_PATH, fx.REPRO)
    yield sb
    sb.destroy()


@pytest.fixture
def ctx(sandbox):
    return ToolContext(sandbox=sandbox, timeout_seconds=30)


class NeverGate:
    """Fails the test if the agent reaches the gate when it must not."""
    def verify(self, diff, attempt, run_id):
        raise AssertionError("the gate must not be called")


class ScriptedGate:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.diffs = []

    def verify(self, diff, attempt, run_id):
        self.diffs.append(diff)
        status, code = self.verdicts.pop(0)
        return GateFeedback(attempt=attempt, status=status, failure_code=code, stage=code, message=f"{code}",
                            failed_tests=["tests/test_rates.py::test_percent_negative"] if code == "REGRESSION" else [])


def _state():
    return RepairState(run_id="r1", repository="calcrepo", issue_id="#1", issue_title=fx.TITLE, issue_description=fx.BODY)


# ---------------------------------------------------------------- path policy

@pytest.mark.parametrize("path", ["/etc/passwd", "C:/Windows/win.ini", "~/.ssh/id_rsa", "../outside.py",
                                  "calc/../../x.py", ".git/config", ".env", "deploy/server.pem", ".github/workflows/ci.yml"])
def test_read_policy_refuses_host_secret_and_traversal_paths(path):
    with pytest.raises(ToolError) as exc:
        check_readable(path)
    assert exc.value.code == PERMISSION_DENIED


def test_reproduction_test_is_readable_but_not_writable():
    assert check_readable(REPRO_TEST_PATH) == REPRO_TEST_PATH
    with pytest.raises(ToolError):
        check_writable(REPRO_TEST_PATH)


@pytest.mark.parametrize("path", ["tests/test_rates.py", "conftest.py", ".cerberus.yml", ".cerberus/x.py", ".git/HEAD"])
def test_write_policy_refuses_tests_and_infrastructure(path):
    with pytest.raises(ToolError) as exc:
        check_writable(path)
    assert exc.value.code == PERMISSION_DENIED


# ---------------------------------------------------------------- tools

def test_search_read_symbol_references_and_tests(ctx):
    reg = build_registry()
    s = reg.execute(ctx, "search_code", {"query": "percent"})
    assert s.ok and any(m["file"] == "calc/rates.py" and m["symbol"] == "percent" for m in s.payload["matches"])
    r = reg.execute(ctx, "read_file", {"path": "calc/rates.py", "start_line": 1, "end_line": 2})
    assert r.ok and "return part / whole" in r.payload["content"] and r.payload["end_line"] == 2
    f = reg.execute(ctx, "find_symbol", {"name": "percent"})
    assert f.ok and f.payload["definitions"][0]["file"] == "calc/rates.py"
    m = reg.execute(ctx, "find_symbol", {"name": "calc.rates", "kind": "module"})
    assert m.ok and m.payload["definitions"][0]["file"] == "calc/rates.py"
    refs = reg.execute(ctx, "find_references", {"name": "percent"})
    assert refs.ok and "tests/test_rates.py" in refs.payload["files"]
    assert any(x["is_definition"] for x in refs.payload["references"])
    t = reg.execute(ctx, "read_test", {})
    assert "tests/test_rates.py" in t.payload["test_files"]
    t2 = reg.execute(ctx, "read_test", {"path": REPRO_TEST_PATH})
    assert t2.payload["tests"] == ["test_percent_zero_whole_returns_zero"]


def test_read_file_rejects_traversal_with_structured_error(ctx):
    out = build_registry().execute(ctx, "read_file", {"path": "../../etc/passwd"})
    assert not out.ok and out.error_code == PERMISSION_DENIED


def test_run_test_executes_in_sandbox_and_reports_status(ctx):
    reg = build_registry()
    before = reg.execute(ctx, "run_test", {"target": REPRO_TEST_PATH})
    assert before.ok and before.payload["status"] == "FAILED"
    assert reg.execute(ctx, "edit_file", {"path": "calc/rates.py", "old_text": fx.OLD, "new_text": fx.GOOD}).ok
    after = reg.execute(ctx, "run_test", {"target": REPRO_TEST_PATH})
    assert after.payload["status"] == "PASSED"
    assert ctx.reproduction_runs == [False, True]


@pytest.mark.parametrize("target", ["tests/test_rates.py; rm -rf /", "tests/test_rates.py && whoami", "$(id).py",
                                    "tests/test_rates.py|cat", "calc/rates.py"])
def test_run_test_refuses_injection_and_non_test_targets(ctx, target):
    out = build_registry().execute(ctx, "run_test", {"target": target})
    assert not out.ok and out.error_code in ("INVALID_ARGUMENT", PERMISSION_DENIED)


def test_tool_timeout_is_structured(sandbox, tmp_path):
    sandbox.write_file("tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    ctx = ToolContext(sandbox=sandbox, timeout_seconds=5)
    out = build_registry().execute(ctx, "run_test", {"target": "tests/test_slow.py", "timeout_seconds": 5})
    assert not out.ok and out.error_code == TOOL_TIMEOUT


def test_edit_and_apply_patch_refuse_tests(ctx):
    reg = build_registry()
    e = reg.execute(ctx, "edit_file", {"path": "tests/test_rates.py", "old_text": "25.0", "new_text": "0"})
    assert not e.ok and e.error_code == PERMISSION_DENIED
    diff = ("--- a/tests/test_rates.py\n+++ b/tests/test_rates.py\n@@ -1 +1 @@\n"
            "-from calc.rates import percent\n+from calc.rates import percent  # weakened\n")
    p = reg.execute(ctx, "apply_patch", {"diff": diff})
    assert not p.ok and p.error_code == PERMISSION_DENIED


def test_git_tools_and_reset(ctx):
    reg = build_registry()
    reg.execute(ctx, "edit_file", {"path": "calc/rates.py", "old_text": fx.OLD, "new_text": fx.GOOD})
    st = reg.execute(ctx, "git_status", {})
    assert st.payload["changed_files"] == ["calc/rates.py"]
    d = reg.execute(ctx, "git_diff", {})
    assert "+    if whole == 0:" in d.payload["diff"]
    assert reg.execute(ctx, "reset_workspace", {}).ok
    assert reg.execute(ctx, "git_status", {}).payload["changed_files"] == []
    assert ctx.sandbox.read_file(REPRO_TEST_PATH) == fx.REPRO  # reset keeps the reproduction test


def test_unavailable_tool_is_permission_denied(ctx):
    reg = build_registry(allowed={"read_file"})
    assert [s.name for s in reg.specs()] == ["read_file"]
    out = reg.execute(ctx, "run_test", {"target": REPRO_TEST_PATH})
    assert not out.ok and out.error_code == PERMISSION_DENIED
    assert reg.execute(ctx, "shell", {"cmd": "id"}).error_code == PERMISSION_DENIED


def test_tool_schemas_are_json_schema_objects():
    for spec in build_registry().specs():
        assert spec.input_schema["type"] == "object"
        json.dumps(spec.input_schema)


# ---------------------------------------------------------------- agent loop (scripted gate)

def _agent(sandbox, model, gate, **cfg):
    return RepairAgent(model, gate, sandbox, fx.config(**cfg), tracer=RecordingTracer())


def test_invalid_patch_never_reaches_the_gate(sandbox):
    model = FakeChatModel([respond(call("submit_patch", reasoning="nothing changed")),
                           respond(text="done")] + [respond(text="done")] * 3)
    st = _agent(sandbox, model, NeverGate(), max_attempts=1).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.MAX_ATTEMPTS_EXCEEDED
    assert st.attempts[0].feedback.status == GateStatus.INVALID_PATCH


def test_agent_stops_at_max_attempts(sandbox):
    edit = call("edit_file", path="calc/rates.py", old_text=fx.OLD, new_text=fx.NO_FIX)
    model = FakeChatModel([respond(edit)] + [respond(call("submit_patch", reasoning="try"))] * 5)
    gate = ScriptedGate([(GateStatus.REFUSED, "GREEN_NOT_REACHED")] * 3)
    st = _agent(sandbox, model, gate, max_attempts=3).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.MAX_ATTEMPTS_EXCEEDED
    assert st.current_iteration == 3 and len(gate.diffs) == 3 and model.calls == 4


def test_llm_failure_is_recorded(sandbox):
    st = _agent(sandbox, FakeChatModel([RuntimeError("503 overloaded")]), NeverGate()).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.LLM_FAILURE and "503" in st.final_message


def test_cost_limit_stops_before_the_next_call(sandbox):
    expensive = respond(call("search_code", query="percent"), input_tokens=300_000, output_tokens=10_000)
    model = FakeChatModel([expensive, expensive], model="claude-sonnet-5-5")
    st = _agent(sandbox, model, NeverGate(), max_cost_per_task=0.5).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.COST_LIMIT_EXCEEDED
    assert model.calls == 1 and st.estimated_cost == pytest.approx(0.7)


def test_token_limit_stops_the_run(sandbox):
    model = FakeChatModel([respond(call("search_code", query="percent"), input_tokens=900, output_tokens=200)] * 3)
    st = _agent(sandbox, model, NeverGate(), max_agent_tokens=1000).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.TOKEN_LIMIT_EXCEEDED and model.calls == 1


def test_tool_call_limit_stops_the_run(sandbox):
    model = FakeChatModel([respond(*[call("search_code", query="percent")] * 4)] * 3)
    st = _agent(sandbox, model, NeverGate(), max_tool_calls=3).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.TOOL_LIMIT_EXCEEDED and len(st.tool_calls) == 3


def test_agent_without_tool_calls_ends_as_no_patch(sandbox):
    st = _agent(sandbox, FakeChatModel([respond(text="I fixed it.")] * 3), NeverGate()).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.NO_PATCH


def test_feedback_reaches_the_model_and_retry_is_admitted(sandbox):
    seen = {}

    def after_refusal(messages):
        seen["feedback"] = json.loads(messages[-1].tool_results[0].content)
        return respond(call("edit_file", path="calc/rates.py", old_text=fx.REGRESSING, new_text=fx.GOOD))

    model = FakeChatModel([
        respond(call("edit_file", path="calc/rates.py", old_text=fx.OLD, new_text=fx.REGRESSING)),
        respond(call("submit_patch", reasoning="first")),
        after_refusal,
        respond(call("submit_patch", reasoning="second")),
    ])
    gate = ScriptedGate([(GateStatus.REFUSED, "REGRESSION"), (GateStatus.ADMITTED, None)])
    tracer = RecordingTracer()
    st = RepairAgent(model, gate, sandbox, fx.config(), tracer=tracer).run(_state(), fx.REPRO)
    assert st.final_status == FinalStatus.ADMITTED and st.current_iteration == 2
    assert seen["feedback"]["failure_code"] == "REGRESSION" and seen["feedback"]["attempts_remaining"] == 2
    assert "abs(part)" in gate.diffs[0] and "abs(part)" not in gate.diffs[1]
    kinds = [e["kind"] for e in tracer.events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("verification") == 2 and "generation" in kinds and "tool" in kinds


# ---------------------------------------------------------------- feedback from run.json

def test_feedback_from_regression_run_json():
    art = {"final_state": "REFUSED", "refusal": {"code": "REGRESSION", "stage": "REGRESSION", "message": "regressions"},
           "regression_results": {"newly_failing": ["tests.test_rates::test_percent_negative"], "missing_tests": []},
           "blast_radius": {"changed_files": ["calc/rates.py"], "allowed_files": ["calc/rates.py"]},
           "diff_lines_added": 2, "diff_lines_deleted": 1}
    fb = feedback_from_run_json(art, 1)
    assert fb.status == GateStatus.REFUSED and fb.failed_tests == ["tests.test_rates::test_percent_negative"]
    assert fb.suggested_next_action and fb.changed_files == ["calc/rates.py"]


def test_feedback_marks_apply_failure_and_admission():
    art = {"final_state": "REFUSED", "refusal": {"code": "GREEN_NOT_REACHED", "stage": "PATCH_LOOP", "message": "x"},
           "patch_history": [{"attempt": 1, "applied": False, "status": "APPLY_FAILED", "detail": "corrupt patch"}]}
    fb = feedback_from_run_json(art, 1)
    assert fb.patch_applied is False and "reset_workspace" in fb.suggested_next_action
    ok = feedback_from_run_json({"final_state": "ADMITTED", "blast_radius": {"changed_files": ["calc/rates.py"]}}, 2)
    assert ok.status == GateStatus.ADMITTED and ok.failure_code is None
    err = feedback_from_run_json({"final_state": "ERROR", "refusal": {"stage": "SANDBOX", "message": "no docker"}}, 3)
    assert err.status == GateStatus.GATE_ERROR


# ---------------------------------------------------------------- metrics

def test_metrics_distinguish_admission_correctness_and_unmeasured():
    def st(status, admitted_files=("calc/rates.py",), cost=0.1):
        s = _state()
        s.final_status = status
        s.estimated_cost = cost
        s.current_iteration = 1
        fb = GateFeedback(attempt=1, status=GateStatus.ADMITTED if status == FinalStatus.ADMITTED else GateStatus.REFUSED,
                          failure_code=None if status == FinalStatus.ADMITTED else "REGRESSION")
        from repair.state import AttemptRecord
        s.attempts.append(AttemptRecord(attempt=1, diff="d", changed_files=list(admitted_files), lines_added=1,
                                        lines_deleted=0, feedback=fb, agent_tested_reproduction=True))
        return s

    rs = [
        task_result_from_state("a", st(FinalStatus.ADMITTED), ["calc/rates.py"], hidden_check=True),
        task_result_from_state("b", st(FinalStatus.ADMITTED), ["calc/rates.py"], hidden_check=False),
        task_result_from_state("c", st(FinalStatus.ADMITTED), [], hidden_check=NOT_MEASURED),
        task_result_from_state("d", st(FinalStatus.MAX_ATTEMPTS_EXCEEDED), ["calc/other.py"]),
    ]
    m = aggregate(rs)
    assert m["successful_repairs"] == 3 and m["fix_rate"] == 0.75
    assert m["false_admissions"] == 1 and m["false_admission_rate"] == 0.5 and m["false_admission_measured_on"] == 2
    assert rs[2].false_admission == NOT_MEASURED
    assert m["regression_failures"] == 1 and m["localization_success"] == 2 and m["localization_measured_on"] == 3
    assert m["agent_claimed_success"] == 4
    assert m["cost_per_successful_fix"] == pytest.approx(0.4 / 3, abs=1e-4)


def test_false_admission_rate_is_not_measured_without_hidden_checks():
    s = _state()
    s.final_status = FinalStatus.ADMITTED
    assert aggregate([task_result_from_state("x", s)])["false_admission_rate"] == NOT_MEASURED


def test_cost_estimate_uses_published_price_and_refuses_to_guess():
    assert estimate_cost("claude-sonnet-5-5", Usage(1_000_000, 100_000)) == pytest.approx(3.0)
    assert estimate_cost("some-unknown-model", Usage(10, 10)) is None


# ---------------------------------------------------------------- tracing and redaction

def test_redaction_masks_secret_keys_formats_and_env_values(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    out = redact({"api_key": "x", "note": "token sk-ant-abcdefghijklmnopqrstu and ghp_abcdefghijklmnopqrstuvwxyz",
                  "args": ["prefix supersecretvalue123 suffix"]})
    assert out["api_key"] == "[REDACTED]"
    assert "sk-ant-" not in out["note"] and "ghp_" not in out["note"]
    assert "supersecretvalue123" not in out["args"][0]


class FakeObs:
    def __init__(self, log, name, kwargs):
        self.log, self.name, self.kwargs = log, name, kwargs

    def start_observation(self, **kw):
        self.log.append(("child", kw))
        return FakeObs(self.log, kw.get("name"), kw)

    def update(self, **kw):
        self.log.append(("update", kw))

    def update_trace(self, **kw):
        self.log.append(("trace", kw))

    def end(self):
        self.log.append(("end", self.name))


class FakeLangfuse:
    def __init__(self):
        self.log = []

    def create_trace_id(self, seed=None):
        return f"tid-{seed}"

    def start_observation(self, **kw):
        self.log.append(("root", kw))
        return FakeObs(self.log, kw["name"], kw)

    def flush(self):
        self.log.append(("flush", None))


def test_langfuse_tracer_maps_observation_types_tokens_and_cost(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-leave-123456")
    client = FakeLangfuse()
    t = LangfuseTracer(client=client)
    t.start_run("run-9", {"issue_id": "#1", "issue_title": "t", "repository": "r", "model": "m"})
    t.generation("llm-call", "claude-sonnet-5-5", 100, 20, 0.0004, 1.2, {"iteration": 0})
    t.tool("read_file", {"path": "calc/rates.py", "token": "abc"}, True, 0.01, {"output_chars": 10})
    t.verification(1, "REFUSED", "REGRESSION", 12.0, {"stage": "REGRESSION"})
    t.end_run("ADMITTED", {"note": "key sk-ant-should-never-leave-123456"})
    assert t.trace_id == "tid-run-9" and client.log[0][1]["as_type"] == "agent"
    children = [kw for kind, kw in client.log if kind == "child"]
    gen = next(c for c in children if c["as_type"] == "generation")
    assert gen["usage_details"] == {"input": 100, "output": 20, "total": 120} and gen["cost_details"] == {"total": 0.0004}
    tool = next(c for c in children if c["as_type"] == "tool")
    assert tool["input"]["token"] == "[REDACTED]"
    assert any(c["as_type"] == "chain" and c["output"]["failure_code"] == "REGRESSION" for c in children)
    assert "sk-ant-should-never-leave" not in json.dumps(client.log, default=str)
    assert client.log[-1] == ("flush", None)


# ---------------------------------------------------------------- Anthropic adapter (fake client)

class FakeBlock(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def test_anthropic_adapter_sends_no_sampling_params_and_replays_raw_turns():
    thinking = FakeBlock(type="thinking", thinking="", signature="sig")
    use = FakeBlock(type="tool_use", id="tu_1", name="read_file", input={"path": "calc/rates.py"})
    resp = SimpleNamespace(content=[thinking, use], stop_reason="tool_use", model="claude-sonnet-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    msgs = FakeMessages([resp, resp])
    model = AnthropicChatModel("claude-sonnet-5-5", client=SimpleNamespace(messages=msgs))
    tools = [ToolSpec("read_file", "d", {"type": "object", "properties": {}})]
    out = model.complete("sys", [Message("user", "hi")], tools, 16000)
    assert out.tool_calls[0].name == "read_file" and out.usage.total_tokens == 15
    req = msgs.requests[0]
    assert "temperature" not in req and req["tool_choice"] == {"type": "auto"}
    assert req["extra_body"]["output_config"] == {"effort": "medium"} and req["extra_body"]["fallbacks"] == "default"
    history = [Message("user", "hi"), Message("assistant", tool_calls=out.tool_calls, raw=out.raw),
               Message("user", tool_results=[ToolResult("tu_1", "{}")])]
    model.complete("sys", history, tools, 16000)
    sent = msgs.requests[1]["messages"][1]["content"]
    assert sent[0] == {"type": "thinking", "thinking": "", "signature": "sig"}  # replayed unchanged


def test_anthropic_refusal_becomes_llm_error():
    resp = SimpleNamespace(content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=0))
    model = AnthropicChatModel("claude-sonnet-5-5", client=SimpleNamespace(messages=FakeMessages([resp])))
    with pytest.raises(LLMError, match="refused"):
        model.complete("s", [Message("user", "x")], [], 100)
