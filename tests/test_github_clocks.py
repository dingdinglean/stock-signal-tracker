"""Authenticated clock reads, rate-limit halt, and fail-closed coverage."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

from research.github_api import GitHubPublicArtifacts, GitHubRateLimitError
from research.run import execute, load_run_index


class _Response:
    def __init__(self, status: int, body: str, headers: dict | None = None, payload: dict | None = None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}
        self.ok = 200 <= status < 300
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_authenticated_request_sends_bearer_and_hides_the_token(monkeypatch):
    seen = []

    def fake_get(url, headers, timeout, allow_redirects):
        seen.append(headers)
        return _Response(200, "", payload={"ok": True})

    monkeypatch.setattr(requests, "get", fake_get)
    client = GitHubPublicArtifacts(token="super-secret-token", sleeper=lambda _seconds: None)
    response = client._get("https://api.github.com/repos/dingdinglean/gupiao/actions/runs/1")
    assert response.json()["ok"] is True
    assert seen[0]["Authorization"] == "Bearer super-secret-token"
    assert "super-secret-token" not in json.dumps({key: value for key, value in seen[0].items() if key != "Authorization"})


def test_permission_denial_falls_back_once_and_does_not_invent_a_clock(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout, allow_redirects):
        calls.append("auth" if "Authorization" in headers else "anon")
        if "Authorization" in headers:
            return _Response(403, "Resource not accessible by integration")
        return _Response(200, "", payload={"created_at": "2026-07-06T12:00:00Z"})

    monkeypatch.setattr(requests, "get", fake_get)
    client = GitHubPublicArtifacts(token="super-secret-token", sleeper=lambda _seconds: None)
    first = client._get("https://api.github.com/repos/dingdinglean/gupiao/actions/runs/1")
    second = client._get("https://api.github.com/repos/dingdinglean/gupiao/actions/runs/2")
    assert first.json()["created_at"].startswith("2026-07-06")
    assert calls == ["auth", "anon", "anon"]
    assert client.cross_repo_denied is True
    assert "super-secret-token" not in second.text if hasattr(second, "text") else True


def test_rate_limit_retries_then_halts_without_dropping_a_cached_clock(tmp_path: Path, monkeypatch):
    waits = []

    def fake_get(url, headers, timeout, allow_redirects):
        return _Response(429, "abuse detection", headers={"Retry-After": "9"})

    monkeypatch.setattr(requests, "get", fake_get)
    client = GitHubPublicArtifacts(token="super-secret-token", sleeper=waits.append, max_rate_retries=2)
    try:
        client._get("https://api.github.com/repos/dingdinglean/gupiao/actions/runs/9")
    except GitHubRateLimitError as exc:
        assert "super-secret-token" not in str(exc)
    else:
        raise AssertionError("rate limit returned a response")
    assert waits == [9.0, 9.0]
    assert client.halted is True
    cache = tmp_path / "run_times.json"
    cache.write_text(json.dumps({"runs": {"9": {
        "created_at": "2026-07-06T12:00:00Z",
        "job_id": "99",
        "job_name": "scan",
        "job_conclusion": "success",
        "job_completed_at": "2026-07-06T20:05:00Z",
        "job_completed_source": "actions_job_completed_at",
    }}}), encoding="utf-8")
    found, errors = load_run_index(cache, fetch=True, needed={"9", "10"}, client=client)
    assert found["9"]["job_completed_at"] == "2026-07-06T20:05:00Z"
    assert any("rate limit" in error for error in errors)


def test_seed_clock_is_used_when_the_network_is_off(tmp_path: Path):
    seed = {"43260511273": {
        "created_at": "2026-11-12T16:36:27Z",
        "run_started_at": "2026-11-12T16:36:27Z",
        "job_id": "131330182973",
        "job_name": "scan",
        "job_conclusion": "success",
        "job_completed_at": "2026-11-12T16:38:47Z",
        "job_completed_source": "actions_job_completed_at",
    }}
    found, errors = load_run_index(tmp_path / "missing.json", fetch=False, needed={"43260511273"}, seed=seed)
    assert errors == []
    assert found["43260511273"]["job_id"] == "131330182973"
    text = (Path(__file__).resolve().parents[1] / "research" / "source_clocks.json").read_text(encoding="utf-8")
    assert "ghs_" not in text and "ghp_" not in text and "Bearer" not in text
    assert "131330182973" in text
    assert "run-radar" in text


def test_dry_run_fails_closed_when_every_study_clock_is_missing(tmp_path: Path):
    from tests.test_research_asof_caller import _write_history, _write_prices

    _write_history(tmp_path / "data" / "signal_history.csv", "signal-run")
    (tmp_path / "data" / "sector_snapshots.csv").write_text("market_date,run_id,run_created_at\n", encoding="utf-8")
    _write_prices(tmp_path)
    code, coverage = execute(tmp_path, full=False, fetch=False, today=date(2026, 8, 20))
    assert code == 1
    assert coverage["quality_gate"].startswith("fail:")
    assert "missing_asof:1" in coverage["quality_gate"]
    assert "empty_simulations" in coverage["quality_gate"]
    ledger = (tmp_path / "data" / "signal_history.csv").read_text(encoding="utf-8")
    assert "caller-1" in ledger
