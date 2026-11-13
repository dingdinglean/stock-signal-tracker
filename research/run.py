"""Run the research layer. It reads the ledger and never writes it, and it never sends mail."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from research.asof import choose_scan_job, decision_clocks
from research.charts import render_chart
from research.cohort import assign_cohort
from research.hypothesis import (
    CONCLUSION_RULE,
    DAILY_PARAMS,
    SECTOR_PARAMS,
    STUDY_PARAMS,
    WEEKLY_PARAMS,
)
from research.market import following_sessions, next_entry_session, parse_day, parse_time, xnys
from research.metrics import assign_splits, build_groups, number, six_answers
from research.patterns import complete_weeks_only, completed_daily, detect_pattern, posterior_breakout, weekly_bars
from research.prices import cache_path, fetch_symbol, incremental_start, merge_cache, read_cache
from research.report import render_html, write_csv
from research.sectors import candidate_labels, classify_sector
from research.simulate import (
    benchmark_return, forward_close_return_percent, ledger_benchmark,
    open_to_later_close_percent, path_highwater_percent, path_highwater_todate,
    scale_warning, simulate_trade,
)

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = (
    "data/signal_history.csv",
    "data/sector_snapshots.csv",
    "data/state.json",
    "data/sector_metadata_cache.json",
    "data/events.csv",
    "tracker.py",
    "performance.py",
    "dynamic_tracking.py",
    "notifications.py",
    "email_sender.py",
    "sector_map.py",
    "publish_changes.py",
    "github_artifacts.py",
    ".github/workflows/tracker.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/refresh_constituents.yml",
    ".github/workflows/test_email.yml",
)
PRIORITY = ("IDXX", "ALLE", "STZ", "TDG", "FIS", "XYL", "FICO")
LEDGER_FIELDS = (
    "signal_id", "symbol", "signal_date", "signal_time", "signal_price", "push_date", "push_price",
    "source_run_id", "source_radar", "source_timeframe", "source_signal_level", "daily_dxdx", "h4_dxdx",
    "industry", "sector_theme", "strong_sector", "sector_rank", "display_state", "quote_status", "quote_error",
    "effectiveness", "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
    "mfe_10d", "mae_10d", "mfe_20d", "mae_20d", "latest_return", "sessions_observed",
)


def _run_meta(run: dict, repo: str, workflow: str) -> dict[str, str]:
    return {
        "created_at": str(run.get("created_at") or ""),
        "run_started_at": str(run.get("run_started_at") or ""),
        "updated_at": str(run.get("updated_at") or ""),
        "status": str(run.get("status") or ""),
        "job_completed_at": str(run.get("job_completed_at") or ""),
        "job_completed_source": str(run.get("job_completed_source") or ""),
        "repo": repo,
        "workflow": workflow,
        "run_id": str(run.get("id") or ""),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "time_note": "created_at and run_started_at are run-start metadata, not the email time. updated_at is not an as-of clock.",
    }


def _clock_complete(meta: dict | None) -> bool:
    if not meta:
        return False
    return bool(
        meta.get("job_id")
        and meta.get("job_completed_at")
        and meta.get("job_completed_source") == "actions_job_completed_at"
        and str(meta.get("job_conclusion") or "").lower() == "success"
    )


def _merge_clock(current: dict | None, incoming: dict | None) -> dict:
    """Keep a verified success clock. An empty refresh must not erase it."""
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        if value:
            merged[key] = value
    if _clock_complete(current) and not _clock_complete(incoming):
        for key in ("job_id", "job_name", "job_conclusion", "job_completed_at", "job_completed_source"):
            if (current or {}).get(key):
                merged[key] = current[key]
    return merged


def load_clock_seed(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    signal = payload.get("signal_runs") if isinstance(payload.get("signal_runs"), dict) else {}
    strength = payload.get("strength_runs") if isinstance(payload.get("strength_runs"), dict) else {}
    return signal, strength


def _read_run_cache(cache_file: Path) -> dict[str, dict]:
    if not cache_file.exists():
        return {}
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    runs = payload.get("runs", {}) if isinstance(payload, dict) else {}
    return runs if isinstance(runs, dict) else {}


def _write_run_cache(cache_file: Path, runs: dict[str, dict]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(cache_file)


def load_strength_jobs(
    cache_file: Path,
    run_ids: set[str],
    *,
    fetch: bool,
    seed: dict[str, dict] | None = None,
    client=None,
) -> dict[str, dict]:
    """Scan-job completion for the snapshot run ids. Start time is not availability."""
    found = {str(key): dict(value) for key, value in (seed or {}).items()}
    for key, value in _read_run_cache(cache_file).items():
        found[str(key)] = _merge_clock(found.get(str(key)), value)
    if not fetch:
        return found
    from research.github_api import GitHubArtifactError, GitHubRateLimitError

    if client is None:
        from research.github_api import GitHubPublicArtifacts

        client = GitHubPublicArtifacts()
    ids = {str(run_id) for run_id in run_ids if str(run_id)}
    for run_id in sorted(ids):
        if _clock_complete(found.get(run_id)):
            continue
        if getattr(client, "halted", False):
            break
        try:
            jobs = client._get(
                f"https://api.github.com/repos/dingdinglean/strong-pullback-screener/actions/runs/{run_id}/jobs?per_page=100"
            ).json()
        except GitHubRateLimitError:
            break
        except GitHubArtifactError:
            continue
        chosen = choose_scan_job(list(jobs.get("jobs") or []))
        if chosen is None:
            continue
        found[run_id] = _merge_clock(found.get(run_id), {
            "job_id": str(chosen.get("id") or ""),
            "job_name": str(chosen.get("name") or ""),
            "job_conclusion": str(chosen.get("conclusion") or ""),
            "job_completed_at": str(chosen.get("completed_at") or ""),
            "job_completed_source": "actions_job_completed_at",
        })
    _write_run_cache(cache_file, found)
    return found


def _with_strength_availability(snapshots: list[dict[str, str]], jobs: dict[str, dict]) -> list[dict[str, str]]:
    """Copy snapshots. Availability is the scan-job completion, never run_created_at."""
    enriched = []
    for row in snapshots:
        item = dict(row)
        meta = jobs.get(str(row.get("run_id") or ""), {})
        if (
            meta.get("job_completed_source") == "actions_job_completed_at"
            and meta.get("job_id")
            and meta.get("job_completed_at")
            and str(meta.get("job_conclusion") or "").lower() == "success"
        ):
            item["available_at"] = meta["job_completed_at"]
            item["availability_source"] = "actions_job_completed_at"
            item["job_id"] = meta["job_id"]
            item["job_name"] = meta.get("job_name", "")
            item["job_conclusion"] = meta.get("job_conclusion", "")
        enriched.append(item)
    return enriched


def _points(left: str, right: str) -> str:
    if not left or not right:
        return ""
    try:
        return f"{float(left) - float(right):.6f}"
    except ValueError:
        return ""


def _close_return(frame, push: date, sessions: int, calendar) -> str:
    if frame is None or getattr(frame, "empty", True):
        return ""
    return forward_close_return_percent(frame, push, sessions, calendar)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _protected(root: Path) -> dict[str, str]:
    return {rel: _sha(root / rel) for rel in PROTECTED}


def _baseline_ids(root: Path) -> set[str]:
    path = root / "research" / "baseline_frozen_ids.json"
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload.get("rows") or {})


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_run_index(
    cache_file: Path,
    *,
    fetch: bool,
    needed: set[str] | None = None,
    seed: dict[str, dict] | None = None,
    client=None,
) -> tuple[dict[str, dict], list[str]]:
    """Clock cache for the ledger's source run ids. A failed request keeps saved clocks."""
    found = {str(key): dict(value) for key, value in (seed or {}).items()}
    for key, value in _read_run_cache(cache_file).items():
        found[str(key)] = _merge_clock(found.get(str(key)), value)
    errors: list[str] = []
    if not fetch:
        return found, errors
    from research.github_api import GitHubArtifactError, GitHubRateLimitError

    if client is None:
        from research.github_api import GitHubPublicArtifacts

        client = GitHubPublicArtifacts()
    for run_id in sorted(str(item) for item in (needed or set()) if str(item)):
        if getattr(client, "halted", False):
            errors.append(f"run {run_id}: skipped after rate limit")
            break
        current = dict(found.get(run_id, {}))
        if not current.get("created_at"):
            try:
                payload = client._get(f"https://api.github.com/repos/dingdinglean/gupiao/actions/runs/{run_id}").json()
            except GitHubRateLimitError as exc:
                errors.append(f"run {run_id}: {exc}")
                break
            except GitHubArtifactError as exc:
                errors.append(f"run {run_id}: {exc}")
                payload = {}
            if payload.get("created_at") or payload.get("run_started_at"):
                current = _merge_clock(current, _run_meta(payload, "dingdinglean/gupiao", str(payload.get("name") or "")))
        if _clock_complete(current):
            found[run_id] = current
            continue
        if getattr(client, "halted", False):
            found[run_id] = current
            errors.append(f"jobs {run_id}: skipped after rate limit")
            break
        try:
            jobs = client._get(f"https://api.github.com/repos/dingdinglean/gupiao/actions/runs/{run_id}/jobs?per_page=100").json()
        except GitHubRateLimitError as exc:
            errors.append(f"jobs {run_id}: {exc}")
            found[run_id] = current
            break
        except GitHubArtifactError as exc:
            errors.append(f"jobs {run_id}: {exc}")
            found[run_id] = current
            continue
        chosen = choose_scan_job(list(jobs.get("jobs") or []))
        if chosen is not None:
            current = _merge_clock(current, {
                "job_id": str(chosen.get("id") or ""),
                "job_name": str(chosen.get("name") or ""),
                "job_conclusion": str(chosen.get("conclusion") or ""),
                "job_completed_at": str(chosen.get("completed_at") or ""),
                "job_completed_source": "actions_job_completed_at",
            })
        if current:
            found[run_id] = current
    _write_run_cache(cache_file, found)
    return found, errors


