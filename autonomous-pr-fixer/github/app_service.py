"""
Cerberus GitHub App: event routing, authorization, and run execution.

Triggers
  - pull_request `labeled` with the `cerberus` label, by a user with write access
  - pull_request `synchronize` / `reopened` on a PR that already carries the label
  - PR comment starting with `/cerberus verify`, by a user with write access
  - issue comment starting with `/cerberus repair`, by a user with write access,
    only when the server has a patch source configured

Verification of a pull request
  - The reproduction test is the single new test file the PR adds: it must fail
    on the base commit and pass with the PR. A PR without exactly one new test
    file is refused with guidance, without running anything.
  - Issue text comes from the issue the PR links with "Fixes #N", else the PR.
  - The result is a Check Run on the PR head commit. Nothing is pushed or merged.

Execution
  - Each run is a subprocess of the Cerberus CLI with a wall-clock timeout, so a
    hung run is killed rather than leaking a thread.
  - Runs in the same repository are serialized; runs per hour are capped.
  - The App refuses to run without the Docker sandbox.
  - Bot senders are ignored, so Cerberus cannot trigger itself.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from github.check_report import check_run_output
from github.client import GitHubClient
from github.pr_publisher import _auth_env
from github.run_store import RunStore
from harness.docker_sandbox import ISOLATION_DOCKER, host_unsafe_environment, resolve_isolation
from harness.scope_gate import is_test_path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRIGGER_LABEL = "cerberus"
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
_LINKED_ISSUE_RE = re.compile(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
_REPRO_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class AppSettings:
    webhook_secret: str
    app_id: str = ""
    private_key_pem: bytes = b""
    data_dir: str = os.path.join(ROOT, ".cerberus-app")
    max_runs_per_hour: int = 20
    run_timeout_seconds: int = 3600
    check_name: str = "Cerberus verification"
    repair_patch_source: str = "none"  # none | llm | claude-code
    publish_repairs: bool = False
    allow_host_sandbox_for_tests: bool = False

    @property
    def repos_dir(self) -> str:
        return os.path.join(self.data_dir, "repos")

    @property
    def artifacts_dir(self) -> str:
        return os.path.join(self.data_dir, "artifacts")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "runs.sqlite3")

    @classmethod
    def from_env(cls) -> "AppSettings":
        key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").encode("utf-8")
        key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
        if not key and key_path:
            with open(key_path, "rb") as f:
                key = f.read()
        return cls(
            webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
            app_id=os.environ.get("GITHUB_APP_ID", ""),
            private_key_pem=key,
            data_dir=os.environ.get("CERBERUS_DATA_DIR", os.path.join(ROOT, ".cerberus-app")),
            max_runs_per_hour=int(os.environ.get("CERBERUS_MAX_RUNS_PER_HOUR", "20")),
            run_timeout_seconds=int(os.environ.get("CERBERUS_RUN_TIMEOUT_SECONDS", "3600")),
            repair_patch_source=os.environ.get("CERBERUS_REPAIR_PATCH_SOURCE", "none"),
            publish_repairs=os.environ.get("CERBERUS_PUBLISH_REPAIRS", "0") == "1",
        )


@dataclass
class Job:
    run_id: str
    kind: str  # verify_pr | repair
    owner: str
    repo: str
    number: int
    installation_id: int
    requested_by: str
    head_sha: Optional[str] = None
    comment_body: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


Runner = Callable[[List[str], Dict[str, str], int, str], Tuple[int, bool]]


def _default_runner(argv: List[str], env: Dict[str, str], timeout: int, log_path: str) -> Tuple[int, bool]:
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            return proc.returncode, False
        except subprocess.TimeoutExpired:
            return -1, True


class RepoProvider:
    """Keeps a no-checkout clone per repository and fetches what a run needs."""

    def __init__(self, repos_dir: str):
        self.repos_dir = repos_dir

    def _git(self, args: List[str], cwd: Optional[str], token: str) -> subprocess.CompletedProcess:
        env = host_unsafe_environment()
        env.update(_auth_env(token))
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=900)

    def prepare(self, owner: str, repo: str, token: str, refs: List[str]) -> str:
        if not (_SAFE_NAME_RE.match(owner) and _SAFE_NAME_RE.match(repo)):
            raise RuntimeError("Unexpected repository name.")
        path = os.path.join(self.repos_dir, owner, repo)
        if not os.path.isdir(os.path.join(path, ".git")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            res = self._git(["clone", "--quiet", "--no-checkout", "-c", "core.autocrlf=false",
                             f"https://github.com/{owner}/{repo}.git", path], None, token)
            if res.returncode != 0:
                raise RuntimeError(f"git clone failed: {res.stderr.strip()[:300]}")
        for ref in refs:
            res = self._git(["fetch", "--quiet", "origin", ref], path, token)
            if res.returncode != 0:
                raise RuntimeError(f"git fetch {ref} failed: {res.stderr.strip()[:300]}")
        return path


class CerberusApp:
    def __init__(
        self,
        settings: AppSettings,
        store: RunStore,
        token_for_installation: Callable[[int], str],
        client_factory: Callable[[str], GitHubClient] = GitHubClient,
        repo_provider: Optional[RepoProvider] = None,
        runner: Runner = _default_runner,
    ):
        self.settings = settings
        self.store = store
        self.token_for_installation = token_for_installation
        self.client_factory = client_factory
        self.repos = repo_provider or RepoProvider(settings.repos_dir)
        self.runner = runner
        self._repo_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        os.makedirs(settings.artifacts_dir, exist_ok=True)

    # ------------------------------------------------------------ routing

    def route(self, event: str, delivery_id: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Job]]:
        """Decide what an event means. Returns (response, job to run or None)."""
        sender = payload.get("sender") or {}
        if sender.get("type") == "Bot":
            return {"status": "ignored", "reason": "events from bots are ignored"}, None
        repository = payload.get("repository") or {}
        installation = (payload.get("installation") or {}).get("id")
        full_name = repository.get("full_name", "")
        if not installation or "/" not in full_name:
            return {"status": "ignored", "reason": "not an App installation event"}, None
        owner, repo = full_name.split("/", 1)
        action = payload.get("action")

        job: Optional[Job] = None
        needs_permission = True
        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            labels = {lbl.get("name", "").lower() for lbl in pr.get("labels", [])}
            if action == "labeled" and (payload.get("label") or {}).get("name", "").lower() == TRIGGER_LABEL:
                pass
            elif action in ("synchronize", "reopened") and TRIGGER_LABEL in labels:
                # A maintainer opted the PR in by labelling it; new commits are re-verified.
                needs_permission = False
            else:
                return {"status": "ignored", "reason": "pull_request event without the cerberus trigger"}, None
            job = Job(self._new_run_id(), "verify_pr", owner, repo, int(pr["number"]), int(installation),
                      sender.get("login", ""), head_sha=(pr.get("head") or {}).get("sha"))
        elif event == "issue_comment" and action == "created":
            comment = payload.get("comment") or {}
            issue = payload.get("issue") or {}
            first_line = (comment.get("body") or "").strip().splitlines()[0:1]
            command = first_line[0].strip().lower() if first_line else ""
            actor = (comment.get("user") or {}).get("login", "")
            if command.startswith("/cerberus verify") and issue.get("pull_request"):
                job = Job(self._new_run_id(), "verify_pr", owner, repo, int(issue["number"]), int(installation), actor)
            elif command.startswith("/cerberus repair") and not issue.get("pull_request"):
                job = Job(self._new_run_id(), "repair", owner, repo, int(issue["number"]), int(installation), actor,
                          comment_body=comment.get("body") or "")
            else:
                return {"status": "ignored", "reason": "comment is not a /cerberus command"}, None
        else:
            return {"status": "ignored", "reason": f"unsupported event {event}/{action}"}, None

        if not self.store.claim_delivery(delivery_id, event):
            return {"status": "ignored", "reason": "duplicate delivery"}, None

        client = self.client_factory(self.token_for_installation(job.installation_id))
        if needs_permission:
            permission = client.collaborator_permission(owner, repo, job.requested_by)
            if permission not in WRITE_PERMISSIONS:
                return {"status": "ignored", "reason": f"{job.requested_by} does not have write access"}, None
        if job.kind == "repair" and self.settings.repair_patch_source == "none":
            client.create_issue_comment(owner, repo, job.number,
                                        "Cerberus repair is not enabled on this server (no patch source configured).")
            return {"status": "ignored", "reason": "repair is not enabled"}, None
        if self.store.runs_since(job.slug, 3600) >= self.settings.max_runs_per_hour:
            return {"status": "rate_limited", "reason": f"more than {self.settings.max_runs_per_hour} runs in the last hour"}, None

        self.store.create_run(job.run_id, delivery_id, job.slug, job.kind, job.number, job.head_sha, job.requested_by)
        return {"status": "queued", "run_id": job.run_id, "kind": job.kind}, job

    @staticmethod
    def _new_run_id() -> str:
        return f"app_{uuid.uuid4().hex[:12]}"

    def _lock_for(self, slug: str) -> threading.Lock:
        with self._locks_guard:
            return self._repo_locks.setdefault(slug, threading.Lock())

    # ---------------------------------------------------------- execution

    def execute(self, job: Job) -> None:
        with self._lock_for(job.slug):
            try:
                self._execute(job)
            except Exception as exc:  # recorded, never raised into the web server
                self.store.update_run(job.run_id, status="finished", final_state="ERROR", error=f"{type(exc).__name__}: {exc}"[:2000])

    def _execute(self, job: Job) -> None:
        import time

        token = self.token_for_installation(job.installation_id)
        client = self.client_factory(token)
        self.store.update_run(job.run_id, status="running", started_at=time.time())

        if job.kind == "verify_pr":
            pr = client.get_pull(job.owner, job.repo, job.number)
            job.head_sha = pr["head"]["sha"]
            base_sha = pr["base"]["sha"]
            check_id = client.create_check_run(job.owner, job.repo, job.head_sha, self.settings.check_name, job.run_id)
            self.store.update_run(job.run_id, check_run_id=check_id, head_sha=job.head_sha)

            refusal = self._sandbox_refusal()
            if refusal:
                self._finish_check(client, job, check_id, "neutral", "Cannot verify safely", refusal, "ERROR")
                return
            path = self.repos.prepare(job.owner, job.repo, token, [base_sha, f"+refs/pull/{job.number}/head:refs/cerberus/pr/{job.number}"])
            repro_path, guidance = self._pr_reproduction_test(path, base_sha, job.head_sha, job.run_id)
            if repro_path is None:
                self._finish_check(client, job, check_id, "failure", "Refused: no reproduction test", guidance, "REFUSED", "NO_REPRODUCTION_TEST")
                return
            title, body = self._issue_text(client, job, pr)
            argv = self._cli(path, job, title, body) + ["--repro-test", repro_path, "--base", base_sha, "--head", job.head_sha]
            artifact, timed_out = self._run_cli(job, argv, token=None)
            conclusion, check_title, summary, text = check_run_output(artifact, timed_out=timed_out)
            client.complete_check_run(job.owner, job.repo, check_id, conclusion, check_title, summary, text)
            self._record_outcome(job, artifact, timed_out)
            return

        # repair
        refusal = self._sandbox_refusal()
        if refusal:
            client.create_issue_comment(job.owner, job.repo, job.number, f"Cerberus cannot run: {refusal}")
            self.store.update_run(job.run_id, status="finished", final_state="ERROR", error=refusal)
            return
        issue = client.get_issue(job.owner, job.repo, job.number)
        match = _REPRO_BLOCK_RE.search(job.comment_body) or _REPRO_BLOCK_RE.search(issue.get("body") or "")
        if not match:
            client.create_issue_comment(job.owner, job.repo, job.number,
                                        "Cerberus refused: add a failing reproduction test as a ```python code block to the issue or the command comment.")
            self.store.update_run(job.run_id, status="finished", final_state="REFUSED", refusal_code="NO_REPRODUCTION_TEST")
            return
        path = self.repos.prepare(job.owner, job.repo, token, ["HEAD"])
        subprocess.run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=path, capture_output=True, env=host_unsafe_environment())
        repro_path = self._write_run_file(job.run_id, "test_reproduce.py", match.group(1))
        argv = self._cli(path, job, issue.get("title", ""), issue.get("body") or "") + ["--repro-test", repro_path]
        argv += ["--use-llm"] if self.settings.repair_patch_source == "llm" else ["--agent", self.settings.repair_patch_source]
        if self.settings.publish_repairs:
            argv += ["--mode", "github"]
        else:
            argv += ["--dry-run"]
        artifact, timed_out = self._run_cli(job, argv, token=token if self.settings.publish_repairs else None)
        conclusion, check_title, summary, _ = check_run_output(artifact, timed_out=timed_out)
        pr_url = artifact.get("pr_url")
        suffix = f"\n\nDraft pull request: {pr_url}" if pr_url else ""
        client.create_issue_comment(job.owner, job.repo, job.number, f"### Cerberus: {check_title}\n\n{summary}{suffix}")
        self._record_outcome(job, artifact, timed_out)

    # ------------------------------------------------------------ helpers

    def _sandbox_refusal(self) -> Optional[str]:
        try:
            isolation = resolve_isolation()
        except Exception as exc:
            return str(exc)
        if isolation != ISOLATION_DOCKER and not self.settings.allow_host_sandbox_for_tests:
            return "the GitHub App only runs repository code in the Docker sandbox"
        return None

    def _run_dir(self, run_id: str) -> str:
        path = os.path.join(self.settings.data_dir, "work", run_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _write_run_file(self, run_id: str, name: str, content: str) -> str:
        path = os.path.join(self._run_dir(run_id), name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path

    def _pr_reproduction_test(self, repo_path: str, base: str, head: str, run_id: str) -> Tuple[Optional[str], str]:
        env = host_unsafe_environment()
        res = subprocess.run(
            ["git", "diff", "--no-renames", "--name-status", "--no-ext-diff", base, head],
            cwd=repo_path, capture_output=True, text=True, env=env,
        )
        added_tests = [
            line.split("\t", 1)[1] for line in res.stdout.splitlines()
            if line.startswith("A\t") and is_test_path(line.split("\t", 1)[1]) and line.endswith(".py")
            and not line.split("\t", 1)[1].endswith("conftest.py")
        ]
        if len(added_tests) != 1:
            return None, (
                f"Cerberus verifies a pull request against exactly one new test file that fails without the change "
                f"and passes with it. This PR adds {len(added_tests)} new test file(s)"
                + (f": {', '.join(added_tests)}" if added_tests else "") + "."
            )
        show = subprocess.run(["git", "show", f"{head}:{added_tests[0]}"], cwd=repo_path, capture_output=True, text=True, env=env)
        if show.returncode != 0:
            return None, f"Could not read {added_tests[0]} at {head}."
        return self._write_run_file(run_id, "test_reproduce.py", show.stdout), ""

    def _issue_text(self, client: GitHubClient, job: Job, pr: Dict[str, Any]) -> Tuple[str, str]:
        match = _LINKED_ISSUE_RE.search(pr.get("body") or "")
        if match:
            try:
                issue = client.get_issue(job.owner, job.repo, int(match.group(1)))
                return issue.get("title", ""), issue.get("body") or ""
            except Exception:
                pass
        return pr.get("title", ""), pr.get("body") or ""

    def _cli(self, repo_path: str, job: Job, title: str, body: str) -> List[str]:
        return [sys.executable, os.path.join(ROOT, "main.py"), "--repo", repo_path, "--run-id", job.run_id,
                "--issue", str(job.number), "--title", title or f"#{job.number}", "--body", body]

    def _run_cli(self, job: Job, argv: List[str], token: Optional[str]) -> Tuple[Dict[str, Any], bool]:
        import json

        env = host_unsafe_environment()
        env["RUN_ARTIFACTS_DIR"] = self.settings.artifacts_dir
        if os.environ.get("CERBERUS_SANDBOX"):
            env["CERBERUS_SANDBOX"] = os.environ["CERBERUS_SANDBOX"]
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CERBERUS_MODEL", "CERBERUS_SANDBOX_IMAGE"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if token:
            env["GITHUB_TOKEN"] = token
            env["GITHUB_REPO_SLUG"] = job.slug
        log_path = os.path.join(self._run_dir(job.run_id), "cli.log")
        _, timed_out = self.runner(argv, env, self.settings.run_timeout_seconds, log_path)
        artifact_path = os.path.join(self.settings.artifacts_dir, job.run_id, "run.json")
        if os.path.isfile(artifact_path):
            with open(artifact_path, "r", encoding="utf-8") as f:
                artifact = json.load(f)
        else:
            artifact = {"final_state": "ERROR", "refusal": {"stage": "WORKER", "message": "the run produced no artifact"}}
        artifact["_artifact_path"] = artifact_path if os.path.isfile(artifact_path) else None
        return artifact, timed_out

    def _record_outcome(self, job: Job, artifact: Dict[str, Any], timed_out: bool) -> None:
        import time

        refusal = artifact.get("refusal") or {}
        self.store.update_run(
            job.run_id,
            status="finished",
            final_state="ERROR" if timed_out else artifact.get("final_state", "ERROR"),
            refusal_code=refusal.get("code"),
            artifact_path=artifact.get("_artifact_path"),
            error="timed out" if timed_out else (refusal.get("message") if artifact.get("final_state") == "ERROR" else None),
            finished_at=time.time(),
        )

    def _finish_check(self, client: GitHubClient, job: Job, check_id: int, conclusion: str, title: str, message: str,
                      final_state: str, code: Optional[str] = None) -> None:
        import time

        client.complete_check_run(job.owner, job.repo, check_id, conclusion, title, message)
        self.store.update_run(job.run_id, status="finished", final_state=final_state, refusal_code=code,
                              error=message if final_state == "ERROR" else None, finished_at=time.time())
