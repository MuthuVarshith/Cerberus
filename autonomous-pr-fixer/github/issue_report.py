"""
Render an issue-triggered repair run for GitHub: issue comment, Check Run, PR body.

Everything derived from the repository or the model (paths, test ids, messages, the
diff) is fenced as code. Sizes are bounded so the comment stays under GitHub's limit.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from github.issue_commands import bullet_list, fence, inline_code

MAX_DIFF_CHARS = 30_000
ADMITTED_NEXT = ("**Awaiting human review.** Comment `/cerberus approve` to open a draft pull request from the "
                 "base commit, or `/cerberus reject` to discard it. Cerberus never merges.")


def _retrieval_queries(agent_run: Optional[Dict[str, Any]]) -> List[str]:
    calls = (agent_run or {}).get("tool_calls") or []
    return [str((c.get("arguments") or {}).get("query", "")) for c in calls if c.get("tool") == "retrieve_code"]


def _attempt_lines(agent_run: Optional[Dict[str, Any]]) -> List[str]:
    lines = []
    for a in ((agent_run or {}).get("attempts") or [])[:10]:
        fb = a.get("feedback") or {}
        lines.append(f"attempt {a.get('attempt')}: {fb.get('status')}"
                     + (f" at {fb.get('stage')} ({fb.get('failure_code')})" if fb.get("failure_code") else "")
                     + f"; files: {', '.join(a.get('changed_files') or []) or 'none'}"
                     + (f"; {str(fb.get('message'))[:300]}" if fb.get("message") else ""))
    return lines


def admitted_diff(result: Dict[str, Any], gate_run: Optional[Dict[str, Any]]) -> str:
    """The diff the gate verified (run.json evidence), else the agent's submitted patch."""
    return ((gate_run or {}).get("evidence") or {}).get("diff") or result.get("patch") or ""


def render_summary(row: Dict[str, Any], result: Optional[Dict[str, Any]], agent_run: Optional[Dict[str, Any]],
                   gate_run: Optional[Dict[str, Any]], note: str = "") -> str:
    result = result or {}
    status = row.get("status")
    cost = result.get("estimated_cost")
    lines = [
        f"### Cerberus repair for #{row['issue_number']}: {status}",
        "",
        "| | |", "| --- | --- |",
        f"| Run | {inline_code(row['run_id'])} |",
        f"| Final status | {inline_code(result.get('final_status') or row.get('final_status') or status)} |",
        f"| Gate | {inline_code(result.get('gate_status') or 'no gate run')}"
        + (f" at {inline_code(result.get('failure_stage'))} / {inline_code(result.get('failure_code'))}"
           if result.get("failure_code") else "") + " |",
        f"| Base commit | {inline_code(row.get('base_sha') or 'unknown')} on {inline_code(row.get('default_branch') or '?')} |",
        f"| Attempts | {result.get('attempts', 0)} |",
        f"| Model | {inline_code(result.get('model') or 'unknown')} |",
        f"| Tokens | {result.get('input_tokens', 0)} in / {result.get('output_tokens', 0)} out / {result.get('total_tokens', 0)} total |",
        f"| Estimated cost | {'not known' if cost is None else f'${cost:.4f}'} |",
        f"| Retrieval | {inline_code(result.get('retrieval') or 'off')} |",
    ]
    if note:
        lines += ["", note]
    if result.get("failed_tests"):
        lines += ["", "**Failed tests**", bullet_list(result["failed_tests"])]
    if result.get("changed_files"):
        lines += ["", "**Changed files**", bullet_list(result["changed_files"])]
    attempts = _attempt_lines(agent_run)
    if attempts:
        lines += ["", "<details><summary>Attempts</summary>", "", fence("\n".join(attempts), limit=6000), "", "</details>"]
    queries = _retrieval_queries(agent_run)
    if queries:
        lines += ["", f"<details><summary>Retrieval queries ({len(queries)})</summary>", "",
                  fence("\n".join(queries), limit=3000), "", "</details>"]
    if result.get("message") and status != "admitted":
        lines += ["", "**Reason**", fence(str(result["message"]), limit=2000)]
    return "\n".join(lines)


def render_comment(row: Dict[str, Any], result: Optional[Dict[str, Any]], agent_run: Optional[Dict[str, Any]],
                   gate_run: Optional[Dict[str, Any]], note: str = "") -> str:
    parts = [render_summary(row, result, agent_run, gate_run, note)]
    diff = admitted_diff(result or {}, gate_run)
    if diff:
        parts.append(f"<details><summary>Patch diff ({diff.count(chr(10))} lines)</summary>\n\n"
                     + fence(diff, "diff", MAX_DIFF_CHARS) + "\n\n</details>")
    if row.get("status") == "admitted":
        parts.append(ADMITTED_NEXT)
    elif row.get("status") in ("refused", "error"):
        parts.append("No pull request can be opened for this run: the gate did not admit a patch, and a human "
                     "approval cannot override the gate. Adjust the issue or reproduction test and comment `/cerberus fix` to retry.")
    return "\n\n".join(parts)


def check_output(row: Dict[str, Any], result: Optional[Dict[str, Any]], agent_run: Optional[Dict[str, Any]],
                 gate_run: Optional[Dict[str, Any]], note: str = "") -> Tuple[str, str, str, str]:
    """(conclusion, title, summary, text). Non-admitted runs are `neutral`: the base commit did not fail."""
    status = row.get("status")
    if status == "admitted":
        conclusion, title = "success", "Repair admitted: awaiting human review"
    else:
        conclusion, title = "neutral", f"No admitted repair ({status})"
    diff = admitted_diff(result or {}, gate_run)
    return conclusion, title, render_summary(row, result, agent_run, gate_run, note), fence(diff, "diff", MAX_DIFF_CHARS) if diff else ""


def render_pr_body(row: Dict[str, Any], result: Dict[str, Any], agent_run: Optional[Dict[str, Any]],
                   gate_run: Optional[Dict[str, Any]], approver: str, moved_note: str) -> str:
    return "\n\n".join(filter(None, [
        f"Fixes #{row['issue_number']}",
        f"Draft pull request opened by Cerberus after human approval by {inline_code(approver)}. "
        "Every verification gate admitted this patch; it has not been merged and auto-merge is not enabled.",
        moved_note,
        render_comment({**row, "status": "approved"}, result, agent_run, gate_run),
    ]))