def _prices_for(root: Path, symbols: list[str], *, fetch: bool, today: date) -> tuple[dict[str, object], dict[str, str]]:
    cache_root = root / "data" / "research_cache" / "prices"
    frames = {}
    errors = {}
    end = (today + timedelta(days=2)).isoformat()
    for symbol in symbols:
        path = cache_path(cache_root, symbol)
        error = ""
        if fetch:
            cached = read_cache(path)
            fresh, error = fetch_symbol(symbol, incremental_start(cached, STUDY_PARAMS.price_start), end)
            merge_cache(path, fresh)
        frame = read_cache(path)
        frames[symbol] = frame
        if frame.empty:
            errors[symbol] = error or f"{symbol}: 缓存为空"
    return frames, errors


def _pattern_fields(prefix: str, pattern, posterior: dict[str, str]) -> dict[str, str]:
    raw = pattern.as_dict()
    fields = {f"{prefix}{key}": str(value) for key, value in raw.items()}
    fields[f"{prefix}posterior_breakout_state"] = posterior.get("posterior_breakout_state", "")
    fields[f"{prefix}posterior_breakout_date"] = posterior.get("posterior_breakout_date", "")
    return fields


def _study_pattern(cohort: str, daily_pattern, weekly_pattern) -> tuple[str, str]:
    chosen = weekly_pattern if cohort == "weekly" else daily_pattern
    if cohort not in {"daily", "weekly"}:
        return "not_applicable", "not_applicable"
    return chosen.status, chosen.breakout_state


