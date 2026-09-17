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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
