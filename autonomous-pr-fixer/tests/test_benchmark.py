"""
Tests for the benchmark's arithmetic and integrity checks. They never modify
the frozen benchmark files.
"""
import json

import pytest

from evaluation import benchmark as bm


def _result(**overrides):
    base = dict(
        id="x", category="correct-fix", expected_decision="ADMITTED", acceptable_refusal_codes=[],
        final_state="ADMITTED", admitted=True, refusal_code=None, refusal_stage=None,
        red_passed=True, reproduction_valid_on_reference=True, patch_attempts=1, wall_time_sec=1.0,
        candidate_applies=True, hidden_correct=True, ungated_admits=True, tokens=None,
    )
    base.update(overrides)
    return base


def test_wilson_interval_matches_known_values():
    assert bm.wilson(0, 0) is None
    lo, hi = bm.wilson(6, 60)
    assert (lo, hi) == (0.047, 0.201)
    assert bm.wilson(5, 5)[1] == 1.0


def test_summary_counts_false_admissions_against_the_ideal_decision():
    results = [
        _result(id="good"),
        _result(id="wrong-admitted", expected_decision="REFUSED", acceptable_refusal_codes=["*"], hidden_correct=False),
        _result(id="refused-ok", expected_decision="REFUSED", acceptable_refusal_codes=["SCOPE_VIOLATION"],
                admitted=False, final_state="REFUSED", refusal_code="SCOPE_VIOLATION"),
        _result(id="refused-wrong-code", expected_decision="REFUSED", acceptable_refusal_codes=["REGRESSION"],
                admitted=False, final_state="REFUSED", refusal_code="SCOPE_VIOLATION", ungated_admits=False),
        _result(id="false-rejection", admitted=False, final_state="REFUSED", refusal_code="RED_WRONG_REASON"),
    ]
    s = bm.summarize(results)
    c, u = s["cerberus"], s["ungated_baseline"]
    assert c["admitted"] == 2
    assert (c["false_admission_rate"]["k"], c["false_admission_rate"]["n"]) == (1, 2)
    assert c["admitted_code_failing_hidden_tests"] == 1
    assert (c["admitted_fix_rate"]["k"], c["admitted_fix_rate"]["n"]) == (1, 2)
    assert (c["false_rejection_rate"]["k"], c["false_rejection_rate"]["n"]) == (1, 2)
    assert (c["rejection_accuracy"]["k"], c["rejection_accuracy"]["n"]) == (1, 3)
    assert (c["refused_when_should_refuse"]["k"], c["refused_when_should_refuse"]["n"]) == (2, 3)
    assert c["refusals_by_code"] == {"SCOPE_VIOLATION": 2, "RED_WRONG_REASON": 1}
    assert u["admitted"] == 4
    assert (u["false_admission_rate"]["k"], u["false_admission_rate"]["n"]) == (2, 4)


def test_report_renders_every_instance_and_no_invented_cost(tmp_path):
    results = [_result(id="a"), _result(id="b", admitted=False, final_state="REFUSED", refusal_code="REGRESSION",
                                        expected_decision="REFUSED", acceptable_refusal_codes=["REGRESSION"])]
    text = bm.render_report(bm.summarize(results), results, {
        "date": "d", "cerberus_commit": "c", "manifest_digest": "m", "isolation": "host-unsafe", "python": "3", "platform": "p",
    })
    assert "| a |" in text and "| b |" in text
    assert "not measured" in text
    assert "authored by the Cerberus developer" in text


def test_frozen_manifest_matches_the_checked_in_benchmark():
    """The committed instance set must be exactly what was frozen."""
    assert bm.check_manifest() == json.load(open(bm.MANIFEST_FILE, encoding="utf-8"))["digest"]


def test_manifest_detects_a_changed_instance(monkeypatch):
    real = bm.compute_manifest()
    tampered = json.loads(json.dumps(real))
    tampered["files"]["instances/v1.yml"] = "0" * 64
    tampered["digest"] = "tampered"
    monkeypatch.setattr(bm, "compute_manifest", lambda: real)
    monkeypatch.setattr(bm.json, "load", lambda f: tampered)
    with pytest.raises(bm.BenchmarkError, match="instances/v1.yml"):
        bm.check_manifest()


def test_apply_ops_requires_exactly_one_match(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(bm.BenchmarkError, match="occurs 2 times"):
        bm.apply_ops(str(tmp_path), [{"file": "m.py", "find": "x = 1", "replace": "x = 2"}], "t")


def test_all_instances_load_and_reference_fixes_are_derivable():
    instances = bm.load_instances()
    assert len(instances) == 24
    for inst in instances:
        if inst.bug:
            assert len(bm.reference_ops(inst)) == len(inst.bug)
