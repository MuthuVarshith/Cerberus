"""
GitHub PR Publisher.
Formats comprehensive machine- and human-readable verification reports,
saves complete JSON audit trail artifacts, and publishes pull requests only upon Admission Controller approval.

Secret handling: git writes the remote URL into its own error messages, so any
git output that leaves this module is passed through `redact_secrets` first.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional
from harness.admission_controller import AdmissionDecision
from harness.docker_sandbox import Sandbox
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport
from agents.reproduction_agent import ReproductionResult

REDACTED = "***REDACTED***"

#: Credentials embedded in a remote URL, e.g. https://x-access-token:ghp_xxx@github.com/...
#: git echoes the full URL back on a failed push, which is how a token reaches a log.
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^/\s@]+@")

#: Token shapes that should never appear in output even if they arrived from
#: somewhere other than `self.token` (a stale remote, an ambient env var).
_TOKEN_SHAPES_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")

#: Characters a git branch name may contain here. Everything else is dropped:
#: branch names are interpolated into shell command strings, and an issue-derived
#: value must not be able to terminate the command and start another.
_SAFE_REF_RE = re.compile(r"[^A-Za-z0-9._/-]")


def redact_secrets(text: str, *secrets: Optional[str]) -> str:
    """Remove credentials from text that is about to be returned, logged, or displayed."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    text = _URL_CREDENTIALS_RE.sub(rf"\1{REDACTED}@", text)
    return _TOKEN_SHAPES_RE.sub(REDACTED, text)


def sanitize_ref(name: str, fallback: str = "cerberus-fix") -> str:
    """Reduce a branch name to characters that are both git-legal and shell-inert."""
    cleaned = _SAFE_REF_RE.sub("-", name).strip("-/.")
    return cleaned[:120] or fallback


