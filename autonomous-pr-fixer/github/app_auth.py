"""
GitHub App authentication.

A GitHub App authenticates as itself with a short-lived RS256 JWT, then exchanges
it for an installation access token scoped to one installation's repositories
and the App's granted permissions. Installation tokens expire after an hour;
they are cached until shortly before expiry and never written to disk.

Required App permissions (least privilege for Cerberus):
  - Checks: read & write        (report verification results on pull requests)
  - Pull requests: read & write (read PRs; open draft PRs for admitted repairs)
  - Issues: read & write        (read issues; comment with refusals)
  - Contents: read & write      (clone; push the branch of an admitted repair)
  - Metadata: read              (mandatory)
Workflows permission is deliberately not requested: Cerberus never modifies
workflow files, and without the permission GitHub rejects such pushes too.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_URL = "https://api.github.com"


class GitHubAppAuthError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, private_key_pem: bytes, now: Optional[int] = None) -> str:
    """A JWT identifying the App, valid for 9 minutes (GitHub allows at most 10)."""
    issued = int(now if now is not None else time.time()) - 60  # tolerate clock drift
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued, "exp": issued + 600 - 60, "iss": str(app_id)}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAppAuthError("GitHub App private key could not be loaded.") from exc
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


@dataclass
class _CachedToken:
    token: str
    expires_at: float


HttpPost = Callable[[str, Dict[str, str]], Dict[str, object]]


class InstallationTokenProvider:
    """Mints and caches installation access tokens for a GitHub App."""

    def __init__(self, app_id: str, private_key_pem: bytes, http_post: Optional[HttpPost] = None, api_url: str = API_URL):
        if not app_id or not private_key_pem:
            raise GitHubAppAuthError("GITHUB_APP_ID and a GitHub App private key are required.")
        self.app_id = app_id
        self._key = private_key_pem
        self._post = http_post or _default_post
        self._api = api_url.rstrip("/")
        self._cache: Dict[int, _CachedToken] = {}
        self._lock = threading.Lock()

    def token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached and cached.expires_at - time.time() > 300:
                return cached.token
            headers = {
                "Authorization": f"Bearer {app_jwt(self.app_id, self._key)}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Cerberus-Verification-Gate",
            }
            data = self._post(f"{self._api}/app/installations/{int(installation_id)}/access_tokens", headers)
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise GitHubAppAuthError("GitHub did not return an installation token.")
            expires = _parse_expiry(str(data.get("expires_at", "")))
            self._cache[installation_id] = _CachedToken(token, expires)
            return token


def _parse_expiry(value: str) -> float:
    import datetime as _dt

    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return time.time() + 1800


def _default_post(url: str, headers: Dict[str, str]) -> Dict[str, object]:
    import httpx

    resp = httpx.post(url, headers=headers, timeout=30)
    if resp.status_code >= 300:
        raise GitHubAppAuthError(f"Installation token request failed with HTTP {resp.status_code}.")
    return resp.json()
