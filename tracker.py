"""Read-only verifier for emails actually sent by the independent DXDX radar."""
from __future__ import annotations

import csv
import argparse
import copy
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from github_artifacts import GitHubArtifactError, GitHubPublicArtifacts, find_member, parse_github_time
from email_sender import send_connectivity_test, send_email
from notifications import build_body, build_subject, changes_between
from performance import calculate_performance, fetch_history
from sector_map import load_metadata, metadata_for


GUPIAO_REPO = "dingdinglean/gupiao"
GUPIAO_WORKFLOW = "screen.yml"
GUPIAO_ARTIFACT = "dxdx-pullback-radar"
STRENGTH_REPO = "dingdinglean/strong-pullback-screener"
STRENGTH_WORKFLOW = "strong_pullback_screener.yml"
STRENGTH_ARTIFACT = "market-strength-radar-v4"
DEFAULT_START = "2026-09-06T00:00:00Z"
NEW_YORK = ZoneInfo("America/New_York")

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
HISTORY_CSV = DATA_DIR / "signal_history.csv"
LEGACY_EVENTS_CSV = DATA_DIR / "events.csv"
SECTOR_SNAPSHOTS_CSV = DATA_DIR / "sector_snapshots.csv"
STATE_JSON = DATA_DIR / "state.json"
METADATA_CACHE = DATA_DIR / "sector_metadata_cache.json"
REPORT_CSV = OUTPUT_DIR / "tracker_report.csv"
REPORT_TXT = OUTPUT_DIR / "tracker_report.txt"

HISTORY_FIELDS = [
    "signal_id", "symbol", "signal_date", "signal_time", "signal_price", "source_run_id", "source_signal_level",
    "daily_dxdx", "h4_dxdx", "industry", "sector_theme", "strong_sector", "sector_rank", "sector_snapshot_run_id",
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "mfe_10d", "mae_10d", "mfe_20d", "mae_20d",
    "effectiveness", "sessions_observed", "last_updated",
]
SNAPSHOT_FIELDS = ["market_date", "run_id", "run_created_at", "rank1_theme", "rank2_theme", "rank3_theme"]
EMAIL_SENT = re.compile(r"邮件是否发送\s*[:：]\s*是|email\s+(?:sent|delivery)\s+(?:success|successful)", re.I)


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _decode(raw: bytes | None) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return (raw or b"").decode(encoding)
        except UnicodeDecodeError:
            pass
    return (raw or b"").decode("utf-8", errors="replace")


def _load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("processed_gupiao_run_ids", [])
    state.setdefault("processed_strength_run_ids", [])
    state.setdefault("tracking_start_utc", os.getenv("TRACKING_START_UTC", DEFAULT_START))
    return state


def _save_state(state: dict[str, Any]) -> None:
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _start(state: dict[str, Any]) -> datetime:
    value = datetime.fromisoformat(str(state["tracking_start_utc"]).replace("Z", "+00:00"))
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _market_date(run: dict[str, Any]) -> str:
    created = parse_github_time(run.get("created_at"))
    return created.astimezone(NEW_YORK).date().isoformat() if created else ""


def _signal_date(row: dict[str, str], run: dict[str, Any]) -> str:
    for key in ("h4_signal_time", "daily_signal_time"):
        value = (row.get(key) or "").strip()
        if value:
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (timestamp.astimezone(NEW_YORK) if timestamp.tzinfo else timestamp).date().isoformat()
            except ValueError:
                if len(value) >= 10:
                    return value[:10]
    return _market_date(run)


def _signal_time(row: dict[str, str], fallback: str) -> str:
    return (row.get("h4_signal_time") or row.get("daily_signal_time") or fallback).strip()


def build_signal_id(run_id: int | str, symbol: str, signal_time: str) -> str:
    """One formal push per source run/symbol/signal bar; repeat pushes survive."""
    raw = f"{run_id}|{symbol.upper()}|{signal_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load_history() -> list[dict[str, str]]:
    history = _read_csv(HISTORY_CSV)
    if history or not LEGACY_EVENTS_CSV.exists():
        return history
    # Non-destructive one-time migration for the initial local prototype.
    migrated: list[dict[str, str]] = []
    for old in _read_csv(LEGACY_EVENTS_CSV):
        run_id = old.get("gupiao_run_id", "")
        signal_time = old.get("h4_signal_time") or old.get("daily_signal_time") or old.get("signal_date", "")
        item = {field: "" for field in HISTORY_FIELDS}
        item.update({
            "signal_id": old.get("event_id") or build_signal_id(run_id, old.get("symbol", ""), signal_time),
            "symbol": old.get("symbol", ""), "signal_date": old.get("signal_date", ""), "signal_time": signal_time,
            "signal_price": old.get("pushed_price", ""), "source_run_id": run_id,
            "source_signal_level": old.get("source_signal_level", ""), "daily_dxdx": "", "h4_dxdx": "",
            "industry": old.get("stock_theme_raw", ""), "sector_theme": old.get("stock_theme", ""),
            "strong_sector": old.get("strong_sector_status", "未知"), "sector_rank": old.get("strong_sector_rank", ""),
            "sector_snapshot_run_id": old.get("strong_sector_snapshot_run_id", ""),
            "return_1d": old.get("ret_1d", ""), "return_3d": old.get("ret_3d", ""), "return_5d": old.get("ret_5d", ""),
            "return_10d": old.get("ret_10d", ""), "return_20d": old.get("ret_20d", ""),
            "mfe_10d": old.get("mfe_10d", ""), "mae_10d": old.get("mae_10d", ""), "mfe_20d": old.get("mfe_20d", ""), "mae_20d": old.get("mae_20d", ""),
            "effectiveness": old.get("effective_10d", "pending"), "sessions_observed": old.get("sessions_observed", "0"), "last_updated": old.get("last_updated_at", ""),
        })
        migrated.append(item)
    return migrated


