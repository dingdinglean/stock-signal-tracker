"""Publish tracker data by rebasing onto the remote tip.

The scheduled job used to commit and push without fetching.  A concurrent push
then rejected the update and the runner's history disappeared with it.

This replays the local data commit onto the latest remote commit.  Overlapping
edits to history, state, and sector snapshots are merged record-by-record.
Existing signal ids, processed run ids, and snapshot dates are never dropped.
A real field conflict fails the publish.  The push is never forced.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sector_map
import tracker


class PublishConflict(RuntimeError):
    """Raised when data cannot be published without dropping or clobbering history."""


HISTORY_FIELDS = list(tracker.HISTORY_FIELDS)
SNAPSHOT_FIELDS = list(tracker.SNAPSHOT_FIELDS)
_DATA_FILES = (
    "data/signal_history.csv",
    "data/state.json",
    "data/sector_snapshots.csv",
    "data/sector_metadata_cache.json",
)
_REPORTS = ("output/tracker_report.csv", "output/tracker_report.txt")
_RESOLVABLE = set(_DATA_FILES) | set(_REPORTS)
_TERMINAL_EFFECTIVENESS = {"有效", "无效", "中性", "无法判定"}
_REF_NAME = re.compile(r"[A-Za-z0-9._/-]+")


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _same(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return False
    try:
        return float(left) == float(right)
    except ValueError:
        return False


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _index_rows(rows: list[dict[str, str]], key: str, label: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        identity = _norm(row.get(key))
        if not identity:
            raise PublishConflict(f"{label} row is missing {key}")
        if identity in indexed and not all(_same(_norm(indexed[identity].get(field)), _norm(row.get(field))) for field in set(indexed[identity]) | set(row)):
            raise PublishConflict(f"duplicate {key} {identity} in {label}")
        indexed[identity] = row
    return indexed


def _merge_timestamp(signal_id: str, base: str, upstream: str, local: str) -> str:
    if _same(upstream, local):
        return upstream or local
    if _same(upstream, base):
        return local
    if _same(local, base):
        return upstream
    upstream_at, local_at = _parse_ts(upstream), _parse_ts(local)
    if upstream_at and local_at:
        return upstream if upstream_at >= local_at else local
    raise PublishConflict(f"signal_id {signal_id} last_updated conflicts (upstream={upstream!r}, local={local!r})")


def _merge_effectiveness(signal_id: str, base: str, upstream: str, local: str) -> str:
    if _same(upstream, local):
        return upstream or local
    if _same(upstream, base):
        return local
    if _same(local, base):
        return upstream

    def richness(value: str) -> int:
        if value in _TERMINAL_EFFECTIVENESS:
            return 2
        return 1 if value == "pending" else 0

    if richness(upstream) != richness(local):
        return upstream if richness(upstream) > richness(local) else local
    raise PublishConflict(f"signal_id {signal_id} effectiveness conflicts (upstream={upstream!r}, local={local!r}); refusing to overwrite history")


def _merge_value(signal_id: str, field: str, base: str, upstream: str, local: str) -> str:
    if field == "last_updated":
        return _merge_timestamp(signal_id, base, upstream, local)
    if field == "effectiveness":
        return _merge_effectiveness(signal_id, base, upstream, local)
    if _same(upstream, local):
        return upstream or local
    if _same(upstream, base):
        return local
    if _same(local, base):
        return upstream
    if not upstream:
        return local
    if not local:
        return upstream
    raise PublishConflict(
        f"signal_id {signal_id} field {field} conflicts (upstream={upstream!r}, local={local!r}); refusing to overwrite history"
    )


def _canon_history_row(row: dict[str, Any]) -> dict[str, str]:
    return {field: _norm(row.get(field)) for field in HISTORY_FIELDS}


def merge_history_rows(base: list[dict[str, str]], upstream: list[dict[str, str]], local: list[dict[str, str]]) -> list[dict[str, str]]:
    """Union signal ids. One-sided edits win; both-sided disagreements fail."""
    base_rows = _index_rows(base, "signal_id", "base history")
    upstream_rows = _index_rows(upstream, "signal_id", "upstream history")
    local_rows = _index_rows(local, "signal_id", "local history")
    order: list[str] = []
    for signal_id in list(upstream_rows) + list(local_rows) + list(base_rows):
        if signal_id not in order:
            order.append(signal_id)
    merged: list[dict[str, str]] = []
    for signal_id in order:
        current = base_rows.get(signal_id, {})
        remote = upstream_rows.get(signal_id, {})
        ours = local_rows.get(signal_id, {})
        if remote and ours:
            merged.append({field: _merge_value(signal_id, field, _norm(current.get(field)), _norm(remote.get(field)), _norm(ours.get(field))) for field in HISTORY_FIELDS})
        elif remote:
            merged.append(_canon_history_row(remote))
        elif ours:
            merged.append(_canon_history_row(ours))
        else:
            merged.append(_canon_history_row(current))
    published_ids = [row["signal_id"] for row in merged]
    missing = (set(base_rows) | set(upstream_rows) | set(local_rows)) - set(published_ids)
    if missing:
        raise PublishConflict(f"refusing to drop signal history ids: {sorted(missing)}")
    return merged


def merge_state(base: dict[str, Any], upstream: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Union processed run ids. Never drop an id that any side has recorded."""
    for name, payload in (("base", base), ("upstream", upstream), ("local", local)):
        if not isinstance(payload, dict):
            raise PublishConflict(f"{name} state.json is not an object")
    merged: dict[str, Any] = {}
    for key in set(base) | set(upstream) | set(local):
        values = [payload[key] for payload in (base, upstream, local) if key in payload]
        if all(isinstance(value, list) for value in values):
            combined: list[Any] = []
            for value in values:
                combined.extend(value)
            if all(isinstance(item, int) for item in combined):
                merged[key] = sorted(set(combined))
            else:
                ordered: list[Any] = []
                for item in combined:
                    if item not in ordered:
                        ordered.append(item)
                merged[key] = ordered
            continue
        distinct = []
        for value in values:
            if value not in distinct:
                distinct.append(value)
        if len(distinct) > 1:
            raise PublishConflict(f"state.json key {key} conflicts ({distinct!r}); refusing to overwrite it")
        merged[key] = distinct[0]
    return merged


