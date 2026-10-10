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
    email_workflow = Path(".github/workflows/test_email.yml").read_text(encoding="utf-8")
    ci_workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    publisher = Path("publish_changes.py").read_text(encoding="utf-8")
    for text in (tracker_workflow, refresh_workflow):
        assert "permissions:\n  contents: write\n" in text
        assert "group: tracker-data-${{ github.repository }}" in text
        assert "cancel-in-progress: false" in text
        assert "python publish_changes.py" in text
        assert "--force" not in text
        assert "git push" not in text
    assert 'cron: "30 23 * * 1-5"' in tracker_workflow
    assert 'cron: "0 18 * * 0"' in refresh_workflow
    assert "success() && inputs.test_email != true" in tracker_workflow
    assert "fetch-depth: 0" in tracker_workflow
    assert "python sector_map.py --refresh" in refresh_workflow
    refresh_commit = refresh_workflow.split("Commit constituent cache", 1)[1]
    assert "always()" not in refresh_commit
    assert "permissions:\n  contents: read\n" in email_workflow
    assert "contents: write" not in email_workflow
    assert "permissions:\n  contents: read\n" in ci_workflow
    assert "python-version: \"3.11\"" in ci_workflow
    assert "pip check" in ci_workflow
    assert "python -m pytest -q" in ci_workflow
    assert "publish_changes.py" not in ci_workflow
    assert "push --force" not in publisher
    assert '["--force"]' not in publisher
    assert "force push is forbidden" in publisher


def test_repository_source_never_invokes_force_push():
    """A force-push flag may appear only as a rejection or an assertion."""
    allowed = (" not in ", 'arg == "--force"', 'startswith("--force")', "force push is forbidden")
    offenders = []
    for path in Path(".").rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".yml", ".yaml", ".sh", ".md"}:
            continue
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "--force" not in line and "force-with-lease" not in line and "push -f" not in line:
                continue
            if any(token in line for token in allowed):
                continue
            offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == []


_FROZEN_HISTORY_FIELDS = (
    "signal_id", "symbol", "signal_date", "signal_time", "signal_price", "push_date", "push_price",
    "source_run_id", "source_radar", "source_timeframe", "source_signal_level", "daily_dxdx", "h4_dxdx",
    "industry", "sector_theme", "strong_sector", "sector_rank", "sector_snapshot_run_id",
)


def _load_real_history() -> list[dict[str, str]]:
    return publish_changes._parse_csv(Path("data/signal_history.csv").read_text(encoding="utf-8"))


def _fill_empty_return(row: dict[str, str], value: str, stamp: str) -> str:
    for field in ("return_1d", "return_3d", "return_5d", "return_10d", "return_20d"):
        if not (row.get(field) or "").strip():
            row[field] = value
            row["last_updated"] = stamp
            return field
    row["last_updated"] = stamp
    return "last_updated"


def test_real_history_merge_keeps_every_record_once_and_restores_backup():
    history_path = Path("data/signal_history.csv")
    backup = history_path.read_bytes()
    try:
        rows = publish_changes._parse_csv(backup.decode("utf-8"))
        original_ids = [row["signal_id"] for row in rows]
        assert len(original_ids) == len(set(original_ids))
        empty = [index for index, row in enumerate(rows) if not (row.get("return_1d") or "").strip()]
        assert len(empty) >= 2
        left = [dict(row) for row in rows]
        right = [dict(row) for row in rows]
        left_field = _fill_empty_return(left[empty[0]], "0.51", "2026-10-10T12:00:00+00:00")
        right_field = _fill_empty_return(right[empty[1]], "0.62", "2026-10-10T13:00:00+00:00")
        left.append(_row("writer-a", symbol="ZZZA", push_price="1.00"))
        right.append(_row("writer-b", symbol="ZZZB", push_price="2.00"))
        shared = _row("writer-both", symbol="ZZZC", push_price="3.00")
        merged = merge_history_rows(rows, left + [dict(shared)], right + [dict(shared)])
        ids = [row["signal_id"] for row in merged]
        assert len(ids) == len(set(ids)) == len(original_ids) + 3
        assert ids[: len(original_ids)] == original_ids
        assert ids.count("writer-a") == ids.count("writer-b") == ids.count("writer-both") == 1
        published = {row["signal_id"]: row for row in merged}
        for original in rows:
            for field in _FROZEN_HISTORY_FIELDS:
                assert published[original["signal_id"]][field] == (original.get(field) or "").strip()
        assert published[rows[empty[0]]["signal_id"]][left_field] == "0.51"
        assert published[rows[empty[1]]["signal_id"]][right_field] == "0.62"
    finally:
        if history_path.read_bytes() != backup:
            history_path.write_bytes(backup)
            raise AssertionError("data/signal_history.csv changed; restored from backup")
    assert history_path.read_bytes() == backup


