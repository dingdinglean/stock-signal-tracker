from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import publish_changes
import tracker
from publish_changes import PublishConflict, merge_history_rows, merge_metadata, merge_snapshots, merge_state


def _row(signal_id: str, **updates: str) -> dict[str, str]:
    row = {field: "" for field in tracker.HISTORY_FIELDS}
    row.update({
        "signal_id": signal_id,
        "symbol": "AMD",
        "signal_date": "2026-09-08",
        "push_date": "2026-09-08",
        "push_price": "100.00",
        "effectiveness": "pending",
        "last_updated": "2026-10-01T00:00:00+00:00",
    })
    row.update(updates)
    return row


def _snapshot(market_date: str, created: str, theme: str = "半导体") -> dict[str, str]:
    return {"market_date": market_date, "run_id": "1", "run_created_at": created, "rank1_theme": theme, "rank2_theme": "", "rank3_theme": ""}


def _state(ids: list[int], start: str = "2026-09-06T00:00:00Z") -> dict:
    return {
        "processed_gupiao_run_ids": ids,
        "processed_long_gupiao_run_ids": [],
        "processed_strength_run_ids": [],
        "tracking_start_utc": start,
    }


def test_fast_forward_merge_keeps_local_performance_update_and_new_signal():
    base = [_row("a", return_5d="")]
    local = [_row("a", return_5d="1.5", last_updated="2026-10-02T00:00:00+00:00"), _row("b")]
    merged = merge_history_rows(base, base, local)
    assert [row["signal_id"] for row in merged] == ["a", "b"]
    assert merged[0]["return_5d"] == "1.5"
    assert merged[0]["push_price"] == "100.00"
    assert merged[0]["last_updated"] == "2026-10-02T00:00:00+00:00"


def test_union_keeps_rows_from_both_sides_and_existing_spelling():
    base = [_row("a")]
    upstream = [_row("a"), _row("c", symbol="NVDA")]
    local = [_row("a", push_price="100"), _row("b", symbol="META")]
    merged = merge_history_rows(base, upstream, local)
    assert [row["signal_id"] for row in merged] == ["a", "c", "b"]
    assert merged[0]["push_price"] == "100.00"
    assert {row["symbol"] for row in merged} == {"AMD", "NVDA", "META"}


def test_dropped_history_row_is_restored():
    base = [_row("a"), _row("b")]
    merged = merge_history_rows(base, [_row("a")], [_row("a")])
    assert {row["signal_id"] for row in merged} == {"a", "b"}


def test_divergent_frozen_field_fails_loudly():
    base = [_row("a", push_price="100")]
    upstream = [_row("a", push_price="101")]
    local = [_row("a", push_price="102")]
    with pytest.raises(PublishConflict, match="push_price"):
        merge_history_rows(base, upstream, local)


def test_state_union_never_drops_ids_and_start_conflict_fails():
    base = _state([1])
    upstream = _state([1, 2])
    local = _state([1, 3])
    assert merge_state(base, upstream, local)["processed_gupiao_run_ids"] == [1, 2, 3]
    with pytest.raises(PublishConflict, match="tracking_start_utc"):
        merge_state(base, upstream, _state([1], start="2026-01-01T00:00:00Z"))


def test_snapshot_dates_are_unioned_and_equal_timestamps_do_not_guess():
    base = [_snapshot("2026-09-08", "2026-09-08T01:00:00Z", "能源")]
    upstream = [_snapshot("2026-09-08", "2026-09-08T01:00:00Z", "能源"), _snapshot("2026-09-09", "2026-09-09T01:00:00Z")]
    local = [_snapshot("2026-09-08", "2026-09-08T03:00:00Z", "半导体"), _snapshot("2026-09-10", "2026-09-10T01:00:00Z")]
    merged = merge_snapshots(base, upstream, local)
    assert [row["market_date"] for row in merged] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    assert merged[0]["rank1_theme"] == "半导体"
    conflicted = [_snapshot("2026-09-08", "2026-09-08T02:00:00Z", "云软件")]
    other = [_snapshot("2026-09-08", "2026-09-08T02:00:00Z", "半导体")]
    with pytest.raises(PublishConflict, match="2026-09-08"):
        merge_snapshots(base, conflicted, other)


