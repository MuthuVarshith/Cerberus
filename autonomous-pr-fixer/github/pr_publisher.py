"""
GitHub PR publisher.

Builds the evidence report and, only for an admitted patch, publishes it.

Publishing never happens inside the sandbox: the token must not be visible to
repository code, and the repair container has no network. Instead the verified
diff is treated as data and applied to a fresh host-side clone of the source
repository at the verified base commit, committed, and pushed. The token reaches
git through GIT_CONFIG_* environment variables (not argv, not a remote URL, not
.git/config), and the pull request is opened as a draft.

Secret handling: any git or API output that leaves this module is passed
through `redact_secrets` first.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence

from harness.admission_controller import AdmissionDecision
from harness.diff_utils import DiffUtils
from harness.docker_sandbox import host_unsafe_environment
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport
from agents.reproduction_agent import ReproductionResult

REDACTED = "***REDACTED***"

#: Credentials embedded in a remote URL, e.g. https://x-access-token:ghp_xxx@github.com/...
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^/\s@]+@")

#: Token shapes that should never appear in output even if they arrived from
#: somewhere other than `self.token` (a stale remote, an ambient env var).
_TOKEN_SHAPES_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")

#: Characters a git branch name may contain here. Everything else is dropped.
_SAFE_REF_RE = re.compile(r"[^A-Za-z0-9._/-]")

GitRunner = Callable[[Sequence[str], Optional[str], Optional[Dict[str, str]]], subprocess.CompletedProcess]


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


def _host_git(args: Sequence[str], cwd: Optional[str], extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    env = host_unsafe_environment()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env=env,
    )


def _auth_env(token: str) -> Dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _remove_tree(path: str) -> None:
    def _retry(func, target, *_):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)


class PRPublisher:
    """Formats verification evidence reports and publishes draft pull requests."""

    def __init__(self, github_token: Optional[str] = None, git_runner: Optional[GitRunner] = None):
        self.token = github_token or os.environ.get("GITHUB_TOKEN")
        self._git: GitRunner = git_runner or _host_git

    def build_evidence_report(
        self,
        issue_number: int,
        issue_title: str,
        reproduction_res: ReproductionResult,
        patch_res: PatchLoopResult,
        regression_res: RegressionReport,
        scope_res: Any,
        decision: AdmissionDecision,
        branch_name: str,
        token_usage: Optional[Dict[str, int]] = None,
        base_commit: str = "",
    ) -> str:
        repro_snippet = (reproduction_res.raw_output or "").strip()
        if len(repro_snippet) > 800:
            repro_snippet = repro_snippet[-800:]

        # No invented number here: a plausible default token count would be
        # indistinguishable from a measured one to anyone reading the PR.
        if token_usage and token_usage.get("total_tokens"):
            tokens_line = f"`{token_usage['total_tokens']} tokens`"
        else:
            tokens_line = "`not measured (no model in the loop)`"

        red_runs = getattr(reproduction_res, "runs", 0)
        failing = getattr(reproduction_res, "failing_test_ids", []) or []
        failure_types = getattr(reproduction_res, "failure_types", {}) or {}
        failing_lines = "\n".join(
            f"  - `{tid}` failed with `{failure_types.get(tid) or 'unknown'}`" for tid in failing
        ) or "  - (none recorded)"

        changed_files = getattr(scope_res, "changed_files", []) or []
        new_files = getattr(scope_res, "new_files", []) or []
        symbols = getattr(scope_res, "changed_symbols", []) or []

        body = f"""## Cerberus verification report: issue #{issue_number}
**Title:** `{issue_title}`
**Branch:** `{branch_name}`
**Base commit:** `{base_commit or 'unknown'}`

### Admission decision: **{'APPROVED' if decision.approved else 'REJECTED'}**

"""
        for r in decision.reasons:
            body += f"- {r}\n"

        body += f"""
---

### 1. RED Gate (reproduction on the unpatched code)
The reproduction test failed on the base commit in {red_runs} of {red_runs} runs, for these reasons:
{failing_lines}

```python
{(reproduction_res.test_code or '').strip()}
```
<details>
<summary>Reproduction failure output</summary>

```
{repro_snippet}
```
</details>

### 2. GREEN Gate
- **Patch attempts:** `{patch_res.total_attempts}`
- **Reproduction test with the patch:** `{'PASSED' if patch_res.reached_green else 'NOT PASSING'}`

### 3. Regression Gate (baseline-aware)
- **Baseline tests:** `{regression_res.baseline_total}`
- **After patch:** `{regression_res.passed_count}/{regression_res.total_tests} passing`
- **Newly failing:** `{len(regression_res.newly_failing)}`
- **Already failing at baseline:** `{len(regression_res.preexisting_failures)}`
- **Flaky (also failed on base re-check):** `{len(regression_res.flaky_tests)}`

### 4. Scope Gate
- **Files changed:** `{', '.join(changed_files) if changed_files else 'None'}`
- **New files:** `{', '.join(new_files) if new_files else 'None'}`
- **Lines:** `+{getattr(scope_res, 'lines_added', 0)} / -{getattr(scope_res, 'lines_deleted', 0)}`
- **Changed symbols (Python AST):** `{', '.join(symbols) if symbols else 'None identified'}`

### 5. Applied diff
```diff
{(patch_res.winning_diff or '').strip()}
```

### 6. Cost
- **Model tokens:** {tokens_line}
- **Regression suite time:** `{regression_res.execution_time_sec}s`