def ingest_sector_snapshots(client: GitHubPublicArtifacts, state: dict[str, Any]) -> list[dict[str, str]]:
    snapshots = {row.get("market_date", ""): row for row in _read_csv(SECTOR_SNAPSHOTS_CSV) if row.get("market_date")}
    processed = {int(value) for value in state["processed_strength_run_ids"]}
    try:
        runs = client.workflow_runs(STRENGTH_REPO, STRENGTH_WORKFLOW, since=_start(state))
    except GitHubArtifactError as exc:
        print(f"::warning::Strength workflow list unavailable: {exc}")
        runs = []
    for run in runs:
        run_id = int(run["id"])
        if run_id in processed:
            continue
        try:
            archive = client.artifact_archive(STRENGTH_REPO, run_id, STRENGTH_ARTIFACT)
        except GitHubArtifactError as exc:
            print(f"::warning::Strength Artifact unavailable for run {run_id}: {exc}")
            continue
        processed.add(run_id)
        raw = find_member(archive or {}, "sector_strength.csv")
        if not raw:
            continue
        rows = sorted(csv.DictReader(io.StringIO(_decode(raw))), key=lambda item: int(float(item.get("rank") or 999)))[:3]
        market_date = _market_date(run)
        if not market_date or not rows:
            continue
        candidate = {"market_date": market_date, "run_id": str(run_id), "run_created_at": str(run.get("created_at", ""))}
        for rank in range(1, 4):
            candidate[f"rank{rank}_theme"] = (rows[rank - 1].get("sector_theme") if len(rows) >= rank else "") or ""
        current = snapshots.get(market_date)
        if not current or candidate["run_created_at"] >= current.get("run_created_at", ""):
            snapshots[market_date] = candidate
    state["processed_strength_run_ids"] = sorted(processed)
    result = sorted(snapshots.values(), key=lambda row: row["market_date"])
    _write_csv(SECTOR_SNAPSHOTS_CSV, result, SNAPSHOT_FIELDS)
    return result


def ingest_formal_pushes(client: GitHubPublicArtifacts, state: dict[str, Any], history: list[dict[str, str]]) -> list[dict[str, str]]:
    known = {row.get("signal_id") for row in history}
    processed = {int(value) for value in state["processed_gupiao_run_ids"]}
    try:
        runs = client.workflow_runs(GUPIAO_REPO, GUPIAO_WORKFLOW, since=_start(state))
    except GitHubArtifactError as exc:
        print(f"::warning::DXDX workflow list unavailable: {exc}")
        runs = []
    for run in runs:
        run_id = int(run["id"])
        if run_id in processed:
            continue
        try:
            archive = client.artifact_archive(GUPIAO_REPO, run_id, GUPIAO_ARTIFACT)
        except GitHubArtifactError as exc:
            print(f"::warning::DXDX Artifact unavailable for run {run_id}: {exc}")
            continue
        processed.add(run_id)
        report = _decode(find_member(archive or {}, "dxdx_report.txt"))
        if not EMAIL_SENT.search(report):
            continue
        raw = find_member(archive or {}, "dxdx_signals.csv")
        if not raw:
            continue
        for row in csv.DictReader(io.StringIO(_decode(raw))):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            signal_date = _signal_date(row, run)
            signal_time = _signal_time(row, signal_date)
            signal_id = build_signal_id(run_id, symbol, signal_time)
            if signal_id in known:
                continue
            event = {field: "" for field in HISTORY_FIELDS}
            event.update({
                "signal_id": signal_id, "symbol": symbol, "signal_date": signal_date, "signal_time": signal_time,
                "signal_price": row.get("close", ""), "source_run_id": str(run_id),
                "source_signal_level": row.get("signal_level", ""), "daily_dxdx": row.get("daily_dxdx", ""),
                "h4_dxdx": row.get("h4_dxdx", ""), "effectiveness": "pending", "sessions_observed": "0",
            })
            history.append(event)
            known.add(signal_id)
    state["processed_gupiao_run_ids"] = sorted(processed)
    return history


