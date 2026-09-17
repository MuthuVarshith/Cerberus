"""
Minimal GitHub REST client for the Cerberus GitHub App.

Only the calls Cerberus needs. The HTTP transport is injectable so the App's
behaviour can be tested without network access; error bodies are redacted
before they are raised.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from github.pr_publisher import redact_secrets

API_URL = "https://api.github.com"
MAX_CHECK_TEXT = 65535

Transport = Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]]], Tuple[int, Any]]


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status


def _default_transport(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]]) -> Tuple[int, Any]:
    import httpx

    resp = httpx.request(method, url, headers=headers, json=body, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        data = {"message": resp.text[:500]}
    return resp.status_code, data


def _clip(text: str, limit: int = MAX_CHECK_TEXT) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n… truncated by Cerberus to fit GitHub's limit; see the run artifact for the full record."
    return text[: limit - len(marker)] + marker


class GitHubClient:
    def __init__(self, token: str, transport: Optional[Transport] = None, api_url: str = API_URL):
        self._token = token
        self._transport = transport or _default_transport
        self._api = api_url.rstrip("/")

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Cerberus-Verification-Gate",
        }
        status, data = self._transport(method, f"{self._api}{path}", headers, body)
        if status >= 300:
            message = data.get("message", "") if isinstance(data, dict) else str(data)
            raise GitHubAPIError(status, redact_secrets(str(message), self._token))
        return data

    def get_pull(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        return self._call("GET", f"/repos/{owner}/{repo}/pulls/{int(number)}")

    def get_issue(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        return self._call("GET", f"/repos/{owner}/{repo}/issues/{int(number)}")

    def collaborator_permission(self, owner: str, repo: str, username: str) -> str:
        try:
            data = self._call("GET", f"/repos/{owner}/{repo}/collaborators/{username}/permission")
        except GitHubAPIError as exc:
            if exc.status == 404:
                return "none"
            raise
        return str(data.get("permission", "none"))

    def create_check_run(self, owner: str, repo: str, head_sha: str, name: str, external_id: str) -> int:
        data = self._call("POST", f"/repos/{owner}/{repo}/check-runs", {
            "name": name, "head_sha": head_sha, "status": "in_progress", "external_id": external_id,
        })
        return int(data["id"])

    def complete_check_run(self, owner: str, repo: str, check_run_id: int, conclusion: str, title: str, summary: str, text: str = "") -> None:
        body: Dict[str, Any] = {
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title[:255], "summary": _clip(summary)},
        }
        if text:
            body["output"]["text"] = _clip(text)
        self._call("PATCH", f"/repos/{owner}/{repo}/check-runs/{int(check_run_id)}", body)

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._call("POST", f"/repos/{owner}/{repo}/issues/{int(number)}/comments", {"body": _clip(body)})
