"""
Issue-triggered repair runs with human approval before any pull request.

  fix      clone the default branch, record its HEAD SHA, run github.repair_job (frozen
           repair interface) in the Docker sandbox, report as an issue comment and as a
           Check Run on the base commit.
  approve  only for an ADMITTED run: branch from the recorded base SHA, apply the diff the
           gate admitted, push and open a draft PR (pr_publisher's token-safe mechanics).
  reject   record the human rejection.

The GitHub token never reaches the repair subprocess, the sandbox, argv or a comment.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from github.client import GitHubAPIError, GitHubClient
from github.issue_commands import REPRO_FORMAT_HELP, extract_reproduction_test, fence, reproduction_test_problem
from github.issue_report import admitted_diff, check_output, render_comment, render_pr_body
from github.pr_publisher import PRPublisher, redact_secrets
from harness.docker_sandbox import host_unsafe_environment

if TYPE_CHECKING:
    from github.app_service import CerberusApp, Job

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
ERROR_STATUSES = {"ERROR", "LLM_FAILURE", "SETUP_FAILURE"}
FORWARDED_ENV_PREFIXES = ("LLM_", "LANGFUSE_", "MAX_")
FORWARDED_ENV = ("RETRIEVAL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
                 "OPENAI_API_KEY", "CERBERUS_SANDBOX", "CERBERUS_SANDBOX_IMAGE", "HF_HOME")


def _read_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class _AppPublisher(PRPublisher):
    """pr_publisher's clone/apply/commit/push, with the PR opened through the App client on the right base."""

    def __init__(self, token: str, client: GitHubClient, owner: str, repo: str, base_branch: str):
        super().__init__(github_token=token)
        self._client, self._owner, self._repo, self._base = client, owner, repo, base_branch
        self.draft = True

    def _open_pull_request(self, repo_slug: str, pr_title: str, pr_body: str, pr_head: str, branch: str) -> Dict[str, Any]:
        try:
            try:
                data = self._client.create_pull(self._owner, self._repo, pr_title, pr_head, self._base, pr_body, draft=True)
            except GitHubAPIError as exc:  # repositories without draft PR support
                if exc.status != 422 or "draft" not in str(exc).lower():
                    raise
                self.draft = False
                data = self._client.create_pull(self._owner, self._repo, pr_title, pr_head, self._base, pr_body, draft=False)
        except Exception as exc:
            return {"status": "error", "error": redact_secrets(str(exc), self.token), "branch": branch}
        url = data.get("html_url")
        if not url:
            return {"status": "error", "error": "GitHub did not return a pull request URL.", "branch": branch}
        return {"status": "published", "pr_url": url, "branch": branch}