*This draft pull request was opened by Cerberus. It has not been merged and requires human review.*
"""
        return body

    def publish_pr(
        self,
        issue_number: int,
        issue_title: str,
        repo_slug: str,
        branch_name: str,
        pr_body: str,
        dry_run: bool = True,
        fork_owner: Optional[str] = None,
        diff_text: str = "",
        source_repo_dir: Optional[str] = None,
        base_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        pr_title = f"fix(cerberus): resolve issue #{issue_number} - {issue_title[:50]}"

        if dry_run or not self.token:
            return {
                "status": "dry_run_success" if dry_run else "local_success",
                "pr_title": pr_title,
                "branch": branch_name,
                "repo": repo_slug,
                "pr_url": None,
                "display_url": "NOT CREATED (dry-run mode)" if dry_run else "NOT CREATED (no GitHub token)",
                "published": False,
                "body": pr_body,
            }

        safe_branch = sanitize_ref(branch_name)
        error = self._validate_publish_inputs(diff_text, source_repo_dir, base_commit)
        if error:
            return {"status": "error", "error": error, "pr_title": pr_title, "branch": safe_branch, "body": pr_body}

        push_slug = f"{fork_owner}/{repo_slug.split('/')[-1]}" if fork_owner else repo_slug
        pr_head = f"{fork_owner}:{safe_branch}" if fork_owner else safe_branch

        work_root = tempfile.mkdtemp(prefix="cerberus_publish_")
        try:
            clone_dir = os.path.join(work_root, "repo")
            patch_path = os.path.join(work_root, "verified.patch")
            msg_path = os.path.join(work_root, "commit_msg.txt")
            with open(patch_path, "w", encoding="utf-8", newline="") as f:
                f.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
            with open(msg_path, "w", encoding="utf-8") as f:
                f.write(pr_title + "\n\nVerified by Cerberus. See the pull request body for evidence.\n")

            clone = self._git(
                ["clone", "--quiet", "--no-checkout", "-c", "core.autocrlf=false", os.path.abspath(source_repo_dir), clone_dir],
                None,
                None,
            )
            if clone.returncode != 0:
                return self._git_error("git clone", clone, pr_title, safe_branch, pr_body)
            for args, label in (
                (["checkout", "--quiet", "-b", safe_branch, base_commit], "git checkout"),
                (["apply", "--index", "--whitespace=nowarn", patch_path], "git apply"),
                (["-c", "user.name=Cerberus", "-c", "user.email=cerberus@localhost", "commit", "--quiet", "-F", msg_path], "git commit"),
            ):
                res = self._git(args, clone_dir, None)
                if res.returncode != 0:
                    return self._git_error(label, res, pr_title, safe_branch, pr_body)

            push = self._git(
                ["push", "--quiet", f"https://github.com/{push_slug}.git", f"HEAD:refs/heads/{safe_branch}"],
                clone_dir,
                _auth_env(self.token),
            )
            if push.returncode != 0:
                return self._git_error("Git push", push, pr_title, safe_branch, pr_body)
        finally:
            _remove_tree(work_root)

        return self._open_pull_request(repo_slug, pr_title, pr_body, pr_head, safe_branch)

    @staticmethod
    def _validate_publish_inputs(diff_text: str, source_repo_dir: Optional[str], base_commit: Optional[str]) -> str:
        if not diff_text.strip():
            return "Refusing to publish: no verified diff was provided."
        if not source_repo_dir or not os.path.isdir(source_repo_dir):
            return "Refusing to publish: the source repository directory is not available."
        if not base_commit or not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            return "Refusing to publish: the verified base commit is unknown."
        forbidden = DiffUtils.forbidden_targets(diff_text)
        if forbidden:
            return f"Refusing to publish: the diff touches forbidden paths ({', '.join(forbidden)})."
        if any(path.startswith(".github/workflows/") for path in DiffUtils.parse_targeted_files(diff_text)):
            return "Refusing to publish: Cerberus never modifies GitHub workflow files."
        return ""

    def _git_error(self, label: str, res: subprocess.CompletedProcess, pr_title: str, branch: str, body: str) -> Dict[str, Any]:
        return {
            "status": "error",
            "error": redact_secrets(f"{label} failed: {res.stderr}", self.token),
            "pr_title": pr_title,
            "branch": branch,
            "body": body,
        }

    def _open_pull_request(self, repo_slug: str, pr_title: str, pr_body: str, pr_head: str, branch: str) -> Dict[str, Any]:
        payload = {
            "title": pr_title,
            "body": pr_body,
            "head": pr_head,
            # Repositories disagree on the default branch name, and a wrong base
            # makes the API reject the PR after the push already succeeded.
            "base": os.environ.get("GITHUB_BASE_BRANCH", "main"),
            "draft": True,
        }
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo_slug}/pulls",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Cerberus-Verification-Gate",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {
                "status": "error",
                "error": redact_secrets(str(e), self.token),
                "pr_title": pr_title,
                "branch": branch,
                "body": pr_body,
            }
        url = data.get("html_url")
        if not url:
            return {"status": "error", "error": "GitHub did not return a pull request URL.", "pr_title": pr_title, "branch": branch, "body": pr_body}
        return {
            "status": "published",
            "pr_title": pr_title,
            "branch": branch,
            "repo": repo_slug,
            "pr_url": url,
            "body": pr_body,
        }
