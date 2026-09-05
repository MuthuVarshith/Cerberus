"""
Pipeline State Machine for Cerberus: Verification-First Autonomous Software Repair Harness.

Defines all named pipeline states, terminal failure states, and valid transitions.
PipelineStateMachine enforces that only declared transitions can occur;
InvalidTransitionError prevents agents from skipping verification gates.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, List, Set


class PipelineState(str, Enum):
    """All named states of the repair pipeline."""
    UNVERIFIED = "UNVERIFIED"
    TRIAGE_PENDING = "TRIAGE_PENDING"
    TRIAGED = "TRIAGED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    REPRODUCTION_PENDING = "REPRODUCTION_PENDING"
    REPRODUCED_RED = "REPRODUCED_RED"
    LOCALIZATION_PENDING = "LOCALIZATION_PENDING"
    LOCALIZED = "LOCALIZED"
    PATCH_PENDING = "PATCH_PENDING"
    PATCH_GREEN = "PATCH_GREEN"
    REGRESSION_PENDING = "REGRESSION_PENDING"
    REGRESSION_CLEAN = "REGRESSION_CLEAN"
    BLAST_RADIUS_PENDING = "BLAST_RADIUS_PENDING"
    BLAST_RADIUS_ACCEPTABLE = "BLAST_RADIUS_ACCEPTABLE"
    ADMISSION_PENDING = "ADMISSION_PENDING"
    ADMITTED = "ADMITTED"
    REJECTED_NON_REPRODUCIBLE = "REJECTED_NON_REPRODUCIBLE"
    REJECTED_PATCH_FAILED = "REJECTED_PATCH_FAILED"
    REJECTED_EMPTY_PATCH = "REJECTED_EMPTY_PATCH"
    REJECTED_REGRESSION = "REJECTED_REGRESSION"
    REJECTED_BLAST_RADIUS = "REJECTED_BLAST_RADIUS"
    REJECTED_ADMISSION = "REJECTED_ADMISSION"
    ERROR = "ERROR"


TERMINAL_STATES: FrozenSet[PipelineState] = frozenset({
    PipelineState.ADMITTED,
    PipelineState.REJECTED_NON_REPRODUCIBLE,
    PipelineState.REJECTED_PATCH_FAILED,
    PipelineState.REJECTED_EMPTY_PATCH,
    PipelineState.REJECTED_REGRESSION,
    PipelineState.REJECTED_BLAST_RADIUS,
    PipelineState.REJECTED_ADMISSION,
    PipelineState.ERROR,
})

REJECTION_STATES: FrozenSet[PipelineState] = frozenset({
    PipelineState.REJECTED_NON_REPRODUCIBLE,
    PipelineState.REJECTED_PATCH_FAILED,
    PipelineState.REJECTED_EMPTY_PATCH,
    PipelineState.REJECTED_REGRESSION,
    PipelineState.REJECTED_BLAST_RADIUS,
    PipelineState.REJECTED_ADMISSION,
})

VALID_TRANSITIONS: Dict[PipelineState, Set[PipelineState]] = {
    PipelineState.UNVERIFIED: {PipelineState.TRIAGE_PENDING, PipelineState.ERROR},
    PipelineState.TRIAGE_PENDING: {PipelineState.TRIAGED, PipelineState.ERROR},
    PipelineState.TRIAGED: {PipelineState.INDEXING, PipelineState.ERROR},
    PipelineState.INDEXING: {PipelineState.INDEXED, PipelineState.ERROR},
    PipelineState.INDEXED: {PipelineState.REPRODUCTION_PENDING, PipelineState.ERROR},
    PipelineState.REPRODUCTION_PENDING: {
        PipelineState.REPRODUCED_RED,
        PipelineState.REJECTED_NON_REPRODUCIBLE,
        PipelineState.ERROR,
    },
    PipelineState.REPRODUCED_RED: {PipelineState.LOCALIZATION_PENDING, PipelineState.ERROR},
    PipelineState.LOCALIZATION_PENDING: {PipelineState.LOCALIZED, PipelineState.ERROR},
    PipelineState.LOCALIZED: {PipelineState.PATCH_PENDING, PipelineState.ERROR},
    PipelineState.PATCH_PENDING: {
        PipelineState.PATCH_GREEN,
        PipelineState.REJECTED_PATCH_FAILED,
        PipelineState.REJECTED_EMPTY_PATCH,
        PipelineState.ERROR,
    },
    PipelineState.PATCH_GREEN: {PipelineState.REGRESSION_PENDING, PipelineState.ERROR},
    PipelineState.REGRESSION_PENDING: {
        PipelineState.REGRESSION_CLEAN,
        PipelineState.REJECTED_REGRESSION,
        PipelineState.ERROR,
    },
    PipelineState.REGRESSION_CLEAN: {PipelineState.BLAST_RADIUS_PENDING, PipelineState.ERROR},
    PipelineState.BLAST_RADIUS_PENDING: {
        PipelineState.BLAST_RADIUS_ACCEPTABLE,
        PipelineState.REJECTED_BLAST_RADIUS,
        PipelineState.ERROR,
    },
    PipelineState.BLAST_RADIUS_ACCEPTABLE: {PipelineState.ADMISSION_PENDING, PipelineState.ERROR},
    PipelineState.ADMISSION_PENDING: {
        PipelineState.ADMITTED,
        PipelineState.REJECTED_EMPTY_PATCH,
        PipelineState.REJECTED_ADMISSION,
        PipelineState.ERROR,
    },
    PipelineState.ADMITTED: set(),
    PipelineState.REJECTED_NON_REPRODUCIBLE: set(),
    PipelineState.REJECTED_PATCH_FAILED: set(),
    PipelineState.REJECTED_EMPTY_PATCH: set(),
    PipelineState.REJECTED_REGRESSION: set(),
    PipelineState.REJECTED_BLAST_RADIUS: set(),
    PipelineState.REJECTED_ADMISSION: set(),
    PipelineState.ERROR: set(),
}


class InvalidTransitionError(RuntimeError):
    """Raised when an agent attempts an undeclared state transition."""
    def __init__(self, from_state: PipelineState, to_state: PipelineState) -> None:
        allowed = [s.value for s in VALID_TRANSITIONS.get(from_state, set())]
        super().__init__(
            f"Invalid pipeline transition: {from_state.value} -> {to_state.value}. "
            f"Allowed from {from_state.value}: {allowed}"
        )
        self.from_state = from_state
        self.to_state = to_state


class PipelineStateMachine:
    """
    Lightweight state machine enforcing the declared transition graph.

    Usage:
        sm = PipelineStateMachine()
        sm.transition(PipelineState.TRIAGE_PENDING)
        sm.transition(PipelineState.TRIAGED)
    """

    def __init__(self) -> None:
        self._state = PipelineState.UNVERIFIED
        self._history: List[PipelineState] = [PipelineState.UNVERIFIED]

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def history(self) -> List[PipelineState]:
        return list(self._history)

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def is_admitted(self) -> bool:
        return self._state == PipelineState.ADMITTED

    @property
    def is_rejected(self) -> bool:
        return self._state in REJECTION_STATES

    def transition(self, target: PipelineState) -> PipelineState:
        """Move to target if declared, else raise InvalidTransitionError."""
        if self.is_terminal:
            raise RuntimeError(f"Cannot transition from terminal state {self._state.value}.")
        allowed = VALID_TRANSITIONS.get(self._state, set())
        if target not in allowed:
            raise InvalidTransitionError(self._state, target)
        self._state = target
        self._history.append(target)
        return target

    def to_dict(self) -> dict:
        return {
            "current_state": self._state.value,
            "is_terminal": self.is_terminal,
            "is_admitted": self.is_admitted,
            "is_rejected": self.is_rejected,
            "history": [s.value for s in self._history],
        }
