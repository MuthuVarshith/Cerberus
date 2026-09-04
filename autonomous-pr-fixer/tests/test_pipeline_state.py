"""
Tests for the pipeline state machine.
Covers: valid transitions, invalid transitions, terminal state enforcement,
rejection states, and ensuring no agent can skip gates.
"""
from __future__ import annotations

import pytest
from harness.pipeline_state import (
    InvalidTransitionError,
    PipelineState,
    PipelineStateMachine,
    TERMINAL_STATES,
    REJECTION_STATES,
)


def test_initial_state():
    sm = PipelineStateMachine()
    assert sm.state == PipelineState.UNVERIFIED
    assert not sm.is_terminal
    assert not sm.is_admitted
    assert not sm.is_rejected


def test_happy_path_full_pipeline():
    """Walk the complete happy path from UNVERIFIED to ADMITTED."""
    sm = PipelineStateMachine()
    happy_path = [
        PipelineState.TRIAGE_PENDING,
        PipelineState.TRIAGED,
        PipelineState.INDEXING,
        PipelineState.INDEXED,
        PipelineState.REPRODUCTION_PENDING,
        PipelineState.REPRODUCED_RED,
        PipelineState.LOCALIZATION_PENDING,
        PipelineState.LOCALIZED,
        PipelineState.PATCH_PENDING,
        PipelineState.PATCH_GREEN,
        PipelineState.REGRESSION_PENDING,
        PipelineState.REGRESSION_CLEAN,
        PipelineState.BLAST_RADIUS_PENDING,
        PipelineState.BLAST_RADIUS_ACCEPTABLE,
        PipelineState.ADMISSION_PENDING,
        PipelineState.ADMITTED,
    ]
    for s in happy_path:
        sm.transition(s)
    assert sm.state == PipelineState.ADMITTED
    assert sm.is_terminal
    assert sm.is_admitted
    assert not sm.is_rejected


def test_invalid_transition_raises():
    """Attempting to skip directly from UNVERIFIED to ADMITTED must raise."""
    sm = PipelineStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition(PipelineState.ADMITTED)


def test_cannot_skip_reproduction_gate():
    """An agent cannot jump from INDEXED straight to LOCALIZED."""
    sm = PipelineStateMachine()
    for s in [PipelineState.TRIAGE_PENDING, PipelineState.TRIAGED,
               PipelineState.INDEXING, PipelineState.INDEXED]:
        sm.transition(s)
    with pytest.raises(InvalidTransitionError):
        sm.transition(PipelineState.LOCALIZED)


def test_cannot_skip_regression_gate():
    """An agent cannot jump from PATCH_GREEN straight to ADMITTED."""
    sm = PipelineStateMachine()
    for s in [
        PipelineState.TRIAGE_PENDING, PipelineState.TRIAGED,
        PipelineState.INDEXING, PipelineState.INDEXED,
        PipelineState.REPRODUCTION_PENDING, PipelineState.REPRODUCED_RED,
        PipelineState.LOCALIZATION_PENDING, PipelineState.LOCALIZED,
        PipelineState.PATCH_PENDING, PipelineState.PATCH_GREEN,
    ]:
        sm.transition(s)
    with pytest.raises(InvalidTransitionError):
        sm.transition(PipelineState.ADMITTED)


def test_rejection_non_reproducible():
    """Issue fails RED gate -> REJECTED_NON_REPRODUCIBLE terminal state."""
    sm = PipelineStateMachine()
    sm.transition(PipelineState.TRIAGE_PENDING)
    sm.transition(PipelineState.TRIAGED)
    sm.transition(PipelineState.INDEXING)
    sm.transition(PipelineState.INDEXED)
    sm.transition(PipelineState.REPRODUCTION_PENDING)
    sm.transition(PipelineState.REJECTED_NON_REPRODUCIBLE)
    assert sm.is_terminal
    assert sm.is_rejected
    assert not sm.is_admitted


def test_cannot_transition_from_terminal():
    """Once in a terminal state, all transitions must raise RuntimeError."""
    sm = PipelineStateMachine()
    sm.transition(PipelineState.TRIAGE_PENDING)
    sm.transition(PipelineState.TRIAGED)
    sm.transition(PipelineState.INDEXING)
    sm.transition(PipelineState.INDEXED)
    sm.transition(PipelineState.REPRODUCTION_PENDING)
    sm.transition(PipelineState.REJECTED_NON_REPRODUCIBLE)
    assert sm.is_terminal
    with pytest.raises(RuntimeError):
        sm.transition(PipelineState.TRIAGE_PENDING)


def test_history_is_recorded():
    sm = PipelineStateMachine()
    sm.transition(PipelineState.TRIAGE_PENDING)
    sm.transition(PipelineState.TRIAGED)
    hist = sm.history
    assert hist[0] == PipelineState.UNVERIFIED
    assert hist[-1] == PipelineState.TRIAGED
    assert len(hist) == 3


def test_to_dict_contains_required_fields():
    sm = PipelineStateMachine()
    d = sm.to_dict()
    assert "current_state" in d
    assert "is_terminal" in d
    assert "is_admitted" in d
    assert "is_rejected" in d
    assert "history" in d


def test_all_rejection_states_are_terminal():
    for rs in REJECTION_STATES:
        assert rs in TERMINAL_STATES, f"{rs} should be in TERMINAL_STATES"


def test_rejected_empty_patch_state():
    """Verify REJECTED_EMPTY_PATCH transition from ADMISSION_PENDING and terminal properties."""
    sm = PipelineStateMachine()
    path = [
        PipelineState.TRIAGE_PENDING,
        PipelineState.TRIAGED,
        PipelineState.INDEXING,
        PipelineState.INDEXED,
        PipelineState.REPRODUCTION_PENDING,
        PipelineState.REPRODUCED_RED,
        PipelineState.LOCALIZATION_PENDING,
        PipelineState.LOCALIZED,
        PipelineState.PATCH_PENDING,
        PipelineState.PATCH_GREEN,
        PipelineState.REGRESSION_PENDING,
        PipelineState.REGRESSION_CLEAN,
        PipelineState.BLAST_RADIUS_PENDING,
        PipelineState.BLAST_RADIUS_ACCEPTABLE,
        PipelineState.ADMISSION_PENDING,
        PipelineState.REJECTED_EMPTY_PATCH,
    ]
    for s in path:
        sm.transition(s)
    assert sm.state == PipelineState.REJECTED_EMPTY_PATCH
    assert sm.is_terminal is True
    assert sm.is_rejected is True
    assert sm.is_admitted is False
    with pytest.raises(RuntimeError):
        sm.transition(PipelineState.ADMITTED)
