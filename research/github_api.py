"""Read public Actions clocks without logging a token or inventing a timestamp."""
from __future__ import annotations

import os
import time
from typing import Callable

import requests


class GitHubArtifactError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubArtifactError):
    """The public or token quota is exhausted. Callers must keep cached clocks."""


class GitHubPublicArtifacts:
    """REST client for public workflow runs.

    A workflow ``GITHUB_TOKEN`` is sent when present. GitHub scopes that token
    to the repository that owns the workflow, so a cross-repo Actions request
    can return "Resource not accessible by integration". That response is not a
    clock. One anonymous retry is allowed for public data, and later requests
    skip the token. Rate limits and abuse responses wait for ``Retry-After``
    and then stop the client. The token is never included in an error string.
    """

    def __init__(
        self,
        token: str | None = None,
        timeout: int = 30,
        sleeper: Callable[[float], None] | None = None,
        max_rate_retries: int = 2,
        max_wait: float = 60.0,
    ) -> None:
        self.token = token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        self.sleeper = sleeper or time.sleep
        self.max_rate_retries = max_rate_retries
        self.max_wait = max_wait
        self.cross_repo_denied = False
        self.halted = False
        self.base_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stock-signal-tracker/1.0",
        }

    def _headers(self, authenticated: bool) -> dict[str, str]:
        headers = dict(self.base_headers)
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _public(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "[redacted]")
        return text

    @staticmethod
    def _permission_denied(response: requests.Response) -> bool:
        return response.status_code == 403 and "Resource not accessible by integration" in response.text

    @staticmethod
    def _rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        body = response.text.lower()
        return "rate limit" in body or "abuse detection" in body

    def _delay_seconds(self, response: requests.Response, attempt: int) -> float:
        raw = str(response.headers.get("Retry-After") or "").strip()
        if not raw:
            return float(2 ** attempt)
        try:
            return float(raw)
        except ValueError:
            self.halted = True
            raise GitHubRateLimitError(
                "Retry-After is not a number of seconds; not retrying before the server's indicated time"
            )

    def _wait(self, response: requests.Response, attempt: int) -> None:
        delay = self._delay_seconds(response, attempt)
        if delay > self.max_wait:
            self.halted = True
            raise GitHubRateLimitError(
                f"server asked to wait {delay:.0f}s; not retrying before that interval"
            )
        self.sleeper(max(delay, 0.0))

    def _get(self, url: str, *, binary: bool = False) -> requests.Response:
        if self.halted:
            raise GitHubRateLimitError(f"GET {url} skipped after the rate limit halted this client")
        use_token = bool(self.token) and not self.cross_repo_denied
        rate_attempts = 0
        last: requests.Response | None = None
        while True:
            authenticated = use_token
            try:
                response = requests.get(
                    url,
                    headers=self._headers(authenticated),
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise GitHubArtifactError(f"GET {url} network failure: {exc.__class__.__name__}") from exc
            last = response
            if response.ok:
                return response
            if self._permission_denied(response) and authenticated:
                self.cross_repo_denied = True
                use_token = False
                continue
            if self._rate_limited(response) and rate_attempts < self.max_rate_retries:
                rate_attempts += 1
                self._wait(response, rate_attempts)
                continue
            break
        assert last is not None
        detail = self._public(last.text[:300])
        if self._rate_limited(last):
            self.halted = True
            raise GitHubRateLimitError(f"GET {url} failed: {last.status_code} {detail}")
        if self._permission_denied(last):
            detail += (
                " GITHUB_TOKEN only covers the workflow repository. "
                "Reading Actions jobs in dingdinglean/gupiao or dingdinglean/strong-pullback-screener "
                "needs a GitHub App installed on those repositories with Actions read-only, "
                "passed only as an environment variable. A classic PAT is not required, and "
                "widening this workflow token does not grant that access."
            )
        raise GitHubArtifactError(f"GET {url} failed: {last.status_code} {detail}")