def test_metadata_merge_keeps_a_usable_cache_and_prefers_the_larger_one():
    old = {"updated_at": "2026-10-01T00:00:00+00:00", "symbols": {"AAPL": {"sector": "Old"}}}
    newer = {"updated_at": "2026-10-08T00:00:00+00:00", "symbols": {"AAPL": {"sector": "New"}, "MSFT": {"sector": "New"}}}
    assert merge_metadata({}, old, {})["symbols"]["AAPL"]["sector"] == "Old"
    assert merge_metadata({}, old, newer)["symbols"]["MSFT"]["sector"] == "New"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")


def _write_repo_files(repo: Path, rows: list[dict[str, str]], ids: list[int], snapshots: list[dict[str, str]]) -> None:
    tracker._write_csv(repo / "data" / "signal_history.csv", rows, tracker.HISTORY_FIELDS)
    (repo / "data" / "state.json").write_text(json.dumps(_state(ids), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tracker._write_csv(repo / "data" / "sector_snapshots.csv", snapshots, tracker.SNAPSHOT_FIELDS)
    previous = (tracker.REPORT_CSV, tracker.REPORT_TXT)
    tracker.REPORT_CSV = repo / "output" / "tracker_report.csv"
    tracker.REPORT_TXT = repo / "output" / "tracker_report.txt"
    try:
        tracker.write_reports(rows)
    finally:
        tracker.REPORT_CSV, tracker.REPORT_TXT = previous


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "--", "data", "output")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_pair(tmp_path: Path):
    bare = tmp_path / "origin.git"
    local = tmp_path / "local"
    other = tmp_path / "other"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-b", "main", str(local))
    _identity(local)
    _write_repo_files(local, [_row("a")], [1], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z")])
    base = _commit_all(local, "base")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(bare), str(other))
    _identity(other)
    return bare, local, other, base


def _history_ids(repo: Path, rev: str = "HEAD") -> list[str]:
    text = _git(repo, "show", f"{rev}:data/signal_history.csv").stdout
    return [row["signal_id"] for row in publish_changes._parse_csv(text)]


def _push_commands(monkeypatch) -> list[list[str]]:
    real_run = subprocess.run
    pushes: list[list[str]] = []

    def wrapped(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
            pushes.append(list(cmd))
            assert "--force" not in cmd and "--force-with-lease" not in cmd
            assert not any(str(part).startswith("+") for part in cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish_changes.subprocess, "run", wrapped)
    return pushes


def test_publish_fast_forward_keeps_new_signal_and_does_not_force_push(tmp_path, monkeypatch):
    _bare, local, _other, base = _init_pair(tmp_path)
    pushes = _push_commands(monkeypatch)
    rows = [_row("a", return_5d="1.5", last_updated="2026-10-02T00:00:00+00:00"), _row("b")]
    _write_repo_files(local, rows, [1, 2], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z"), _snapshot("2026-09-10", "2026-09-10T01:00:00Z")])

    publish_changes.publish_tracker_changes(local, "main", attempts=2, retry_delay=0)

    assert _history_ids(local, "origin/main") == ["a", "b"]
    assert _git(local, "merge-base", "--is-ancestor", base, "origin/main").returncode == 0
    assert pushes and all("HEAD:main" in command for command in pushes)
    state = json.loads(_git(local, "show", "origin/main:data/state.json").stdout)
    assert state["processed_gupiao_run_ids"] == [1, 2]
    history = publish_changes._parse_csv(_git(local, "show", "origin/main:data/signal_history.csv").stdout)
    assert history[0]["return_5d"] == "1.5"
    assert history[0]["push_price"] == "100.00"


def test_publish_rebase_unions_concurrent_history_without_rewriting_remote(tmp_path, monkeypatch):
    _bare, local, other, base = _init_pair(tmp_path)
    _push_commands(monkeypatch)
    _write_repo_files(
        other,
        [_row("a"), _row("c", symbol="NVDA")],
        [1, 3],
        [_snapshot("2026-09-08", "2026-09-08T01:00:00Z"), _snapshot("2026-09-09", "2026-09-09T01:00:00Z")],
    )
    upstream = _commit_all(other, "upstream signal")
    _git(other, "push", "origin", "HEAD:main")
    _write_repo_files(
        local,
        [_row("a", push_price="100"), _row("b", symbol="META")],
        [1, 2],
        [_snapshot("2026-09-08", "2026-09-08T01:00:00Z"), _snapshot("2026-09-10", "2026-09-10T01:00:00Z")],
    )

    publish_changes.publish_tracker_changes(local, "main", attempts=3, retry_delay=0)

    assert _history_ids(local, "origin/main") == ["a", "c", "b"]
    history = publish_changes._parse_csv(_git(local, "show", "origin/main:data/signal_history.csv").stdout)
    assert history[0]["push_price"] == "100.00"
    state = json.loads(_git(local, "show", "origin/main:data/state.json").stdout)
    assert state["processed_gupiao_run_ids"] == [1, 2, 3]
    snapshots = publish_changes._parse_csv(_git(local, "show", "origin/main:data/sector_snapshots.csv").stdout)
    assert [row["market_date"] for row in snapshots] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    assert _git(local, "merge-base", "--is-ancestor", base, "origin/main").returncode == 0
    assert _git(local, "merge-base", "--is-ancestor", upstream, "origin/main").returncode == 0


def test_publish_retries_rebase_when_push_is_rejected(tmp_path, monkeypatch):
    _bare, local, other, _base = _init_pair(tmp_path)
    real_run = subprocess.run
    pushes: list[list[str]] = []

    def wrapped(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
            pushes.append(list(cmd))
            assert "--force" not in cmd and not any(str(part).startswith("+") for part in cmd)
            if len(pushes) == 1:
                _write_repo_files(other, [_row("a"), _row("race", symbol="NVDA")], [1, 9], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z")])
                _commit_all(other, "racing push")
                real_run(["git", "push", "origin", "HEAD:main"], cwd=other, check=True, capture_output=True, text=True)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish_changes.subprocess, "run", wrapped)
    _write_repo_files(local, [_row("a"), _row("b", symbol="META")], [1, 2], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z")])

    publish_changes.publish_tracker_changes(local, "main", attempts=4, retry_delay=0)

    assert len(pushes) >= 2
    assert _history_ids(local, "origin/main") == ["a", "race", "b"]


def test_real_history_conflict_fails_and_leaves_remote_unchanged(tmp_path):
    bare, local, other, _base = _init_pair(tmp_path)
    _write_repo_files(other, [_row("a", push_price="102")], [1], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z")])
    _commit_all(other, "conflicting price")
    _git(other, "push", "origin", "HEAD:main")
    remote_before = _git(bare, "rev-parse", "main").stdout.strip()
    _write_repo_files(local, [_row("a", push_price="101"), _row("b")], [1], [_snapshot("2026-09-08", "2026-09-08T01:00:00Z")])

    with pytest.raises(PublishConflict, match="push_price"):
        publish_changes.publish_tracker_changes(local, "main", attempts=2, retry_delay=0)

    assert _git(bare, "rev-parse", "main").stdout.strip() == remote_before
    assert _history_ids(bare, "main") == ["a"]
    assert publish_changes._parse_csv(_git(bare, "show", "main:data/signal_history.csv").stdout)[0]["push_price"] == "102"


def test_no_local_changes_does_not_push(tmp_path, monkeypatch):
    _bare, local, _other, _base = _init_pair(tmp_path)
    pushes = _push_commands(monkeypatch)
    publish_changes.publish_tracker_changes(local, "main", attempts=1, retry_delay=0)
    assert pushes == []


def test_workflows_share_concurrency_and_do_not_force_push():
    tracker_workflow = Path(".github/workflows/tracker.yml").read_text(encoding="utf-8")
    refresh_workflow = Path(".github/workflows/refresh_constituents.yml").read_text(encoding="utf-8")
    publisher = Path("publish_changes.py").read_text(encoding="utf-8")
    for text in (tracker_workflow, refresh_workflow):
        assert "group: tracker-data-${{ github.repository }}" in text
        assert "cancel-in-progress: false" in text
        assert "python publish_changes.py" in text
        assert "--force" not in text
        assert "git push" not in text
    assert "success() && inputs.test_email != true" in tracker_workflow
    assert "fetch-depth: 0" in tracker_workflow
    assert "python sector_map.py --refresh" in refresh_workflow
    assert "push --force" not in publisher
    assert '["--force"]' not in publisher
    assert "force push is forbidden" in publisher
