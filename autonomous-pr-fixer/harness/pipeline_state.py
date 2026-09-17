"""
Pipeline state machine.

A run ends in exactly one of three terminal states:
  - ADMITTED: every gate passed; only reachable from ADMISSION_PENDING.
  - REFUSED:  a gate withheld admission; the reason is a RefusalCode.
  - ERROR:    the run could not produce a decision (environment, setup, bug).

Progress states enforce gate order: a run cannot reach ADMISSION_PENDING
without passing through RED, baseline, GREEN, regression and scope in turn.
Any non-terminal state may move to REFUSED or ERROR.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, List, Set


class PipelineState(str, Enum):
    """All named states of the verification pipeline."""
    UNVERIFIED = "UNVERIFIED"
    TRIAGE_PENDING = "TRIAGE_PENDING"
    TRIAGED = "TRIAGED"
    SETUP_PENDING = "SETUP_PENDING"
    SETUP_COMPLETE = "SETUP_COMPLETE"
    REPRODUCTION_PENDING = "REPRODUCTION_PENDING"
    REPRODUCED_RED = "REPRODUCED_RED"
    BASELINE_PENDING = "BASELINE_PENDING"
    BASELINE_RECORDED = "BASELINE_RECORDED"
    LOCALIZATION_PENDING = "LOCALIZATION_PENDING"
    LOCALIZED = "LOCALIZED"
    PATCH_PENDING = "PATCH_PENDING"
    PATCH_GREEN = "PATCH_GREEN"
    REGRESSION_PENDING = "REGRESSION_PENDING"
    REGRESSION_CLEAN = "REGRESSION_CLEAN"
    SCOPE_PENDING = "SCOPE_PENDING"
    SCOPE_ACCEPTABLE = "SCOPE_ACCEPTABLE"
    ADMISSION_PENDING = "ADMISSION_PENDING"
    ADMITTED = "ADMITTED"
    REFUSED = "REFUSED"
    ERROR = "ERROR"


class RefusalCode(str, Enum):
    """Why a run was refused. Every code names the gate that withheld admission."""
    NO_REPRODUCTION_TEST = "NO_REPRODUCTION_TEST"
    RED_NOT_FAILING = "RED_NOT_FAILING"
    RED_INVALID_TEST = "RED_INVALID_TEST"
    RED_WRONG_REASON = "RED_WRONG_REASON"
    RED_UNRELATED_TEST = "RED_UNRELATED_TEST"
    RED_NONDETERMINISTIC = "RED_NONDETERMINISTIC"
    RED_UNVERIFIABLE = "RED_UNVERIFIABLE"
    NO_TEST_COMMAND = "NO_TEST_COMMAND"
    BASELINE_UNVERIFIABLE = "BASELINE_UNVERIFIABLE"
    NOT_LOCALIZED = "NOT_LOCALIZED"
    NO_PATCH_SOURCE = "NO_PATCH_SOURCE"
    GREEN_NOT_REACHED = "GREEN_NOT_REACHED"
    GREEN_NONDETERMINISTIC = "GREEN_NONDETERMINISTIC"
    REPRODUCTION_TEST_TAMPERED = "REPRODUCTION_TEST_TAMPERED"
    REGRESSION = "REGRESSION"
    REGRESSION_UNVERIFIABLE = "REGRESSION_UNVERIFIABLE"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    EMPTY_PATCH = "EMPTY_PATCH"


TERMINAL_STATES: FrozenSet[PipelineState] = frozenset({
    PipelineState.ADMITTED,
    PipelineState.REFUSED,
    PipelineState.ERROR,
})

_ORDERED_PROGRESS: List[PipelineState] = [
    PipelineState.UNVERIFIED,
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


def _build_transitions() -> Dict[PipelineState, Set[PipelineState]]:
    transitions: Dict[PipelineState, Set[PipelineState]] = {s: set() for s in PipelineState}
    for current, nxt in zip(_ORDERED_PROGRESS, _ORDERED_PROGRESS[1:]):
        transitions[current].add(nxt)
    for state in PipelineState:
        if state not in TERMINAL_STATES:
            transitions[state].update({PipelineState.REFUSED, PipelineState.ERROR})
    return transitions


VALID_TRANSITIONS: Dict[PipelineState, Set[PipelineState]] = _build_transitions()


class InvalidTransitionError(RuntimeError):
    """Raised when an agent attempts an undeclared state transition."""
    def __init__(self, from_state: PipelineState, to_state: PipelineState) -> None:
        allowed = sorted(s.value for s in VALID_TRANSITIONS.get(from_state, set()))
        super().__init__(
            f"Invalid pipeline transition: {from_state.value} -> {to_state.value}. "
            f"Allowed from {from_state.value}: {allowed}"
        )
        self.from_state = from_state
        self.to_state = to_state


class PipelineStateMachine:
    """Lightweight state machine enforcing the declared transition graph."""

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
    def is_refused(self) -> bool:
        return self._state == PipelineState.REFUSED

    def transition(self, target: PipelineState) -> PipelineState:
        """Move to target if declared, else raise InvalidTransitionError."""
        if self.is_terminal:
            raise RuntimeError(f"Cannot transition from terminal state {self._state.value}.")
        if target not in VALID_TRANSITIONS[self._state]:
            raise InvalidTransitionError(self._state, target)
        self._state = target
        self._history.append(target)
        return target

    def to_dict(self) -> dict:
        return {
            "current_state": self._state.value,
            "is_terminal": self.is_terminal,
            "is_admitted": self.is_admitted,
            "is_refused": self.is_refused,
            "history": [s.value for s in self._history],
        }