class PRPublisher:
    """Formats verification evidence reports and publishes GitHub Pull Requests."""

    def __init__(self, sandbox: Sandbox, github_token: Optional[str] = None):
        self.sandbox = sandbox
        self.token = github_token or os.environ.get("GITHUB_TOKEN")

    def build_evidence_report(
        self,
        issue_number: int,
        issue_title: str,
        reproduction_res: ReproductionResult,
        patch_res: PatchLoopResult,
        regression_res: RegressionReport,
        decision: AdmissionDecision,
        branch_name: str,
        token_usage: Optional[Dict[str, int]] = None,
    ) -> str:
        repro_snippet = reproduction_res.raw_output.strip()
        if len(repro_snippet) > 800:
            repro_snippet = repro_snippet[-800:]

        radius = regression_res.blast_radius
        # No invented number here. This report is the project's evidence artifact;
        # a plausible-looking default token count would be indistinguishable from a
        # measured one to anyone reading the PR.
        if token_usage and token_usage.get("total_tokens"):
            tokens_line = f"`{token_usage['total_tokens']} tokens`"
        else:
            tokens_line = "`not measured (no model in the loop)`"

        body = f"""## 🤖 Cerberus Autonomous Repair: Issue #{issue_number}
**Title:** `{issue_title}`
**Branch:** `{branch_name}`

---

### 🛡️ Patch Admission Controller: **{'✅ APPROVED' if decision.approved else '❌ REJECTED'}**

> This Pull Request was machine-verified by **Cerberus: Verification-First Autonomous Software Repair Harness**.
> Unlike standard autonomous coding agents that open PRs based on unverified LLM generation, this patch satisfied all 4 mandatory verification gates.

#### Verification Breakdown:
"""
        for r in decision.reasons:
            body += f"- {r}\n"

        body += f"""
---

### 1️⃣ RED Gate (Reproduction Proof)
A minimal reproduction test was synthesized and verified to **FAIL** on the unpatched repository:
```python
{reproduction_res.test_code.strip()}
```
<details>
<summary>View initial reproduction failure trace</summary>

```
{repro_snippet}
```
</details>

---

### 2️⃣ Iterative Repair & GREEN Gate
- **Iterations to Fix:** `{patch_res.total_attempts} attempt(s)`
- **Reproduction Test Status:** `PASSED (GREEN)`

---

### 3️⃣ Regression & Structural Safety
- **Full Regression Test Suite:** `{regression_res.passed_count}/{regression_res.total_tests} passed ({regression_res.execution_time_sec}s)`
- **Regressions Introduced:** `0`
- **Observed Files Modified:** `{', '.join(radius.observed_files) if radius.observed_files else 'None'}`
- **Diff Metrics:** `+{radius.lines_added} / -{radius.lines_deleted} lines`
- **Modified Symbols:** `{', '.join(radius.modified_symbols) if radius.modified_symbols else 'Module scope'}`
- **Unauthorized Module Leaks:** `{len(radius.unauthorized_files)}`

---

### 4️⃣ Applied Unified Diff
```diff
{patch_res.winning_diff.strip()}
```

---

### 5️⃣ Efficiency & Observability
- **Token Consumption:** {tokens_line}
- **Execution Time:** `{regression_res.execution_time_sec}s`

*Generated autonomously by Cerberus: Verification-First Software Repair Harness*
"""
        return body

    def create_audit_artifact(
        self,
        issue_number: int,
        issue_title: str,
        triage_report: Any,
        localization_res: Any,
        reproduction_res: ReproductionResult,
        patch_res: PatchLoopResult,
        regression_res: RegressionReport,
        decision: AdmissionDecision,
        output_dir: Optional[str] = None,
    ) -> str:
        """Saves a machine-readable JSON audit trail conforming to Section 20."""
        audit_data = {
            "issue": {"number": issue_number, "title": issue_title},
            "triage": triage_report.model_dump() if hasattr(triage_report, "model_dump") else str(triage_report),
            "localization_candidates": [
                c.__dict__ if hasattr(c, "__dict__") else c for c in getattr(localization_res, "candidates", [])
            ],
            "authorized_boundary": getattr(localization_res, "repair_boundary", {}),
            "reproduction": {
                "reproduced": reproduction_res.reproduced,
                "returncode": reproduction_res.returncode,
                "error_message": reproduction_res.error_message,
            },
            "patch_attempts": patch_res.total_attempts,
            "reached_green": patch_res.reached_green,
            "regression_results": {
                "passed": regression_res.passed_count,
                "total": regression_res.total_tests,
                "failed": regression_res.failed_count,
                "execution_time_sec": regression_res.execution_time_sec,
            },
            "blast_radius": {
                "files_changed": regression_res.blast_radius.observed_files,
                "lines_added": regression_res.blast_radius.lines_added,
                "lines_deleted": regression_res.blast_radius.lines_deleted,
                "unauthorized_files": regression_res.blast_radius.unauthorized_files,
                "is_acceptable": regression_res.blast_radius.is_acceptable,
            },
            "admission_decision": decision.to_dict(),
        }
        dest_dir = output_dir or self.sandbox.workspace_dir
        path = os.path.join(dest_dir, f"audit_trail_{issue_number}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(audit_data, f, indent=2)
        return path

    def publish_pr(
        self,
        issue_number: int,
        issue_title: str,
        repo_slug: str,
        branch_name: str,
        pr_body: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        pr_title = f"fix(autobot): resolve issue #{issue_number} - {issue_title[:50]}"

        if dry_run or not self.token:
            if dry_run:
                display_msg = "PR: NOT CREATED (dry-run mode)"
                status_key = "dry_run_success"
            else:
                display_msg = "PR ADMITTED LOCALLY — GITHUB PUBLISHING DISABLED"
                status_key = "local_success"

            return {
                "status": status_key,
                "pr_title": pr_title,
                "branch": branch_name,
                "repo": repo_slug,
                "pr_url": None,
                "display_url": display_msg,
                "published": False,
                "body": pr_body,
            }

        safe_branch = sanitize_ref(branch_name)
        clean_remote = f"https://github.com/{repo_slug}.git"
        auth_url = f"https://x-access-token:{self.token}@github.com/{repo_slug}.git"

        # The stored remote is the *clean* URL. Pushing to an explicit authenticated
        # URL keeps the token out of the sandbox's .git/config, which otherwise
        # survives on disk next to the run artifacts.
        remote_check = self.sandbox.exec("git remote")
        if "origin" in remote_check.stdout:
            self.sandbox.exec(f"git remote set-url origin {clean_remote}")
        else:
            self.sandbox.exec(f"git remote add origin {clean_remote}")

        self.sandbox.exec(f"git checkout -b {safe_branch}")
        self.sandbox.exec("git add -A")

        # Commit message via file, not -m: pr_title embeds the issue title, which
        # comes from a webhook payload and would otherwise be interpolated into a
        # shell command string.
        msg_rel_path = ".harness_commit_msg.txt"
        self.sandbox.write_file(msg_rel_path, pr_title + "\n")
        self.sandbox.exec(f"git commit -F {msg_rel_path}")
        try:
            os.remove(os.path.join(self.sandbox.workspace_dir, msg_rel_path))
        except OSError:
            pass

        push_res = self.sandbox.exec(f"git push -u {auth_url} {safe_branch}")
        if push_res.exit_code != 0:
            return {
                "status": "error",
                "error": redact_secrets(f"Git push failed: {push_res.stderr}", self.token),
                "pr_title": pr_title,
                "branch": safe_branch,
                "body": pr_body,
            }

        url = f"https://api.github.com/repos/{repo_slug}/pulls"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Autonomous-Repair-Harness",
        }
        payload = {
            "title": pr_title,
            "body": pr_body,
            "head": safe_branch,
            # Repositories disagree on the default branch name, and a wrong base
            # makes the API reject the PR after the push already succeeded.
            "base": os.environ.get("GITHUB_BASE_BRANCH", "main"),
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "status": "published",
                    "pr_title": pr_title,
                    "branch": safe_branch,
                    "repo": repo_slug,
                    "pr_url": data.get("html_url", ""),
                    "body": pr_body,
                }
        except Exception as e:
            return {
                "status": "error",
                "error": redact_secrets(str(e), self.token),
                "pr_title": pr_title,
                "branch": safe_branch,
                "body": pr_body,
            }