class IssueRepairService:
    def __init__(self, app: "CerberusApp"):
        self.app = app
        self.store = app.store

    # ----------------------------------------------------------- helpers

    def _client(self, job: "Job") -> Tuple[str, GitHubClient]:
        token = self.app.token_for_installation(job.installation_id)
        return token, self.app.client_factory(token)

    def _comment(self, client: GitHubClient, job: "Job", body: str) -> Optional[int]:
        try:
            return client.create_issue_comment(job.owner, job.repo, job.number, body)
        except Exception:
            return None

    def _report(self, client: GitHubClient, job: "Job", row: Dict[str, Any], body: str) -> None:
        if row.get("comment_id"):
            try:
                client.update_issue_comment(job.owner, job.repo, int(row["comment_id"]), body)
                return
            except Exception:
                pass
        cid = self._comment(client, job, body)
        if cid:
            self.store.update_issue_run(row["run_id"], comment_id=cid)

    @staticmethod
    def _git(args: list, cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=host_unsafe_environment(), timeout=300)

    def _env(self) -> Dict[str, str]:
        env = host_unsafe_environment()
        for key, value in os.environ.items():
            upper = key.upper()
            if upper.startswith("GITHUB") or upper == "CERBERUS_API_TOKEN":
                continue
            if upper in FORWARDED_ENV or upper.startswith(FORWARDED_ENV_PREFIXES):
                env[key] = value
        env["CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS"] = str(self.app.settings.run_timeout_seconds + 300)
        return env

    def _resolve(self, client: GitHubClient, job: "Job") -> Optional[Dict[str, Any]]:
        target = job.extra.get("target_run_id")
        row = self.store.get_issue_run(target) if target else self.store.latest_issue_run(job.slug, job.number)
        if row is None or row["repo"] != job.slug or int(row["issue_number"]) != job.number:
            self._comment(client, job, "Cerberus found no repair run" + (f" `{target}`" if target else "")
                          + " for this issue. Start one with `/cerberus fix`.")
            return None
        return row

    # --------------------------------------------------------------- fix

    def run_fix(self, job: "Job") -> None:
        app, s = self.app, self.app.settings
        token, client = self._client(job)
        self.store.update_run(job.run_id, status="running", started_at=time.time())
        self.store.update_issue_run(job.run_id, status="running")
        row = self.store.get_issue_run(job.run_id) or {}
        self._report(client, job, row, f"Cerberus repair run `{job.run_id}` started for #{job.number}. "
                                        "The report will replace this comment.")
        row = self.store.get_issue_run(job.run_id) or row

        refusal = app._sandbox_refusal()
        if refusal:
            return self._finish(client, job, "error", None, None, None, f"Cerberus cannot run: {refusal}")
        issue = client.get_issue(job.owner, job.repo, job.number)
        problem = reproduction_test_problem(issue.get("body") or "")
        if problem:
            return self._finish(client, job, "error", None, None, None, f"{problem}\n\n{REPRO_FORMAT_HELP}")
        repro = extract_reproduction_test(issue.get("body") or "") or ""

        branch = client.get_repo(job.owner, job.repo).get("default_branch", "") if hasattr(client, "get_repo") else ""
        branch = branch or job.extra.get("default_branch") or "main"
        if not _BRANCH_RE.match(branch):
            return self._finish(client, job, "error", None, None, None, "Unsupported default branch name.")
        path = app.repos.prepare(job.owner, job.repo, token, [f"+refs/heads/{branch}:refs/cerberus/default"])
        sha = self._git(["rev-parse", "--verify", "refs/cerberus/default^{commit}"], path).stdout.strip()
        if not _SHA_RE.match(sha):
            return self._finish(client, job, "error", None, None, None, "Could not resolve the default branch HEAD.")
        self._git(["update-ref", f"refs/cerberus/runs/{job.run_id}", sha], path)
        self.store.update_issue_run(job.run_id, base_sha=sha, default_branch=branch)
        self.store.update_run(job.run_id, head_sha=sha)
        try:
            check_id = client.create_check_run(job.owner, job.repo, sha, f"Cerberus repair (#{job.number})", job.run_id)
            self.store.update_issue_run(job.run_id, check_run_id=check_id)
        except Exception:
            pass  # the issue comment still carries the result

        run_dir = app._run_dir(job.run_id)
        repro_path = app._write_run_file(job.run_id, "test_reproduce.py", repro)
        result_path = os.path.join(run_dir, "repair_result.json")
        spec_path = os.path.join(run_dir, "repair_spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump({"run_id": job.run_id, "repo_dir": path, "base_sha": sha, "issue_number": job.number,
                       "issue_title": (issue.get("title") or f"#{job.number}")[:256],
                       "issue_body": (issue.get("body") or "")[:60_000], "repro_test_path": repro_path,
                       "retrieval": s.retrieval, "out_root": os.path.join(s.data_dir, "repair"),
                       "tmp_dir": os.path.join(s.data_dir, "tmp")}, f)
        argv = [sys.executable, "-m", "github.repair_job", "--spec", spec_path, "--result", result_path]
        _, timed_out = app.runner(argv, self._env(), s.run_timeout_seconds, os.path.join(run_dir, "repair.log"))

        result = _read_json(result_path)
        self.store.update_issue_run(job.run_id, result_path=result_path if result else None)
        if timed_out or result is None:
            why = f"timed out after {s.run_timeout_seconds}s" if timed_out else "the repair produced no result"
            return self._finish(client, job, "error", result, None, None, f"Run {why}.")
        agent_run = _read_json(result.get("agent_run_json"))
        gate_run = _read_json(result.get("gate_run_json"))
        admitted = (result.get("admitted") is True and result.get("final_status") == "ADMITTED"
                    and result.get("gate_status") == "ADMITTED" and (gate_run or {}).get("final_state") == "ADMITTED")
        status = "admitted" if admitted else ("error" if result.get("final_status") in ERROR_STATUSES else "refused")
        self._finish(client, job, status, result, agent_run, gate_run, "")

    def _finish(self, client: GitHubClient, job: "Job", status: str, result: Optional[Dict[str, Any]],
                agent_run: Optional[Dict[str, Any]], gate_run: Optional[Dict[str, Any]], note: str) -> None:
        final = (result or {}).get("final_status") or ("ERROR" if status == "error" else None)
        self.store.update_issue_run(job.run_id, status=status, final_status=final,
                                    agent_run_path=(result or {}).get("agent_run_json"),
                                    error=note[:2000] if status == "error" and note else None)
        self.store.update_run(job.run_id, status="finished", finished_at=time.time(),
                              final_state={"admitted": "ADMITTED", "refused": "REFUSED"}.get(status, "ERROR"),
                              refusal_code=(result or {}).get("failure_code"),
                              artifact_path=(result or {}).get("agent_run_json"))
        row = self.store.get_issue_run(job.run_id) or {}
        note_md = fence(note, limit=4000) if note and REPRO_FORMAT_HELP not in note else note
        self._report(client, job, row, render_comment(row, result, agent_run, gate_run, note_md))
        if row.get("check_run_id"):
            try:
                conclusion, title, summary, text = check_output(row, result, agent_run, gate_run, note_md)
                client.complete_check_run(job.owner, job.repo, int(row["check_run_id"]), conclusion, title, summary, text)
            except Exception:
                pass

    def on_crash(self, job: "Job", message: str) -> None:
        if job.kind != "issue_fix":
            return
        self.store.update_issue_run(job.run_id, status="error", error=message)
        self.store.update_run(job.run_id, status="finished", final_state="ERROR", error=message, finished_at=time.time())
        try:
            _, client = self._client(job)
            self._comment(client, job, f"Cerberus repair run `{job.run_id}` failed with an internal error.")
        except Exception:
            pass

    # ------------------------------------------------------------ review

    def approve(self, job: "Job") -> None:
        token, client = self._client(job)
        row = self._resolve(client, job)
        if row is None:
            return
        rid, status = row["run_id"], row["status"]
        if status == "pr_opened":
            return void(self._comment(client, job, f"Run `{rid}` already has a pull request: {row['pr_url']}"))
        if status in ("queued", "running"):
            return void(self._comment(client, job, f"Run `{rid}` is still {status}; wait for its report."))
        if status == "rejected":
            return void(self._comment(client, job, f"Run `{rid}` was rejected. Start a new run with `/cerberus fix`."))
        if status not in ("admitted", "approved"):
            return void(self._comment(client, job, f"Cannot approve run `{rid}`: its status is **{status}**. Only a run the "
                                                   "Cerberus gate ADMITTED can become a pull request; human approval cannot override a failed gate."))
        result = _read_json(row.get("result_path")) or {}
        gate_run = _read_json(result.get("gate_run_json"))
        diff = admitted_diff(result, gate_run)
        if not (result.get("admitted") and (gate_run or {}).get("final_state") == "ADMITTED" and diff and row.get("base_sha")):
            return void(self._comment(client, job, f"Cannot approve run `{rid}`: its admission evidence is missing on the server."))
        if not self.store.transition_issue_run(rid, ("admitted", "approved"), "approved", approver=job.requested_by):
            return void(self._comment(client, job, f"Run `{rid}` changed state; try again."))

        base, branch = row["base_sha"], row.get("default_branch") or "main"
        path = self.app.repos.prepare(job.owner, job.repo, token, [])
        if self._git(["cat-file", "-e", f"{base}^{{commit}}"], path).returncode != 0:
            path = self.app.repos.prepare(job.owner, job.repo, token, [base])
        moved_note = ""
        try:
            head = client.get_branch(job.owner, job.repo, branch)["commit"]["sha"]
            if head != base:
                moved_note = (f"Note: `{branch}` has moved since the verified base commit `{base[:12]}` (now `{head[:12]}`). "
                              "This PR is opened from the verified base commit; GitHub will show whether it merges cleanly.")
        except Exception:
            pass
        issue = client.get_issue(job.owner, job.repo, job.number)
        body = render_pr_body(row, result, _read_json(row.get("agent_run_path")), gate_run, job.requested_by, moved_note)
        publisher = _AppPublisher(token, client, job.owner, job.repo, branch)
        pr_branch = f"cerberus/issue-{job.number}-{rid}"
        res = publisher.publish_pr(issue_number=job.number, issue_title=issue.get("title") or "", repo_slug=job.slug,
                                   branch_name=pr_branch, pr_body=body, dry_run=False, diff_text=diff,
                                   source_repo_dir=path, base_commit=base)
        if res.get("status") != "published":
            err = redact_secrets(str(res.get("error") or "unknown error"), token)
            self.store.update_issue_run(rid, error=err[:2000])
            return void(self._comment(client, job, f"Approved by @{job.requested_by}, but opening the pull request failed "
                                                   f"(retry with `/cerberus approve {rid}`):\n\n{fence(err, limit=2000)}"))
        self.store.update_issue_run(rid, status="pr_opened", pr_url=res["pr_url"], pr_branch=res.get("branch") or pr_branch)
        kind = "Draft pull request" if publisher.draft else "Pull request (drafts unsupported here)"
        self._comment(client, job, f"Approved by @{job.requested_by}. {kind} opened: {res['pr_url']}\n\n"
                                   + (moved_note + "\n\n" if moved_note else "") + "Cerberus never merges; review it like any other PR.")

    def reject(self, job: "Job") -> None:
        _, client = self._client(job)
        row = self._resolve(client, job)
        if row is None:
            return
        rid = row["run_id"]
        if row["status"] == "pr_opened":
            return void(self._comment(client, job, f"Run `{rid}` already has a pull request ({row['pr_url']}); close it on GitHub."))
        if not self.store.transition_issue_run(rid, ("admitted", "approved"), "rejected",
                                               rejected_by=job.requested_by, review_note=job.comment_body[:500]):
            return void(self._comment(client, job, f"Nothing to reject: run `{rid}` is **{row['status']}**."))
        self._comment(client, job, f"Run `{rid}` rejected by @{job.requested_by}. No pull request will be opened.")


def void(_: Any) -> None:
    return None


def missing_repro_comment(problem: str) -> str:
    return f"Cerberus did not start a repair run. {problem}\n\n{REPRO_FORMAT_HELP}"
