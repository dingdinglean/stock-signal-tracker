from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests


API_ROOT = "https://api.github.com"


class GitHubArtifactError(RuntimeError):
    pass


class GitHubPublicArtifacts:
    """Small GitHub REST client for public workflow runs/artifacts.

    If a token is present we try it first.  Cross-repo GitHub Actions tokens can
    be more restrictive than anonymous public access, so a 401/403 response is
    retried once without Authorization.
    """

    def __init__(self, token: str | None = None, timeout: int = 30) -> None:
        self.token = token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
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

    def _get(self, url: str, *, binary: bool = False) -> requests.Response:
        attempts = [True, False] if self.token else [False]
        last: requests.Response | None = None
        for authenticated in attempts:
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
            if response.status_code in (401, 403) and authenticated:
                continue
            if response.ok:
                return response
            break
        assert last is not None
        raise GitHubArtifactError(f"GET {url} failed: {last.status_code} {last.text[:300]}")

    def workflow_runs(
        self,
        repo: str,
        workflow_file: str,
        *,
        since: datetime | None = None,
        per_page: int = 100,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        workflow = quote(workflow_file, safe="")
        runs: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            url = (
                f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}/runs"
                f"?status=success&per_page={per_page}&page={page}"
            )
            payload = self._get(url).json()
            batch = payload.get("workflow_runs", [])
            if not batch:
                break
            for run in batch:
                created = parse_github_time(run.get("created_at"))
                if since and created and created < since:
                    continue
                runs.append(run)
            if len(batch) < per_page:
                break
            if since:
                oldest = min(
                    (parse_github_time(item.get("created_at")) for item in batch),
                    default=None,
                )
                if oldest and oldest < since:
                    break
        return sorted(runs, key=lambda item: item.get("created_at", ""))

    def artifact_archive(self, repo: str, run_id: int, artifact_name: str) -> dict[str, bytes] | None:
        url = f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
        payload = self._get(url).json()
        artifacts = payload.get("artifacts", [])
        target = next(
            (
                item
                for item in artifacts
                if item.get("name") == artifact_name and not item.get("expired", False)
            ),
            None,
        )
        if not target:
            return None
        download_url = target.get("archive_download_url")
        if not download_url:
            return None
        raw = self._get(download_url, binary=True).content
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                return {name: zf.read(name) for name in zf.namelist() if not name.endswith("/")}
        except zipfile.BadZipFile as exc:
            raise GitHubArtifactError(f"Artifact {artifact_name} from run {run_id} is not a zip") from exc


def parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_member(archive: dict[str, bytes], basename: str) -> bytes | None:
    for name, content in archive.items():
        if name.rsplit("/", 1)[-1] == basename:
            return content
    return None