def execute(
    root: Path,
    *,
    full: bool,
    fetch: bool,
    today: date | None = None,
    history_file: Path | None = None,
) -> tuple[int, dict]:
    today = today or datetime.now(timezone.utc).date()
    before = _protected(root)
    history_path = history_file or (root / "data" / "signal_history.csv")
    history = _read_csv(history_path)
    baseline_ids = _baseline_ids(root)
    frozen = [(row.get("signal_id"), row.get("push_price"), row.get("signal_price")) for row in history]
    signal_seed, strength_seed = load_clock_seed(root / "research" / "source_clocks.json")
    clock_client = None
    if fetch:
        from research.github_api import GitHubPublicArtifacts

        clock_client = GitHubPublicArtifacts()
    raw_snapshots = _read_csv(root / "data" / "sector_snapshots.csv")
    snapshots = _with_strength_availability(
        raw_snapshots,
        load_strength_jobs(
            root / "data" / "research_cache" / "strength_jobs.json",
            {str(row.get("run_id") or "") for row in raw_snapshots},
            fetch=fetch,
            seed=strength_seed,
            client=clock_client,
        ),
    )
    needed_runs = {str(row.get("source_run_id") or "") for row in history if row.get("source_run_id")}
    run_index, run_errors = load_run_index(
        root / "data" / "research_cache" / "run_times.json",
        fetch=fetch,
        needed=needed_runs,
        seed=signal_seed,
        client=clock_client,
    )
    symbols = sorted({row.get("symbol", "").upper() for row in history if row.get("symbol")})
    frames, price_errors = _prices_for(root, [*symbols, "^GSPC"], fetch=fetch, today=today)
    calendar = xnys(2024, today.year + 1)
    now = datetime.now(timezone.utc)
    index_bars = frames.get("^GSPC")
    research_rows: list[dict] = []
    sim_rows: list[dict] = []
    chart_inputs: list[dict] = []
    for event in history:
        cohort = assign_cohort(event)
        row = {field: event.get(field, "") for field in LEDGER_FIELDS}
        row["cohort"] = cohort
        row["ledger_membership"] = "baseline_71" if row["signal_id"] in baseline_ids else "appended"
        row["loss_hide_applied"] = "no"
        info = run_index.get(str(event.get("source_run_id") or ""))
        clocks = decision_clocks(info)
        asof = clocks["bar_cutoff"]
        entry_asof = clocks["entry_cutoff"]
        row["asof_time"] = asof.isoformat() if asof else ""
        row["asof_time_quality"] = clocks["bar_quality"]
        row["entry_time"] = entry_asof.isoformat() if entry_asof else ""
        row["entry_time_quality"] = clocks["entry_quality"]
        row["asof_time_evidence"] = clocks["evidence"]
        row["asof_run_created_at"] = str((info or {}).get("created_at") or "")
        row["asof_run_started_at"] = str((info or {}).get("run_started_at") or "")
        row["asof_run_updated_at"] = str((info or {}).get("updated_at") or "")
        row["job_completed_at"] = str((info or {}).get("job_completed_at") or "")
        row["entry_job_id"] = str((info or {}).get("job_id") or "")
        row["entry_job_name"] = str((info or {}).get("job_name") or "")
        row["entry_job_conclusion"] = str((info or {}).get("job_conclusion") or "")
        row["entry_job_source"] = str((info or {}).get("job_completed_source") or "")
        row["entry_workflow"] = "long_screen.yml" if row.get("source_radar") == "weekly_monthly" else "screen.yml"
        row["entry_workflow_evidence"] = (
            "Pinned gupiao commit 3e95f1613f8c36ce1bf986ce07cdfc6fe25ffe34: "
            "screen.yml and long_screen.yml each run one synchronous scan job before artifacts. "
            "completed_at is an upper bound on the email, not the send timestamp. updated_at is not used."
        )
        row["asof_time_source"] = clocks["bar_quality"]
        row["sector_asof_time"] = row["asof_time"]
        row["sector_asof_rule"] = "信号前板块使用 created_at 与 run_started_at 的较早者。成功扫描 job 的 completed_at 只用于成交，不把更晚的排名当成信号时已知。"
        symbol = row["symbol"].upper()
        frame = frames.get(symbol)
        push = parse_day(row.get("push_date"))
        if index_bars is not None and push is not None:
            bench5 = ledger_benchmark(index_bars, push, 5, calendar)
            bench10 = ledger_benchmark(index_bars, push, 10, calendar)
            bench20 = ledger_benchmark(index_bars, push, 20, calendar)
            row["benchmark_5d"] = bench5["percent"]
            row["benchmark_10d"] = bench10["percent"]
            row["benchmark_20d"] = bench20["percent"]
            row["benchmark_20d_start"] = bench20["start"]
            row["benchmark_20d_end"] = bench20["end"]
            row["benchmark_unit"] = "percent"
            row["benchmark_label"] = (
                "指数是 push_date 收盘到其后第 N 个交易日收盘的百分数，不是 SPY，也不是分红总收益。"
                "冻结 return_Nd 的起点是 push_price，不保证等于 push_date 收盘，所以冻结收益减指数不叫超额。"
                "可比超额用同一起点的股票收盘。"
            )
            close5 = _close_return(frame, push, 5, calendar)
            close10 = _close_return(frame, push, 10, calendar)
            close20 = _close_return(frame, push, 20, calendar)
            row["comparable_return_5d"] = close5
            row["comparable_return_10d"] = close10
            row["comparable_return_20d"] = close20
            row["comparable_excess_5d"] = _points(close5, bench5["percent"])
            row["comparable_excess_10d"] = _points(close10, bench10["percent"])
            row["comparable_excess_20d"] = _points(close20, bench20["percent"])
            row["return_basis"] = "price_return"
            row["total_return_used"] = "no"
            row["adjustment_note"] = (
                "Yahoo auto_adjust=False 的 Close 是拆股回溯后的价格收益，不含分红，不是 Adj Close 总收益。"
                "^GSPC 收盘是指数价格收益，不是全收益指数。可比超额只在这一种价格收益口径内相减。"
            )
            row["comparable_window_start"] = bench20["start"]
            row["comparable_window_end"] = bench20["end"]
        else:
            row["benchmark_10d"] = ""
            row["benchmark_20d"] = ""
            row["benchmark_20d_start"] = ""
            row["benchmark_20d_end"] = ""
            row["benchmark_unit"] = "percent"
            row["benchmark_label"] = "benchmark_missing"
            for field in (
                "comparable_return_5d", "comparable_return_10d", "comparable_return_20d",
                "comparable_excess_5d", "comparable_excess_10d", "comparable_excess_20d",
                "comparable_window_start", "comparable_window_end", "benchmark_5d",
            ):
                row[field] = ""
            row["return_basis"] = "price_return"
            row["total_return_used"] = "no"
            row["adjustment_note"] = "行情缺失，没有把 Adj Close 拿来填价格收益。"
        daily_pattern = None
        weekly_pattern = None
        if asof is None or frame is None or getattr(frame, "empty", True):
            reason = "missing_push_timestamp" if asof is None else price_errors.get(symbol, "no_prices")
            from research.patterns import _unknown

            daily_pattern = _unknown(reason)
            weekly_pattern = _unknown(reason)
            row.update(_pattern_fields("daily_", daily_pattern, {"posterior_breakout_state": "unknown", "posterior_breakout_date": ""}))
            row.update(_pattern_fields("weekly_", weekly_pattern, {"posterior_breakout_state": "unknown", "posterior_breakout_date": ""}))
        else:
            asof_daily = completed_daily(frame, asof, calendar, STUDY_PARAMS.close_buffer_minutes)
            full_daily = completed_daily(frame, now, calendar, STUDY_PARAMS.close_buffer_minutes)
            asof_weekly = weekly_bars(asof_daily, calendar)
            full_weekly = weekly_bars(full_daily, calendar)
            official_weekly = complete_weeks_only(asof_weekly)
            daily_pattern = detect_pattern(asof_daily, DAILY_PARAMS, timeframe="daily")
            weekly_pattern = detect_pattern(official_weekly, WEEKLY_PARAMS, timeframe="weekly")
            partial_weekly = detect_pattern(asof_weekly, WEEKLY_PARAMS, timeframe="weekly")
            row.update(_pattern_fields("daily_", daily_pattern, posterior_breakout(full_daily, daily_pattern)))
            row.update(_pattern_fields("weekly_", weekly_pattern, posterior_breakout(complete_weeks_only(full_weekly), weekly_pattern)))
            row["weekly_partial_pattern_status"] = partial_weekly.status
            row["weekly_partial_is_auxiliary"] = "yes"
            row["weekly_official_pattern_excludes_partial"] = "yes"
            if len(asof_weekly):
                row["weekly_last_bar_date"] = str(asof_weekly.index[-1])
                row["weekly_last_bar_complete"] = "yes" if bool(asof_weekly.iloc[-1]["week_complete"]) else "no"
            else:
                row["weekly_last_bar_date"] = ""
                row["weekly_last_bar_complete"] = ""
            chart_inputs.append({"event": row, "frame": full_daily, "pattern": weekly_pattern if cohort == "weekly" else daily_pattern})
        status, breakout = _study_pattern(cohort, daily_pattern, weekly_pattern)
        row["pattern_status"] = status
        row["breakout_state"] = breakout
        sector = classify_sector(candidate_labels(event), snapshots, push, asof, calendar, SECTOR_PARAMS, now)
        row.update(sector)
        row["sector_status"] = {"strong": "strong", "not_strong": "not_strong"}.get(sector["asof_sector_status"], "unknown")
        row["posterior_sector_entered"] = sector["posterior_10d_entered_top3"]
        row["highwater_dd_5d"] = path_highwater_percent(frame, push, 5, calendar) if push is not None else ""
        row["highwater_dd_10d"] = path_highwater_percent(frame, push, 10, calendar) if push is not None else ""
        row["highwater_dd_20d"] = path_highwater_percent(frame, push, 20, calendar) if push is not None else ""
        row["highwater_dd_20d_todate"] = path_highwater_todate(frame, push, 20, calendar) if push is not None else ""
        row["highwater_dd_20d_status"] = "mature" if row["highwater_dd_20d"] else ("censored" if row["highwater_dd_20d_todate"] else "")
        row["highwater_intraday_order"] = "unknown; high is applied before low, a conservative path assumption"
        event_day = parse_day(row.get("posterior_first_top3_date"))
        row["return_after_top3_note"] = "事件日收盘起的收益是后验描述。该日收盘时排名尚未发布，不能成交，也不能当成可实现策略结果。"
        row["tradable_after_top3_note"] = "可交易入场只使用该事件日强势雷达 scan job completed_at 之后的下一档开盘。run_created_at 不是公开时间。没有 job id 则为未知。"
        if event_day is None or frame is None:
            row["return_after_top3_10d"] = ""
            row["return_after_top3_20d"] = ""
            row["highwater_dd_after_top3_10d"] = ""
            row["highwater_dd_after_top3_20d"] = ""
            row["highwater_dd_after_top3_20d_todate"] = ""
            row["tradable_after_top3_10d"] = ""
            row["tradable_after_top3_20d"] = ""
            row["tradable_after_top3_entry_date"] = ""
        else:
            row["return_after_top3_10d"] = forward_close_return_percent(frame, event_day, 10, calendar)
            row["return_after_top3_20d"] = forward_close_return_percent(frame, event_day, 20, calendar)
            row["highwater_dd_after_top3_10d"] = path_highwater_percent(frame, event_day, 10, calendar)
            row["highwater_dd_after_top3_20d"] = path_highwater_percent(frame, event_day, 20, calendar)
            row["highwater_dd_after_top3_20d_todate"] = path_highwater_todate(frame, event_day, 20, calendar)
            published = parse_time(row.get("posterior_first_top3_available_at"))
            tradable_day = next_entry_session(calendar, published) if published is not None else None
            row["tradable_after_top3_entry_date"] = tradable_day.isoformat() if tradable_day else ""
            row["tradable_after_top3_10d"] = open_to_later_close_percent(frame, tradable_day, 10, calendar) if tradable_day else ""
            row["tradable_after_top3_20d"] = open_to_later_close_percent(frame, tradable_day, 20, calendar) if tradable_day else ""
            row["tradable_after_top3_note"] = (
                "可交易入场是该事件日强势雷达 scan job completed_at 之后的下一档开盘，不是 run_created_at。"
                "没有 job id 和 completed_at 则为未知。不用更晚日期的排名。"
            )
        research_rows.append(row)
        if cohort not in {"daily", "weekly"} or entry_asof is None:
            continue
        warning, detail = scale_warning(frame, push, number(row.get("push_price")) or 0.0) if push else ("yes", "no_push_date")
        cost_grid = (
            ("zero", 0.0, 0.0),
            ("fee_only", STUDY_PARAMS.fee_bps, 0.0),
            ("slippage_only", 0.0, STUDY_PARAMS.slippage_bps),
            ("fee_and_slippage", STUDY_PARAMS.fee_bps, STUDY_PARAMS.slippage_bps),
        )
        plans = [("barrier_5", "barrier", None), ("hold_5", "hold", 5), ("hold_10", "hold", 10), ("hold_20", "hold", 20)]
        for strategy, mode, hold in plans:
            for cost_name, fee, slip in cost_grid:
                trade = simulate_trade(
                    frame, entry_asof, calendar, mode=mode, hold_sessions=hold,
                    take_profit=STUDY_PARAMS.take_profit, stop_loss=STUDY_PARAMS.stop_loss,
                    fee_bps=fee, slippage_bps=slip,
                )
                bench, bench_note = ("", "")
                exit_day = parse_day(trade.get("exit_date"))
                entry_day = parse_day(trade.get("entry_date"))
                if trade.get("status") == "closed":
                    bench, bench_note = benchmark_return(index_bars, entry_day, exit_day)
                trade.update({
                    "signal_id": row["signal_id"], "symbol": symbol, "cohort": cohort,
                    "sector_status": row["sector_status"], "pattern_status": row["pattern_status"],
                    "strategy": strategy,
                    "cost_mode": cost_name, "fee_bps": fee, "slippage_bps": slip,
                    "scale_warning": warning, "scale_detail": detail,
                    "included_in_mean": "yes" if trade.get("status") == "closed" and warning == "no" else "no",
                    "benchmark_return": bench, "benchmark_note": bench_note,
                    "entry_rule": (
                        "next NYSE open at or after successful scan-job completed_at "
                        f"{row['entry_job_name']} id {row['entry_job_id']}. "
                        "Conservative upper bound, not the exact email time, and not created_at. "
                        "Signal-time sector uses the earlier created_at/run_started_at proxy."
                    ),
                    "sample_split": "", "split_cut": "",
                })
                sim_rows.append(trade)
    assign_splits(research_rows, calendar, STUDY_PARAMS)
    split_of = {row["signal_id"]: (row.get("sample_split", ""), row.get("split_cut", "")) for row in research_rows}
    for trade in sim_rows:
        trade["sample_split"], trade["split_cut"] = split_of.get(trade["signal_id"], ("", ""))
    baseline_rows = [row for row in research_rows if row["signal_id"] in baseline_ids]
    groups: list[dict] = []
    answers: list[dict] = []
    if baseline_rows:
        baseline_groups = build_groups(baseline_rows, STUDY_PARAMS)
        for item in baseline_groups:
            item["ledger_scope"] = "baseline_71"
        groups.extend(baseline_groups)
        for item in six_answers(baseline_groups, baseline_rows, STUDY_PARAMS):
            item["ledger_scope"] = "baseline_71"
            item["answer"] = "账本范围 baseline_71，只含原冻结 signal_id，不含之后追加的正式推送。" + item["answer"]
            answers.append(item)
    current_scope = f"current_{len(history)}"
    current_groups = build_groups(research_rows, STUDY_PARAMS)
    for item in current_groups:
        item["ledger_scope"] = current_scope
    groups.extend(current_groups)
    for item in six_answers(current_groups, research_rows, STUDY_PARAMS):
        item["ledger_scope"] = current_scope
        item["answer"] = f"账本范围 {current_scope}，含原冻结记录和之后追加的正式推送。" + item["answer"]
        answers.append(item)
    charts = _charts(chart_inputs, research_rows, price_errors)
    pins = {}
    pin_path = root / "research" / "source_pins.json"
    if pin_path.exists():
        pins = json.loads(pin_path.read_text(encoding="utf-8"))
    manifest_path = root / "research" / "baseline_manifest.json"
    baseline_sha = ""
    if manifest_path.exists():
        baseline_sha = json.loads(manifest_path.read_text(encoding="utf-8")).get("git_sha", "")
    study = [row for row in research_rows if row["cohort"] in {"daily", "weekly"}]
    appended = [row for row in research_rows if row["signal_id"] not in baseline_ids]
    appended_text = " | ".join(
        f"{row['signal_id']} {row['symbol']} cohort={row['cohort']} signal={row.get('signal_date')} "
        f"push={row.get('push_date')} price={row.get('push_price')} run={row.get('source_run_id')} "
        f"return_5d={row.get('return_5d') or 'immature'} return_10d={row.get('return_10d') or 'immature'} "
        f"return_20d={row.get('return_20d') or 'immature'}"
        for row in appended
    ) or "none"
    coverage = {
        "baseline_git_sha": baseline_sha,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ledger_input": str(history_path),
        "formal_pushes": str(len(history)),
        "baseline_formal_pushes": str(sum(row["signal_id"] in baseline_ids for row in research_rows)),
        "current_formal_pushes": str(len(history)),
        "appended_formal_pushes": appended_text,
        "baseline_daily_study": str(sum(row["cohort"] == "daily" for row in baseline_rows)),
        "baseline_weekly_study": str(sum(row["cohort"] == "weekly" for row in baseline_rows)),
        "current_daily_study": str(sum(row["cohort"] == "daily" for row in research_rows)),
        "current_weekly_study": str(sum(row["cohort"] == "weekly" for row in research_rows)),
        "daily_study": str(sum(row["cohort"] == "daily" for row in research_rows)),
        "weekly_study": str(sum(row["cohort"] == "weekly" for row in research_rows)),
        "monthly_excluded": str(sum(row["cohort"] == "monthly_excluded" for row in research_rows)),
        "h4_excluded": str(sum(row["cohort"] == "h4_excluded" for row in research_rows)),
        "excluded_other": str(sum(row["cohort"] == "excluded_other" for row in research_rows)),
        "hidden_included_in_daily_weekly": str(sum(row["display_state"] == "hidden" for row in study)),
        "pattern_unknown_in_study": str(sum(row["pattern_status"] == "unknown" for row in study)),
        "sector_unknown_in_study": str(sum(row["sector_status"] == "unknown" for row in study)),
        "sector_strong_in_study": str(sum(row["sector_status"] == "strong" for row in study)),
        "sector_not_strong_in_study": str(sum(row["sector_status"] == "not_strong" for row in study)),
        "missing_asof_in_study": str(sum(not row["asof_time"] for row in study)),
        "price_errors": str(len(price_errors)),
        "price_error_detail": " | ".join(f"{key}: {value}" for key, value in sorted(price_errors.items())[:12]),
        "run_time_errors": " | ".join(run_errors),
        "gupiao_commit": pins.get("gupiao", {}).get("commit", ""),
        "screener_commit": pins.get("strong_pullback_screener", {}).get("commit", ""),
        "sector_history": "data/sector_snapshots.csv 的 market_date 是市场日。run_created_at 只说明该雷达运行何时创建，不作为价格时钟，也不把不完整 Top3 或不同词汇的行业名记成非强势。",
        "asof_clock": "K线用 created_at/run_started_at 的较早者，这不是发布时间。信号前板块用同一个较早代理，不用扫描 job 完成上界。成交只用成功扫描 job 的 completed_at；失败、取消或没有 job id 不模拟。updated_at 不代替。",
        "maturity": "T+5、T+10、T+20 都在这次独立研究里跟踪。定时 Research dry-run 在追踪提交之后重算，失败不写原账本。",
        "baseline_policy": "基线仍是 71 条正式推送的原始哈希。之后定时新增的 signal_id 由 publish_changes.merge_history_rows 并入，不删除旧 id。",
        "parameters": CONCLUSION_RULE,
        "costs": f"费用 {STUDY_PARAMS.fee_bps} bps 与滑点 {STUDY_PARAMS.slippage_bps} bps 分开计算，并给出两者都加、以及零成本。没有用全样本搜索这组数字。",
        "artifact_access": "本仓库没有 Pages 站点。完整 HTML/CSV 由 Research dry-run 工作流上传为 Actions artifact，名称 signal-research-report-<run_id>。",
        "adjustment": "模拟成交用 Yahoo auto_adjust=False 的开高低收（拆股已回溯到整段，分红不在 OHLC 里）。冻结 push_price 不参与成交，也不被改写。",
        "network": "full" if full else "dry-run",
        "clock_auth": (
            "Workflow GITHUB_TOKEN is sent when the environment has it and is limited to this repository. "
            "A cross-repo Actions 403 does not become a clock. Known success jobs stay in research/source_clocks.json "
            "and the Actions cache. Rate limits stop further requests instead of deleting those clocks."
        ),
    }
    quality = _quality_gate(study, sim_rows, charts)
    coverage["quality_gate"] = "pass" if not quality else "fail: " + ", ".join(quality)
    out = root / "output" / "research"
    signal_fields = list(research_rows[0].keys()) if research_rows else ["signal_id"]
    write_csv(out / "signals.csv", research_rows, signal_fields)
    write_csv(out / "groups.csv", groups)
    write_csv(out / "simulations.csv", sim_rows)
    write_csv(out / "coverage.csv", [coverage])
    write_csv(out / "conclusions.csv", answers)
    from research.charts import ohlc_audit_rows
    ohlc_rows = []
    for item in research_rows:
        if item.get("symbol") not in PRIORITY or item.get("cohort") not in {"daily", "weekly"}:
            continue
        prefix = "weekly_" if item.get("cohort") == "weekly" else "daily_"
        pattern = {"status": item.get("pattern_status"), "double_bottom": item.get(f"{prefix}double_bottom"),
                   "low1_date": item.get(f"{prefix}low1_date", ""), "low2_date": item.get(f"{prefix}low2_date", "")}
        ohlc_rows.extend(ohlc_audit_rows(frames.get(item["symbol"].upper()), item, pattern))
    write_csv(out / "chart_ohlc.csv", ohlc_rows)
    render_html(out / "report.html", coverage=coverage, answers=answers, groups=groups, sims=sim_rows, charts=charts)
    after = _protected(root)
    changed = [rel for rel in PROTECTED if before[rel] != after[rel]]
    if changed or frozen != [(row.get("signal_id"), row.get("push_price"), row.get("signal_price")) for row in history]:
        raise RuntimeError(f"研究过程改动了受保护文件或冻结价格: {changed}")
    missing_prices = [symbol for symbol in symbols if symbol in price_errors]
    if quality or not study or (full and missing_prices):
        return 1, coverage
    return 0, coverage


