"""
Tests that reported evaluation figures are derived from their inputs.

The project's claim is about gate behaviour, so the numbers describing that
behaviour must follow from the records they summarise. Two failure modes are
pinned here: a hand-written figure drifting away from its data, and an
unmeasured quantity being presented as a measurement.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.ablation_runner import (
    BUG_PROFILES,
    CONFIGS,
    BugProfile,
    GateConfig,
    evaluate_config,
    run_ablation_study,
)
from evaluation.baseline_runner import run_baseline_comparison
from evaluation.metrics_reporter import MetricsReporter, RunRecord


def _record(**overrides) -> RunRecord:
    base = dict(
        instance_id="inst",
        reproduced=True,
        top_1_correct=True,
        top_3_correct=True,
        target_passed=True,
        regression_clean=True,
        blast_radius_clean=True,
        admitted_for_pr=True,
        patch_attempts=1,
        total_tokens=0,
        runtime_sec=1.0,
        patch_size_lines=4,
    )
    base.update(overrides)
    return RunRecord(**base)


def test_unmeasured_tokens_and_localization_are_labelled_not_averaged():
    """A record with no model behind it must not yield a token average."""
    table = MetricsReporter([_record(total_tokens=1500)]).format_markdown_table()
    assert "not measured" in table
    # The fabricated-looking number must not appear as a measurement.
    assert "1500" not in table


def test_measured_tokens_are_reported():
    table = MetricsReporter(
        [_record(total_tokens=1500, tokens_measured=True, localization_measured=True)]
    ).format_markdown_table()
    assert "1500" in table
    assert "100.0%" in table  # localization now reportable
    assert "not measured" not in table


def test_safe_and_unsafe_admissions_are_separated():
    """Admitting a patch that broke a regression counts as unsafe, not resolved."""
    m = MetricsReporter([
        _record(instance_id="ok"),
        _record(instance_id="broke_regression", regression_clean=False),
        _record(instance_id="leaked", blast_radius_clean=False),
        _record(instance_id="blocked", admitted_for_pr=False, target_passed=False),
    ]).compute_metrics()

    assert m["safe_admitted"] == 1
    assert m["unsafe_admitted"] == 2
    assert m["safe_resolution_rate"] == 25.0
    assert m["unsafe_pr_rate"] == 50.0
    # Admission rate alone cannot distinguish these, which is why both exist.
    assert m["pr_admission_rate"] == 75.0


def test_each_gate_strictly_reduces_false_positives():
    """The ablation must show every added gate blocking something the prior one let through."""
    rates = [evaluate_config(cfg, BUG_PROFILES)[0] for cfg in CONFIGS]
    assert rates == sorted(rates, reverse=True)
    assert rates[0] > rates[-1]
    assert rates[-1] == 0.0


def test_gates_do_not_reject_a_genuinely_safe_patch():
    """The safe PR rate must be identical with all gates on and all gates off."""
    safe_rates = [evaluate_config(cfg, BUG_PROFILES)[1] for cfg in CONFIGS]
    assert len(set(safe_rates)) == 1


def test_disabled_gate_cannot_reject():
    """An off gate is inert: the ungated config admits every profile."""
    ungated = GateConfig("none", red_gate=False, blast_radius=False, regression=False)
    assert all(ungated.admits(p) for p in BUG_PROFILES)


@pytest.mark.parametrize(
    "profile,expected_admitted",
    [
        (BugProfile("safe", True, True, True, True), True),
        (BugProfile("not_reproduced", False, True, True, True), False),
        (BugProfile("regression", True, True, False, True), False),
        (BugProfile("leak", True, True, True, False), False),
    ],
)
def test_full_harness_admits_only_safe_profiles(profile, expected_admitted):
    full = CONFIGS[-1]
    assert full.admits(profile) is expected_admitted


def test_ablation_table_reports_its_own_computed_rates():
    """Rendered percentages must match evaluate_config, not a hand-written literal."""
    table = run_ablation_study()
    for cfg in CONFIGS:
        fp_pct, safe_pct, unsafe_n, _ = evaluate_config(cfg, BUG_PROFILES)
        assert f"{fp_pct}% ({unsafe_n}/{len(BUG_PROFILES)})" in table
        assert f"{safe_pct}%" in table


def test_baseline_table_reports_zero_unsafe_prs_for_the_harness():
    """The comparison's headline claim, read back off the rendered table."""
    table = run_baseline_comparison()
    assert f"🛡️ 0.0% (0/{len(BUG_PROFILES)})" in table
    assert "stipulated inputs rather than measured runs" in table


def test_baseline_table_does_not_claim_a_localization_advantage():
    """Unchecked localization booleans must not become a comparative percentage.

    This row previously read `60.0% / 60.0%` vs `80.0% / 80.0% (AST + RAG)`, which
    asserts the retrieval layer localizes better than a baseline — a claim nothing
    in the repository measures.
    """
    table = run_baseline_comparison()
    localization_row = next(
        line for line in table.splitlines() if "Localization" in line
    )
    assert localization_row.count("`not measured`") == 3
    assert "%" not in localization_row
