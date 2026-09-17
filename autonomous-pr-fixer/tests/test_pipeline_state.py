"""
Tests for the pipeline state machine: gate order, three terminal states, and
the guarantee that ADMITTED cannot be reached by skipping a gate.
"""
from __future__ import annotations

import pytest

from harness.pipeline_state import (
    VALID_TRANSITIONS,
    InvalidTransitionError,
    PipelineState,
    PipelineStateMachine,
    TERMINAL_STATES,
)

HAPPY_PATH = [
    PipelineState.TRIAGE_PENDING,
    PipelineState.TRIAGED,
    PipelineState.SETUP_PENDING,
    PipelineState.SETUP_COMPLETE,
    PipelineState.REPRODUCTION_PENDING,
    PipelineState.REPRODUCED_RED,
    PipelineState.BASELINE_PENDING,
    PipelineState.BASELINE_RECORDED,
    PipelineState.LOCALIZATION_PENDING,
    PipelineState.LOCALIZED,
    PipelineState.PATCH_PENDING,
    PipelineState.PATCH_GREEN,
    PipelineState.REGRESSION_PENDING,
    PipelineState.REGRESSION_CLEAN,
    PipelineState.SCOPE_PENDING,
    PipelineState.SCOPE_ACCEPTABLE,
    PipelineState.ADMISSION_PENDING,
    PipelineState.ADMITTED,
]


def _walk_to(target: PipelineState) -> PipelineStateMachine:
    sm = PipelineStateMachine()
    for s in HAPPY_PATH:
        sm.transition(s)
        if s == target:
            return sm
    raise AssertionError(f"{target} not on happy path")


def test_initial_state():
    sm = PipelineStateMachine()
    assert sm.state == PipelineState.UNVERIFIED
    assert not sm.is_terminal
    assert not sm.is_admitted
    assert not sm.is_refused


def test_happy_path_full_pipeline():
    sm = _walk_to(PipelineState.ADMITTED)
    assert sm.is_terminal and sm.is_admitted and not sm.is_refused


def test_exactly_three_terminal_states():
    assert TERMINAL_STATES == {PipelineState.ADMITTED, PipelineState.REFUSED, PipelineState.ERROR}


def test_admitted_is_reachable_only_from_admission_pending():
    sources = [s for s, targets in VALID_TRANSITIONS.items() if PipelineState.ADMITTED in targets]
    assert sources == [PipelineState.ADMISSION_PENDING]


def test_invalid_transition_raises():
    with pytest.raises(InvalidTransitionError):
        PipelineStateMachine().transition(PipelineState.ADMITTED)


@pytest.mark.parametrize(
    "stop_at,skip_to",
    [
        (PipelineState.REPRODUCTION_PENDING, PipelineState.LOCALIZATION_PENDING),  # skip RED
        (PipelineState.REPRODUCED_RED, PipelineState.LOCALIZATION_PENDING),        # skip baseline
        (PipelineState.PATCH_PENDING, PipelineState.REGRESSION_PENDING),           # skip GREEN
        (PipelineState.PATCH_GREEN, PipelineState.SCOPE_PENDING),                  # skip regression
        (PipelineState.REGRESSION_CLEAN, PipelineState.ADMISSION_PENDING),         # skip scope
    ],
)
def test_gates_cannot_be_skipped(stop_at, skip_to):
    sm = _walk_to(stop_at)
    with pytest.raises(InvalidTransitionError):
        sm.transition(skip_to)


@pytest.mark.parametrize("stage", [s for s in HAPPY_PATH if s is not PipelineState.ADMITTED])
def test_every_stage_can_refuse_or_error(stage):
    for terminal in (PipelineState.REFUSED, PipelineState.ERROR):
        sm = _walk_to(stage)
        sm.transition(terminal)
        assert sm.is_terminal


def test_cannot_transition_from_terminal():
    sm = _walk_to(PipelineState.REPRODUCTION_PENDING)
    sm.transition(PipelineState.REFUSED)
    with pytest.raises(RuntimeError):
        sm.transition(PipelineState.TRIAGE_PENDING)


def test_history_is_recorded():
    sm = _walk_to(PipelineState.TRIAGED)
    assert sm.history == [PipelineState.UNVERIFIED, PipelineState.TRIAGE_PENDING, PipelineState.TRIAGED]


def test_to_dict_contains_required_fields():
    d = PipelineStateMachine().to_dict()
    for key in ("current_state", "is_terminal", "is_admitted", "is_refused", "history"):
        assert key in d