def _quality_gate(study: list[dict], sim_rows: list[dict], charts: list[dict]) -> list[str]:
    """Dry-run and full both fail closed when the core evidence is missing.

    A missing clock stays unknown. It is not filled from the push date. The
    workflow must still be red when that happens for every studied push, when
    no trade can be simulated, or when a priced priority chart has no image.
    """
    reasons: list[str] = []
    if not study:
        return ["no_daily_or_weekly_rows"]
    missing = [row["signal_id"] for row in study if not row.get("asof_time")]
    if missing:
        reasons.append(f"missing_asof:{len(missing)}")
    if not sim_rows:
        reasons.append("empty_simulations")
    priority = [row for row in study if row.get("symbol") in PRIORITY and row.get("asof_time")]
    if priority and not any(chart.get("png") for chart in charts):
        reasons.append("no_chart_png")
    return reasons


def _charts(chart_inputs: list[dict], rows: list[dict], price_errors: dict[str, str]) -> list[dict]:
    by_id = {item["event"]["signal_id"]: item for item in chart_inputs}
    chosen: list[dict] = []
    seen: set[str] = set()

    def add(row: dict, note: str) -> None:
        if row["signal_id"] in seen:
            return
        seen.add(row["signal_id"])
        item = by_id.get(row["signal_id"])
        title = f"{row['symbol']} {row['cohort']} {row['push_date']}"
        if item is None:
            reason = price_errors.get(row["symbol"].upper(), "不在日/周研究样本或缺少 as-of 行情")
            chosen.append({"title": title, "note": note + " " + reason, "png": None})
            return
        pattern_dict = item["pattern"].as_dict()
        prefix = "weekly_" if row.get("cohort") == "weekly" else "daily_"
        pattern_dict["posterior_breakout_date"] = row.get(f"{prefix}posterior_breakout_date", "")
        png, error = render_chart(item["frame"], row, pattern_dict)
        posterior = pattern_dict.get("posterior_breakout_date") or "无"
        chosen.append({"title": title, "note": note + (" " + error if error else f" 图中低点来自信号当时的判断。后验突破日期：{posterior}，与信号时突破分开标注。"), "png": png})

    for symbol in PRIORITY:
        matches = [row for row in rows if row["symbol"] == symbol and row["cohort"] in {"daily", "weekly"}]
        if not matches:
            others = [row for row in rows if row["symbol"] == symbol]
            if not others:
                chosen.append({"title": symbol, "note": "历史里没有这只股票的正式推送，不绘制。", "png": None})
            for row in others:
                add(row, "不在日/周研究样本。")
            continue
        for row in matches:
            add(row, "优先案例。")
    ranked = [row for row in rows if row["cohort"] in {"daily", "weekly"} and number(row.get("return_20d")) is not None]
    ranked.sort(key=lambda row: float(row["return_20d"]))
    for row in ranked[:2] + ranked[-2:]:
        add(row, "按已成熟 20 日收益选取的两端案例。")
    return chosen[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research formal daily and weekly pushes. Never sends email.")
    parser.add_argument("--full", action="store_true", help="fail if a study symbol has no price or no as-of clock")
    parser.add_argument("--dry-run", action="store_true", help="same research, still no email and no ledger writes; gaps do not fail the process")
    parser.add_argument("--skip-network", action="store_true", help="use only the local cache")
    parser.add_argument("--history", type=Path, default=None, help="read this ledger instead of data/signal_history.csv; the file is never written")
    args = parser.parse_args(argv)
    if not args.full and not args.dry_run:
        args.dry_run = True
    code, coverage = execute(ROOT, full=args.full, fetch=not args.skip_network, history_file=args.history)
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