def _snapshot_time(row: dict[str, str]) -> str:
    return _norm(row.get("run_created_at"))


def _snapshot_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    fields = set(SNAPSHOT_FIELDS) | set(left) | set(right)
    return all(_same(_norm(left.get(field)), _norm(right.get(field))) for field in fields)


def merge_snapshots(base: list[dict[str, str]], upstream: list[dict[str, str]], local: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep every market date. Same-day disagreements follow the newer source run."""
    base_rows = _index_rows(base, "market_date", "base snapshots")
    upstream_rows = _index_rows(upstream, "market_date", "upstream snapshots")
    local_rows = _index_rows(local, "market_date", "local snapshots")
    merged: list[dict[str, str]] = []
    for market_date in sorted(set(base_rows) | set(upstream_rows) | set(local_rows)):
        current = base_rows.get(market_date)
        remote = upstream_rows.get(market_date)
        ours = local_rows.get(market_date)
        if remote and ours:
            if _snapshot_equal(remote, ours):
                merged.append(remote)
            elif current and _snapshot_equal(remote, current):
                merged.append(ours)
            elif current and _snapshot_equal(ours, current):
                merged.append(remote)
            else:
                remote_at, local_at = _snapshot_time(remote), _snapshot_time(ours)
                if remote_at and local_at and remote_at != local_at:
                    merged.append(remote if remote_at > local_at else ours)
                else:
                    raise PublishConflict(f"sector snapshot {market_date} conflicts; refusing to overwrite it")
        else:
            chosen = remote or ours or current
            if chosen is None:
                raise PublishConflict(f"sector snapshot {market_date} disappeared on every side")
            merged.append(chosen)
    return merged


def merge_metadata(base: dict[str, Any], upstream: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Keep the newest plausible constituent cache. Never replace it with a bad download."""
    plausible = [payload for payload in (local, upstream, base) if sector_map._strictly_plausible(payload)]
    if plausible:
        return max(plausible, key=lambda payload: str(payload.get("updated_at") or ""))
    usable = [payload for payload in (upstream, local, base) if isinstance(payload, dict) and isinstance(payload.get("symbols"), dict) and payload.get("symbols")]
    if usable:
        return max(usable, key=lambda payload: (len(payload.get("symbols") or {}), str(payload.get("updated_at") or "")))
    if all(isinstance(payload, dict) and not payload.get("symbols") for payload in (base, upstream, local)):
        return {}
    raise PublishConflict("sector metadata cache conflict has no usable cache")


def _parse_csv(text: str | None) -> list[dict[str, str]]:
    if not text or not text.strip():
        return []
    return [dict(row) for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff")))]


def _parse_json(text: str | None, label: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PublishConflict(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PublishConflict(f"{label} is not an object")
    return payload


def _dump_state(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)


def _dump_metadata(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _history_matches(left: list[dict[str, str]], right: list[dict[str, str]]) -> bool:
    """Exact match against the merged rows, so equivalent spellings are not left rewritten."""
    if len(left) != len(right):
        return False
    return all(_canon_history_row(a) == _canon_history_row(b) for a, b in zip(left, right))


def _validate_ref(name: str, label: str) -> None:
    if not _REF_NAME.fullmatch(name) or name.startswith("-") or ".." in name or name.endswith("/") or "@{" in name:
        raise PublishConflict(f"refusing to publish to {label} {name!r}")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "push" and any(arg == "--force" or arg.startswith("--force") or arg.startswith("+") for arg in args[1:]):
        raise PublishConflict("force push is forbidden")
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    env["GIT_SEQUENCE_EDITOR"] = "true"
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=check, env=env)


def _show(repo: Path, spec: str) -> str | None:
    proc = subprocess.run(["git", "show", spec], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def _status_path(line: str) -> str:
    body = line[3:]
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    return body.strip().strip('"')


def _tracked_changes(repo: Path) -> list[str]:
    proc = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _ensure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "github-actions[bot]")
    _git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")


def _restore_snapshot(repo: Path, snapshot: str) -> None:
    subprocess.run(["git", "rebase", "--abort"], cwd=repo, capture_output=True, text=True)
    subprocess.run(["git", "reset", "--hard", snapshot], cwd=repo, check=True, capture_output=True, text=True)


def _unmerged(repo: Path) -> list[str]:
    proc = _git(repo, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _merged_payloads(repo: Path, base_sha: str | None, upstream_sha: str, snapshot: str) -> dict[str, Any]:
    def load(sha: str | None, path: str) -> str | None:
        if not sha:
            return None
        return _show(repo, f"{sha}:{path}")

    base_history = _parse_csv(load(base_sha, "data/signal_history.csv"))
    upstream_history = _parse_csv(load(upstream_sha, "data/signal_history.csv"))
    local_history = _parse_csv(load(snapshot, "data/signal_history.csv"))
    history = merge_history_rows(base_history, upstream_history, local_history)
    state = merge_state(
        _parse_json(load(base_sha, "data/state.json"), "base state.json"),
        _parse_json(load(upstream_sha, "data/state.json"), "upstream state.json"),
        _parse_json(load(snapshot, "data/state.json"), "local state.json"),
    )
    snapshots = merge_snapshots(
        _parse_csv(load(base_sha, "data/sector_snapshots.csv")),
        _parse_csv(load(upstream_sha, "data/sector_snapshots.csv")),
        _parse_csv(load(snapshot, "data/sector_snapshots.csv")),
    )
    metadata = merge_metadata(
        _parse_json(load(base_sha, "data/sector_metadata_cache.json"), "base sector metadata"),
        _parse_json(load(upstream_sha, "data/sector_metadata_cache.json"), "upstream sector metadata"),
        _parse_json(load(snapshot, "data/sector_metadata_cache.json"), "local sector metadata"),
    )
    required_ids = {row.get("signal_id") for rows in (base_history, upstream_history, local_history) for row in rows}
    if required_ids - {row["signal_id"] for row in history}:
        raise PublishConflict("refusing to drop signal history ids")
    return {"history": history, "state": state, "snapshots": snapshots, "metadata": metadata}


def _write_merged_files(repo: Path, merged: dict[str, Any]) -> bool:
    """Write merged data and regenerated reports. Return True when the index changed."""
    history_path = repo / "data" / "signal_history.csv"
    state_path = repo / "data" / "state.json"
    snapshot_path = repo / "data" / "sector_snapshots.csv"
    metadata_path = repo / "data" / "sector_metadata_cache.json"
    current_history = _parse_csv(history_path.read_text(encoding="utf-8") if history_path.exists() else None)
    rewrote_history = False
    if not _history_matches(current_history, merged["history"]):
        tracker._write_csv(history_path, merged["history"], HISTORY_FIELDS)
        rewrote_history = True
    current_state = _parse_json(state_path.read_text(encoding="utf-8") if state_path.exists() else None, "worktree state.json")
    if current_state != merged["state"]:
        state_path.write_text(_dump_state(merged["state"]), encoding="utf-8")
    current_snapshots = _parse_csv(snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else None)
    expected_snapshots = [{field: _norm(row.get(field)) for field in SNAPSHOT_FIELDS} for row in merged["snapshots"]]
    actual_snapshots = [{field: _norm(row.get(field)) for field in SNAPSHOT_FIELDS} for row in current_snapshots]
    if actual_snapshots != expected_snapshots:
        tracker._write_csv(snapshot_path, expected_snapshots, SNAPSHOT_FIELDS)
    current_metadata = _parse_json(metadata_path.read_text(encoding="utf-8") if metadata_path.exists() else None, "worktree sector metadata")
    if current_metadata != merged["metadata"] and merged["metadata"]:
        metadata_path.write_text(_dump_metadata(merged["metadata"]), encoding="utf-8")
    if rewrote_history:
        _regenerate_reports(repo, merged["history"])
    _git(repo, "add", "--", "data", "output")
    diff = _git(repo, "diff", "--cached", "--quiet", check=False)
    return diff.returncode != 0


def _regenerate_reports(repo: Path, history: list[dict[str, str]]) -> None:
    previous = (tracker.REPORT_CSV, tracker.REPORT_TXT)
    tracker.REPORT_CSV = repo / "output" / "tracker_report.csv"
    tracker.REPORT_TXT = repo / "output" / "tracker_report.txt"
    try:
        tracker.write_reports(history)
    finally:
        tracker.REPORT_CSV, tracker.REPORT_TXT = previous


def _continue_rebase(repo: Path) -> None:
    proc = _git(repo, "rebase", "--continue", check=False)
    if proc.returncode == 0:
        return
    combined = f"{proc.stdout}\n{proc.stderr}"
    if "No changes" in combined or "no changes" in combined:
        skip = _git(repo, "rebase", "--skip", check=False)
        if skip.returncode != 0:
            raise PublishConflict(f"git rebase --skip failed: {skip.stderr.strip()}")
        return
    raise PublishConflict(f"git rebase --continue failed: {combined.strip()}")


def _resolve_conflicts(repo: Path) -> None:
    conflicts = _unmerged(repo)
    unexpected = sorted(set(conflicts) - _RESOLVABLE)
    if unexpected:
        raise PublishConflict(f"unresolvable conflicts outside tracker data: {unexpected}; refusing to clobber history")
    for path in _DATA_FILES:
        if path not in conflicts:
            continue
        base = _show(repo, f":1:{path}")
        upstream = _show(repo, f":2:{path}")
        local = _show(repo, f":3:{path}")
        if upstream is None or local is None:
            raise PublishConflict(f"{path} was deleted on one side; refusing to drop it")
        if path == "data/signal_history.csv":
            merged_rows = merge_history_rows(_parse_csv(base), _parse_csv(upstream), _parse_csv(local))
            tracker._write_csv(repo / path, merged_rows, HISTORY_FIELDS)
        elif path == "data/state.json":
            merged_state = merge_state(_parse_json(base, "base state.json"), _parse_json(upstream, "upstream state.json"), _parse_json(local, "local state.json"))
            (repo / path).write_text(_dump_state(merged_state), encoding="utf-8")
        elif path == "data/sector_snapshots.csv":
            merged_snapshots = merge_snapshots(_parse_csv(base), _parse_csv(upstream), _parse_csv(local))
            tracker._write_csv(repo / path, [{field: _norm(row.get(field)) for field in SNAPSHOT_FIELDS} for row in merged_snapshots], SNAPSHOT_FIELDS)
        else:
            merged_metadata = merge_metadata(_parse_json(base, "base sector metadata"), _parse_json(upstream, "upstream sector metadata"), _parse_json(local, "local sector metadata"))
            (repo / path).write_text(_dump_metadata(merged_metadata), encoding="utf-8")
        print(f"Resolved {path} without dropping existing records")
        _git(repo, "add", "--", path)
    if any(path in conflicts for path in _REPORTS):
        history_text = (repo / "data" / "signal_history.csv").read_text(encoding="utf-8")
        _regenerate_reports(repo, _parse_csv(history_text))
        _git(repo, "add", "--", *_REPORTS)
    _continue_rebase(repo)


def _non_fast_forward(text: str) -> bool:
    lowered = text.lower()
    return "non-fast-forward" in lowered or "fetch first" in lowered


def publish_tracker_changes(
    repo: Path,
    branch: str,
    *,
    remote: str = "origin",
    attempts: int = 5,
    message: str = "Update signal history",
    retry_delay: float = 2,
) -> None:
    """Commit data/ and output/, rebase onto the remote branch, and push.

    Retries when the remote moves.  Never force-pushes.  A real data conflict
    leaves the remote unchanged and raises PublishConflict.
    """
    if attempts < 1:
        raise PublishConflict("at least one publish attempt is required")
    _validate_ref(branch, "branch")
    _validate_ref(remote, "remote")
    repo = repo.resolve()
    _ensure_identity(repo)
    changes = _tracked_changes(repo)
    outside = [_status_path(line) for line in changes if not _status_path(line).startswith(("data/", "output/"))]
    if outside:
        raise PublishConflict(f"unexpected local changes outside data/ and output/: {outside}")
    relevant = [line for line in changes if _status_path(line).startswith(("data/", "output/"))]
    if not relevant:
        print("No tracker changes to commit.")
        return
    _git(repo, "add", "--", "data", "output")
    commit = _git(repo, "commit", "-m", message, check=False)
    if commit.returncode != 0:
        text = f"{commit.stdout}\n{commit.stderr}"
        if "nothing to commit" in text.lower():
            print("No tracker changes to commit.")
            return
        raise PublishConflict(f"git commit failed: {text.strip()}")
    snapshot = _git(repo, "rev-parse", "HEAD").stdout.strip()
    parent = _git(repo, "rev-parse", f"{snapshot}^", check=False)
    base_sha = parent.stdout.strip() if parent.returncode == 0 else None

    for attempt in range(1, attempts + 1):
        print(f"Rebasing tracker changes onto {remote}/{branch} (attempt {attempt}/{attempts})")
        _git(repo, "fetch", remote, branch)
        upstream_sha = _git(repo, "rev-parse", f"{remote}/{branch}").stdout.strip()
        _git(repo, "reset", "--hard", snapshot)
        rebase = _git(repo, "rebase", f"{remote}/{branch}", check=False)
        try:
            if rebase.returncode != 0:
                if not _unmerged(repo):
                    raise PublishConflict(f"git rebase failed: {(rebase.stderr or rebase.stdout).strip()}")
                _resolve_conflicts(repo)
            merged = _merged_payloads(repo, base_sha, upstream_sha, snapshot)
            if _write_merged_files(repo, merged):
                head = _git(repo, "rev-parse", "HEAD").stdout.strip()
                parent_sha = _git(repo, "rev-parse", "HEAD^").stdout.strip()
                if head == upstream_sha:
                    _git(repo, "commit", "-m", message)
                elif parent_sha == upstream_sha:
                    _git(repo, "commit", "--amend", "--no-edit")
                else:
                    raise PublishConflict("refusing to amend a commit that is not a direct child of the remote tip")
            published = _parse_csv(_show(repo, "HEAD:data/signal_history.csv"))
            if not _history_matches(published, merged["history"]):
                raise PublishConflict("internal error: published history does not match the merged history")
        except PublishConflict:
            _restore_snapshot(repo, snapshot)
            raise
        push = _git(repo, "push", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            print(f"Pushed tracker history to {remote}/{branch}")
            return
        details = f"{push.stderr}\n{push.stdout}"
        if _non_fast_forward(details) and attempt < attempts:
            print(f"Push rejected because the remote moved; retrying rebase (attempt {attempt}/{attempts})")
            time.sleep(retry_delay * attempt)
            continue
        _restore_snapshot(repo, snapshot)
        if _non_fast_forward(details):
            raise PublishConflict(f"git push rejected after {attempts} rebase attempts; remote history was left unchanged")
        raise PublishConflict(f"git push failed: {details.strip()}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish data/ and output/ without dropping history")
    parser.add_argument("--branch", default=os.environ.get("PUBLISH_BRANCH") or os.environ.get("GITHUB_REF_NAME"))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--attempts", type=int, default=int(os.environ.get("PUBLISH_ATTEMPTS", "5")))
    parser.add_argument("--message", default=os.environ.get("PUBLISH_MESSAGE", "Update signal history"))
    parser.add_argument("--retry-delay", type=float, default=float(os.environ.get("PUBLISH_RETRY_DELAY", "2")))
    args = parser.parse_args(argv)
    if not args.branch:
        print("::error::PUBLISH_BRANCH is required", file=sys.stderr)
        raise SystemExit(1)
    try:
        publish_tracker_changes(
            Path.cwd(),
            args.branch,
            remote=args.remote,
            attempts=args.attempts,
            message=args.message,
            retry_delay=args.retry_delay,
        )
    except PublishConflict as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