def freeze_sector_context(history: list[dict[str, str]], snapshots: list[dict[str, str]], metadata: dict[str, dict[str, str]]) -> None:
    by_date = {row["market_date"]: row for row in snapshots}
    for event in history:
        # Once written, sector ranking is historical evidence and never rewrites.
        if event.get("sector_theme") or event.get("sector_snapshot_run_id") or event.get("strong_sector") in {"是", "否", "未知"}:
            continue
        details = metadata_for(event["symbol"], metadata)
        event.update(details)
        snapshot = by_date.get(event["signal_date"])
        if not snapshot or not details["sector_theme"]:
            event["strong_sector"] = "未知"
            continue
        themes = [snapshot.get(f"rank{rank}_theme", "") for rank in (1, 2, 3)]
        event["sector_snapshot_run_id"] = snapshot.get("run_id", "")
        if details["sector_theme"] in themes:
            event["strong_sector"] = "是"
            event["sector_rank"] = str(themes.index(details["sector_theme"]) + 1)
        else:
            event["strong_sector"] = "否"


def update_performance(history: list[dict[str, str]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for event in history:
        if int(float(event.get("sessions_observed") or 0)) < 20:
            grouped[event["symbol"]].append(event)
    for symbol, events in grouped.items():
        try:
            first = min(date.fromisoformat(item["signal_date"]) for item in events)
            prices = fetch_history(symbol, first)
        except Exception as exc:
            print(f"::warning::{symbol} performance data failed: {exc}")
            continue
        for event in events:
            event.update(calculate_performance(prices, date.fromisoformat(event["signal_date"]), event.get("signal_price")))
            event["last_updated"] = now


def _mature(event: dict[str, str]) -> bool:
    return event.get("effectiveness") in {"有效", "无效", "中性", "无法判定"}


def write_reports(history: list[dict[str, str]]) -> None:
    rows = sorted(history, key=lambda item: (item.get("signal_date", ""), item.get("signal_time", ""), item.get("symbol", "")), reverse=True)
    _write_csv(REPORT_CSV, rows, HISTORY_FIELDS)
    strong = [item for item in history if item.get("strong_sector") == "是"]
    non_strong = [item for item in history if item.get("strong_sector") == "否"]
    matured = [item for item in history if _mature(item)]
    lines = [
        "【美股信号验证器】", f"历史正式推送总数：{len(history)}", f"已成熟10日信号数：{len(matured)}",
        f"强势板块内信号数：{len(strong)}", f"非强势板块信号数：{len(non_strong)}", "",
        "基准：推送 Artifact 记录的 signal_price；T+N 为后续第 N 个实际交易日收盘价。",
        "有效性：10 个交易日内，先触及 +5% 为有效；先触及 -5% 为无效；同日双触及为无法判定。", "", "最近具体推送：",
    ]
    if not rows:
        lines.append("暂无已实际发送邮件的 DXDX 股票推送。")
    for item in rows[:12]:
        lines.append(
            "{symbol}｜{date}｜价 {price}｜{theme}｜排名 {rank}｜强势 {strong}｜T+5 {r5}｜T+10 {r10}｜T+20 {r20}｜MFE10 {mfe}｜MAE10 {mae}｜{effect}".format(
                symbol=item.get("symbol", ""), date=item.get("signal_date", ""), price=item.get("signal_price", ""),
                theme=item.get("sector_theme", "未知"), rank=item.get("sector_rank") or "-", strong=item.get("strong_sector", "未知"),
                r5=item.get("return_5d") or "pending", r10=item.get("return_10d") or "pending", r20=item.get("return_20d") or "pending",
                mfe=item.get("mfe_10d") or "pending", mae=item.get("mae_10d") or "pending", effect=item.get("effectiveness", "pending"),
            )
        )
    REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_tracker() -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Run existing ingestion/statistics and return only material history deltas."""
    _ensure_dirs()
    state = _load_state()
    old_history = copy.deepcopy(_load_history())
    client = GitHubPublicArtifacts()
    snapshots = ingest_sector_snapshots(client, state)
    history = ingest_formal_pushes(client, state, copy.deepcopy(old_history))
    freeze_sector_context(history, snapshots, load_metadata(METADATA_CACHE))
    update_performance(history)
    _write_csv(HISTORY_CSV, history, HISTORY_FIELDS)
    write_reports(history)
    _save_state(state)
    return history, changes_between(old_history, history)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Update the independent signal verifier")
    parser.add_argument("--test-email", action="store_true", help="send SMTP connectivity test only")
    args = parser.parse_args(argv)
    if args.test_email:
        send_connectivity_test()
        print("Tracker email connectivity test sent.")
        return
    history, changes = run_tracker()
    if changes:
        # Delivery failure is intentional: the workflow must fail so this
        # one-time material change is not silently treated as notified.
        send_email(build_subject(changes), build_body(changes, history))
    print(REPORT_TXT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
