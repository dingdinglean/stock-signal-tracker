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


def test_retry_after_longer_than_the_budget_is_not_shortened(monkeypatch):
    waits = []

    def fake_get(url, headers, timeout, allow_redirects):
        return _Response(429, "abuse detection", headers={"Retry-After": "120"})

    monkeypatch.setattr(requests, "get", fake_get)
    client = GitHubPublicArtifacts(token="super-secret-token", sleeper=waits.append, max_wait=60)
    try:
        client._get("https://api.github.com/repos/dingdinglean/gupiao/actions/runs/9")
    except GitHubRateLimitError as exc:
        assert "120" in str(exc)
        assert "super-secret-token" not in str(exc)
    else:
        raise AssertionError("a long Retry-After was retried early")
    assert waits == []
    assert client.halted is True


def test_new_unseeded_signal_without_a_job_clock_fails_even_when_older_sims_exist():
    from datetime import datetime, timezone

    from research.market import xnys
    from research.run import _quality_gate

    old = {
        "signal_id": "old", "symbol": "IDXX", "asof_time": "2026-07-06T12:00:00+00:00",
        "entry_time": "2026-07-06T20:05:00+00:00", "entry_job_id": "1",
        "entry_job_source": "actions_job_completed_at", "entry_job_conclusion": "success",
    }
    new = {
        "signal_id": "new", "symbol": "TRGP", "asof_time": "2026-11-12T16:36:27+00:00",
        "entry_time": "", "entry_job_id": "", "entry_job_source": "", "entry_job_conclusion": "",
    }
    sims = [{"signal_id": "old", "status": "closed"}]
    charts = [{"signal_id": "old", "title": "IDXX", "png": b"png", "draw_status": "drawn", "draw_reason": ""}]
    reasons, lines, _charts = _quality_gate(
        [old, new], sims, charts, now=datetime(2026, 11, 13, 4, 41, tzinfo=timezone.utc), calendar=xnys(2026, 2027),
    )
    assert any(reason == "missing_entry_clock:new" for reason in reasons)
    assert "empty_simulations" not in " ".join(reasons)
    assert any(line.startswith("new TRGP") and "missing_entry_clock" in line for line in lines)


def test_pending_next_open_is_not_a_missing_clock():
    from datetime import datetime, timezone

    from research.market import xnys
    from research.run import _quality_gate

    row = {
        "signal_id": "trgp", "symbol": "TRGP", "asof_time": "2026-11-12T16:36:27+00:00",
        "entry_time": "2026-11-12T16:38:47+00:00", "entry_job_id": "131330182973",
        "entry_job_source": "actions_job_completed_at", "entry_job_conclusion": "success",
    }
    sims = [{"signal_id": "trgp", "status": "missing_entry", "exit_reason": "entry_session_missing", "entry_date": "2026-11-13"}]
    reasons, lines, _charts = _quality_gate(
        [row], sims, [], now=datetime(2026, 11, 13, 4, 41, tzinfo=timezone.utc), calendar=xnys(2026, 2027),
    )
    assert reasons == []
    assert any("pending_next_open" in line for line in lines)
    later, _lines, _charts = _quality_gate(
        [row], sims, [], now=datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc), calendar=xnys(2026, 2027),
    )
    assert any(reason == "entry_bar_missing:trgp" for reason in later)


def test_one_missing_priority_png_fails_when_another_chart_exists():
    from datetime import datetime, timezone

    from research.market import xnys
    from research.run import _quality_gate

    row = {
        "signal_id": "old", "symbol": "IDXX", "asof_time": "2026-07-06T12:00:00+00:00",
        "entry_time": "2026-07-06T20:05:00+00:00", "entry_job_id": "1",
        "entry_job_source": "actions_job_completed_at", "entry_job_conclusion": "success",
    }
    sims = [{"signal_id": "old", "status": "closed"}]
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    calendar = xnys(2026, 2027)
    missing, _signals, charts = _quality_gate(
        [row], sims,
        [
            {"signal_id": "idxx", "title": "IDXX", "png": b"png", "draw_status": "drawn", "draw_reason": ""},
            {"signal_id": "alle", "title": "ALLE", "png": None},
        ],
        now=now, calendar=calendar,
    )
    assert any(reason == "missing_png:alle" for reason in missing)
    assert any("ALLE missing_png" in line or line.startswith("alle missing_png") for line in charts)
    allowed, _signals, allowed_charts = _quality_gate(
        [row], sims,
        [
            {"signal_id": "idxx", "png": b"png", "draw_status": "drawn", "draw_reason": ""},
            {"signal_id": "alle", "png": None, "draw_status": "undrawable", "draw_reason": "信号前交易日 12 根，不足 60，不绘制案例图"},
        ],
        now=now, calendar=calendar,
    )
    assert not any(reason.startswith("missing_png:") for reason in allowed)
    assert any("undrawable" in line for line in allowed_charts)


def test_jobs_rate_limit_after_run_metadata_keeps_the_new_signal_without_an_entry(tmp_path: Path):
    from research.github_api import GitHubRateLimitError
    from research.run import load_run_index

    class _Client:
        halted = False

        def _get(self, url: str):
            if url.endswith("/jobs?per_page=100"):
                self.halted = True
                raise GitHubRateLimitError("GET jobs failed: 429")
            return _Response(200, "", payload={
                "created_at": "2026-11-12T16:36:27Z", "run_started_at": "2026-11-12T16:36:27Z", "id": "new-run",
            })

    cache = tmp_path / "run_times.json"
    cache.write_text(json.dumps({"runs": {"old-run": {
        "created_at": "2026-07-06T12:00:00Z", "run_started_at": "2026-07-06T12:00:00Z",
        "job_id": "7", "job_name": "scan", "job_conclusion": "success",
        "job_completed_at": "2026-07-06T20:05:00Z", "job_completed_source": "actions_job_completed_at",
    }}}), encoding="utf-8")
    found, errors = load_run_index(cache, fetch=True, needed={"old-run", "new-run"}, client=_Client())
    assert found["old-run"]["job_completed_at"] == "2026-07-06T20:05:00Z"
    assert found["new-run"]["created_at"] == "2026-11-12T16:36:27Z"
    assert not found["new-run"].get("job_completed_at")
    assert errors


def test_dry_run_fails_closed_when_every_study_clock_is_missing(tmp_path: Path):
    from tests.test_research_asof_caller import _write_history, _write_prices

    _write_history(tmp_path / "data" / "signal_history.csv", "signal-run")
    (tmp_path / "data" / "sector_snapshots.csv").write_text("market_date,run_id,run_created_at\n", encoding="utf-8")
    _write_prices(tmp_path)
    code, coverage = execute(tmp_path, full=False, fetch=False, today=date(2026, 8, 20))
    assert code == 1
    assert coverage["quality_gate"].startswith("fail:")
    assert "missing_asof:caller-1" in coverage["quality_gate"]
    assert "caller-1 TEST" in coverage["signal_clock_coverage"]
    ledger = (tmp_path / "data" / "signal_history.csv").read_text(encoding="utf-8")
    assert "caller-1" in ledger
