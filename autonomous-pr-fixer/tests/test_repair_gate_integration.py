"""Repair agent against the real, unmodified Cerberus gate (host-unsafe sandbox on a trusted fixture).

Fake model, real verification: every verdict here comes from a run.json written by main.run_pipeline.
"""
from __future__ import annotations

import json
import os

from repair.llm.fake import FakeChatModel, call, respond
from repair.runner import RepairTask, run_repair
from repair.state import FinalStatus, GateStatus
from repair.tools.base import REPRO_TEST_PATH
from tests import repair_fixtures as fx


def _task(tmp_path):
    repo = fx.make_repo(str(tmp_path))
    return RepairTask(repo_dir=repo, issue_id="#1", title=fx.TITLE, body=fx.BODY, reproduction_test=fx.REPRO)


def _edit(new, old=fx.OLD):
    return call("edit_file", path="calc/rates.py", old_text=old, new_text=new)


def _run(tmp_path, steps, **cfg):
    out = str(tmp_path / "out")
    state = run_repair(_task(tmp_path), fx.config(**cfg), model=FakeChatModel(steps), run_id="t", out_root=out)
    return state, os.path.join(out, "t")


def test_first_attempt_admitted(tmp_path):
    state, out = _run(tmp_path, [
        respond(call("read_test", path=REPRO_TEST_PATH), call("search_code", query="def percent")),
        respond(_edit(fx.GOOD), call("run_test", target=REPRO_TEST_PATH)),
        respond(call("submit_patch", reasoning="guard zero whole")),
    ])
    assert state.final_status == FinalStatus.ADMITTED and state.current_iteration == 1
    with open(state.verification_result.run_json_path, encoding="utf-8") as f:
        assert json.load(f)["final_state"] == "ADMITTED"   # the verdict of record is the gate's
    saved = json.load(open(os.path.join(out, "agent_run.json"), encoding="utf-8"))
    assert saved["final_status"] == "ADMITTED" and saved["attempts"][0]["agent_tested_reproduction"] is True
    assert os.path.getsize(os.path.join(out, "agent.log")) > 0


def test_green_failure_then_fix(tmp_path):
    state, _ = _run(tmp_path, [
        respond(_edit(fx.NO_FIX)),
        respond(call("submit_patch", reasoning="float")),
        respond(_edit(fx.GOOD, old=fx.NO_FIX)),
        respond(call("submit_patch", reasoning="guard")),
    ])
    first = state.attempts[0].feedback
    assert first.status == GateStatus.REFUSED and first.failure_code == "GREEN_NOT_REACHED"
    assert state.final_status == FinalStatus.ADMITTED and state.current_iteration == 2


def test_regression_feedback_then_fix(tmp_path):
    state, _ = _run(tmp_path, [
        respond(_edit(fx.REGRESSING)),
        respond(call("submit_patch", reasoning="abs")),
        respond(call("reset_workspace"), _edit(fx.GOOD)),
        respond(call("submit_patch", reasoning="guard only")),
    ])
    first = state.attempts[0].feedback
    assert first.failure_code == "REGRESSION" and any("test_percent_negative" in t for t in first.failed_tests)
    assert state.final_status == FinalStatus.ADMITTED


def test_scope_violation_feedback_then_narrowed_patch(tmp_path):
    widen = call("edit_file", path="calc/fmt.py", old_text='f"{value:.1f}%"', new_text='f"{value:.2f}%"')
    state, _ = _run(tmp_path, [
        respond(_edit(fx.GOOD), widen),
        respond(call("submit_patch", reasoning="fix and tidy")),
        respond(call("edit_file", path="calc/fmt.py", old_text='f"{value:.2f}%"', new_text='f"{value:.1f}%"')),
        respond(call("submit_patch", reasoning="narrowed")),
    ])
    first = state.attempts[0].feedback
    assert first.failure_code == "SCOPE_VIOLATION" and "calc/fmt.py" in first.detail
    assert state.final_status == FinalStatus.ADMITTED and state.changed_files == ["calc/rates.py"]


def test_exhausted_attempts_are_reported_not_hidden(tmp_path):
    state, _ = _run(tmp_path, [
        respond(_edit(fx.NO_FIX)),
        respond(call("submit_patch", reasoning="a")),
        respond(call("submit_patch", reasoning="b")),
    ], max_attempts=2)
    assert state.final_status == FinalStatus.MAX_ATTEMPTS_EXCEEDED
    assert [a.feedback.failure_code for a in state.attempts] == ["GREEN_NOT_REACHED", "GREEN_NOT_REACHED"]


def test_cli_negative_demo_is_refused_then_admitted(capsys):
    import main

    assert main.main(["agent", "demo", "--unsafe-local-sandbox"]) == 0
    out = capsys.readouterr().out
    assert "SCRIPTED DEMO" in out and "FAIL (REFUSED, REGRESSION, REGRESSION)" in out and "Final status: ADMITTED" in out