def _seed_real_history_repo(tmp_path: Path):
    bare = tmp_path / "origin.git"
    local = tmp_path / "local"
    other = tmp_path / "other"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-b", "main", str(local))
    _identity(local)
    data_dir = local / "data"
    data_dir.mkdir()
    (local / "output").mkdir()
    for name in ("signal_history.csv", "state.json", "sector_snapshots.csv", "sector_metadata_cache.json", "events.csv"):
        (data_dir / name).write_bytes((Path("data") / name).read_bytes())
    rows = _load_real_history()
    previous = (tracker.REPORT_CSV, tracker.REPORT_TXT)
    tracker.REPORT_CSV = local / "output" / "tracker_report.csv"
    tracker.REPORT_TXT = local / "output" / "tracker_report.txt"
    try:
        tracker.write_reports(rows)
    finally:
        tracker.REPORT_CSV, tracker.REPORT_TXT = previous
    _commit_all(local, "real history backup")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(bare), str(other))
    _identity(other)
    return bare, local, other, rows


def _apply_real_writer(repo: Path, rows: list[dict[str, str]], index: int, new_id: str, value: str, stamp: str, run_id: int) -> str:
    copied = [dict(row) for row in rows]
    field = _fill_empty_return(copied[index], value, stamp)
    added = dict(copied[index])
    added.update({
        "signal_id": new_id, "symbol": "ZZZ", "signal_price": "1", "push_price": "1",
        "return_1d": "", "return_3d": "", "return_5d": "", "return_10d": "", "return_20d": "",
        "mfe_10d": "", "mae_10d": "", "mfe_20d": "", "mae_20d": "",
        "effectiveness": "pending", "sessions_observed": "0", "last_updated": stamp,
        "source_run_id": str(run_id), "strong_sector": "", "sector_rank": "", "sector_snapshot_run_id": "",
    })
    copied.append(added)
    tracker._write_csv(repo / "data" / "signal_history.csv", copied, tracker.HISTORY_FIELDS)
    state_path = repo / "data" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["processed_gupiao_run_ids"] = sorted(set(state["processed_gupiao_run_ids"]) | {run_id})
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return field


def test_two_concurrent_writers_rebase_real_history_without_loss_or_duplication(tmp_path, monkeypatch):
    history_path = Path("data/signal_history.csv")
    state_path = Path("data/state.json")
    backup = history_path.read_bytes()
    state_backup = state_path.read_bytes()
    try:
        _bare, local, other, rows = _seed_real_history_repo(tmp_path)
        original_ids = [row["signal_id"] for row in rows]
        empty = [index for index, row in enumerate(rows) if not (row.get("return_1d") or "").strip()]
        assert len(empty) >= 2
        real_run = subprocess.run
        pushes: list[list[str]] = []

        def wrapped(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
                pushes.append(list(cmd))
                assert "--force" not in cmd and "--force-with-lease" not in cmd
                assert not any(str(part).startswith("+") for part in cmd)
                if len(pushes) == 1:
                    _apply_real_writer(other, rows, empty[0], "writer-a", "0.51", "2026-10-10T12:00:00+00:00", 990001)
                    _commit_all(other, "writer A")
                    real_run(["git", "push", "origin", "HEAD:main"], cwd=other, check=True, capture_output=True, text=True)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(publish_changes.subprocess, "run", wrapped)
        right_field = _apply_real_writer(local, rows, empty[1], "writer-b", "0.62", "2026-10-10T13:00:00+00:00", 990002)
        original_state_ids = set(json.loads(state_backup)["processed_gupiao_run_ids"])

        publish_changes.publish_tracker_changes(local, "main", attempts=4, retry_delay=0)

        published_rows = publish_changes._parse_csv(_git(local, "show", "origin/main:data/signal_history.csv").stdout)
        published_ids = [row["signal_id"] for row in published_rows]
        assert len(pushes) >= 2
        assert len(published_ids) == len(set(published_ids)) == len(original_ids) + 2
        assert published_ids[: len(original_ids)] == original_ids
        assert published_ids.count("writer-a") == published_ids.count("writer-b") == 1
        published = {row["signal_id"]: row for row in published_rows}
        for original in rows:
            for field in _FROZEN_HISTORY_FIELDS:
                assert published[original["signal_id"]][field] == (original.get(field) or "").strip(), (original["signal_id"], field)
        assert published[rows[empty[0]]["signal_id"]]["return_1d"] == "0.51"
        assert published[rows[empty[1]]["signal_id"]][right_field] == "0.62"
        state = json.loads(_git(local, "show", "origin/main:data/state.json").stdout)
        assert original_state_ids <= set(state["processed_gupiao_run_ids"])
        assert {990001, 990002} <= set(state["processed_gupiao_run_ids"])
        assert len(state["processed_gupiao_run_ids"]) == len(set(state["processed_gupiao_run_ids"]))
    finally:
        restored = False
        if history_path.read_bytes() != backup:
            history_path.write_bytes(backup)
            restored = True
        if state_path.read_bytes() != state_backup:
            state_path.write_bytes(state_backup)
            restored = True
        if restored:
            raise AssertionError("history data changed during the simulation; restored from backup")
    assert history_path.read_bytes() == backup
    assert state_path.read_bytes() == state_backup
