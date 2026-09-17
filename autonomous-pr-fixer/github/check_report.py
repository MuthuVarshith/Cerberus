"""
Render a run.json artifact as a GitHub Check Run result.

Every outcome gets a report, not only admissions: a refusal is most useful to a
reviewer when it says which gate refused and what evidence it saw.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

CONCLUSIONS = {"ADMITTED": "success", "REFUSED": "failure", "ERROR": "neutral"}


def _ids(items: List[str], limit: int = 20) -> str:
    if not items:
        return "none"
    shown = ", ".join(f"`{i}`" for i in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def check_run_output(artifact: Dict[str, Any], timed_out: bool = False) -> Tuple[str, str, str, str]:
    """Return (conclusion, title, summary, text) for a finished run."""
    state = artifact.get("final_state") or "ERROR"
    refusal = artifact.get("refusal") or {}
    conclusion = "timed_out" if timed_out else CONCLUSIONS.get(state, "neutral")

    if state == "ADMITTED":
        title = "Admitted: every verification gate passed"
    elif state == "REFUSED":
        title = f"Refused at {refusal.get('stage', 'unknown gate')}: {refusal.get('code', 'unknown')}"
    elif timed_out:
        title = "Timed out before a decision"
    else:
        title = "Could not verify (error)"

    red = artifact.get("red_gate") or {}
    baseline = artifact.get("baseline") or {}
    regression = artifact.get("regression_results") or {}
    scope = artifact.get("blast_radius") or {}
    sandbox = artifact.get("sandbox") or {}
    source = artifact.get("patch_source") or {}

    lines = [
        f"**Decision:** {state}",
        f"**Base commit:** `{(artifact.get('reproducibility') or {}).get('base_commit_sha', 'unknown')}`",
        f"**Patch source:** {source.get('name', 'n/a')}",
        f"**Sandbox:** {sandbox.get('isolation', 'unknown')}",
    ]
    if refusal and state != "ADMITTED":
        lines += ["", f"> **{refusal.get('code') or 'ERROR'}** ({refusal.get('stage')}): {refusal.get('message')}"]

    lines += ["", "| Gate | Result | Evidence |", "| --- | --- | --- |"]
    if red:
        red_result = "pass" if red.get("reproduced") else "fail"
        failures = ", ".join(f"`{tid}` ({etype})" for tid, etype in (red.get("failure_types") or {}).items()) or "none"
        lines.append(f"| RED | {red_result} | {red.get('runs', 0)} run(s); failing: {failures} |")
    if baseline:
        lines.append(f"| Baseline | recorded | {baseline.get('total')} tests; already failing: {_ids(baseline.get('failing') or [])} |")
    if artifact.get("patch_attempts"):
        lines.append(f"| GREEN | {'pass' if artifact.get('reached_green') else 'fail'} | {artifact.get('patch_attempts')} attempt(s) |")
    if regression:
        ok = regression.get("results_parsed") and not regression.get("newly_failing") and not regression.get("missing_tests")
        lines.append(
            f"| Regression | {'pass' if ok else 'fail'} | newly failing: {_ids(regression.get('newly_failing') or [])}; "
            f"no longer run: {_ids(regression.get('missing_tests') or [])}; pre-existing: {len(regression.get('preexisting_failures') or [])}; "
            f"flaky: {len(regression.get('flaky_tests') or [])} |"
        )
    if scope:
        lines.append(
            f"| Scope | {'pass' if scope.get('is_acceptable') else 'fail'} | files: {_ids(scope.get('changed_files') or [])}; "
            f"new: {_ids(scope.get('new_files') or [])}; modified tests: {_ids(scope.get('modified_test_files') or [])}; "
            f"extended tests (run at base): {_ids(scope.get('extended_test_files') or [])}; "
            f"+{scope.get('lines_added', 0)}/-{scope.get('lines_deleted', 0)}; symbols: {_ids(scope.get('changed_symbols') or [])} |"
        )

    lines += ["", "Cerberus never merges. A passing check means the gates found evidence, not that the change is correct."]

    evidence = artifact.get("evidence") or {}
    text_parts = []
    if evidence.get("reproduction_test_code"):
        text_parts.append("### Reproduction test\n\n```python\n" + evidence["reproduction_test_code"].strip() + "\n```")
    if evidence.get("diff"):
        text_parts.append("### Verified diff\n\n```diff\n" + evidence["diff"].strip() + "\n```")
    return conclusion, title, "\n".join(lines), "\n\n".join(text_parts)
