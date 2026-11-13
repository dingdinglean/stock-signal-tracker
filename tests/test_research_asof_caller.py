"""The research runner, not only the sector helper, owns the signal-time clock."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from research.prices import write_cache
from research.run import _with_strength_availability, execute
from research.sectors import classify_sector
from research.market import xnys


def _write_history(path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "signal_id": "caller-1",
        "symbol": "TEST",
        "signal_date": "2026-07-06",
        "signal_time": "2026-07-06T08:00:00-04:00",
        "signal_price": "100",
        "push_date": "2026-07-06",
        "push_price": "100",
        "source_run_id": run_id,
        "source_radar": "daily",
        "source_timeframe": "daily",
        "source_signal_level": "B",
        "daily_dxdx": "True",
        "h4_dxdx": "False",
        "industry": "云软件",
        "sector_theme": "云软件",
        "strong_sector": "",
        "sector_rank": "",
        "display_state": "visible",
        "quote_status": "ok",
        "quote_error": "",
        "effectiveness": "",
        "return_1d": "",
        "return_3d": "",
        "return_5d": "",
        "return_10d": "",
        "return_20d": "",
        "mfe_10d": "",
        "mae_10d": "",
        "mfe_20d": "",
        "mae_20d": "",
        "latest_return": "",
        "sessions_observed": "",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def _write_prices(root: Path) -> None:
    days = []
    cursor = date(2026, 6, 1)
    while cursor <= date(2026, 8, 31):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    closes = [100.0 + index * 0.2 for index in range(len(days))]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [price + 1 for price in closes],
            "low": [price - 1 for price in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1000] * len(days),
            "dividends": [0] * len(days),
            "stock_splits": [0] * len(days),
        },
        index=days,
    )
    write_cache(root / "data" / "research_cache" / "prices" / "TEST.csv", frame)
    write_cache(root / "data" / "research_cache" / "prices" / "_idx_GSPC.csv", frame)


def _snapshot(run_id: str) -> dict[str, str]:
    return {
        "market_date": "2026-07-06",
        "run_id": run_id,
        "run_created_at": "2026-07-06T11:00:00Z",
        "rank1_theme": "云软件",
        "rank2_theme": "能源",
        "rank3_theme": "半导体",
    }


def test_execute_keeps_a_ranking_that_finishes_after_the_email_proxy_out_of_the_signal(tmp_path: Path):
    """Email proxy, then a successful radar, then the signal job. The ranking is not signal-time known."""
    signal_run = "signal-run"
    failed_radar = "radar-failed"
    late_radar = "radar-late"
    _write_history(tmp_path / "data" / "signal_history.csv", signal_run)
    snapshots = tmp_path / "data" / "sector_snapshots.csv"
    with snapshots.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_snapshot(failed_radar)))
        writer.writeheader()
        writer.writerow(_snapshot(failed_radar))
        writer.writerow(_snapshot(late_radar))
    (tmp_path / "data" / "research_cache").mkdir(parents=True)
    (tmp_path / "data" / "research_cache" / "strength_jobs.json").write_text(json.dumps({"runs": {
        failed_radar: {
            "job_id": "1", "job_name": "run-radar", "job_conclusion": "failure",
            "job_completed_at": "2026-07-06T11:30:00Z", "job_completed_source": "actions_job_completed_at",
        },
        late_radar: {
            "job_id": "2", "job_name": "run-radar", "job_conclusion": "success",
            "job_completed_at": "2026-07-06T13:45:00Z", "job_completed_source": "actions_job_completed_at",
        },
    }}), encoding="utf-8")
    (tmp_path / "data" / "research_cache" / "run_times.json").write_text(json.dumps({"runs": {
        signal_run: {
            "created_at": "2026-07-06T12:00:00Z",
            "run_started_at": "2026-07-06T12:00:00Z",
            "updated_at": "2026-07-06T20:05:00Z",
            "job_id": "9",
            "job_name": "scan",
            "job_conclusion": "success",
            "job_completed_at": "2026-07-06T20:05:00Z",
            "job_completed_source": "actions_job_completed_at",
        },
    }}), encoding="utf-8")
    _write_prices(tmp_path)
    code, _coverage = execute(tmp_path, full=False, fetch=False, today=date(2026, 8, 20))
    assert code == 0
    with (tmp_path / "output" / "research" / "signals.csv").open(encoding="utf-8-sig") as handle:
        signal = next(csv.DictReader(handle))
    assert signal["sector_asof_time"].startswith("2026-07-06T12:00:00")
    assert signal["entry_time"].startswith("2026-07-06T20:05:00")
    assert signal["asof_sector_status"] == "unknown"
    assert signal["asof_sector_reason"] == "no_snapshot_known_by_proxy_cutoff"
    assert signal["sector_status"] == "unknown"
    with (tmp_path / "output" / "research" / "simulations.csv").open(encoding="utf-8-sig") as handle:
        trades = list(csv.DictReader(handle))
    assert trades
    assert {row["entry_date"] for row in trades} == {"2026-07-07"}
    ledger = (tmp_path / "data" / "signal_history.csv").read_text(encoding="utf-8")
    assert "caller-1" in ledger and ledger.count("\n") == 2


def test_a_failed_signal_job_does_not_enter_and_a_failed_radar_is_not_strong(tmp_path: Path):
    signal_run = "signal-run"
    _write_history(tmp_path / "data" / "signal_history.csv", signal_run)
    snapshots = tmp_path / "data" / "sector_snapshots.csv"
    with snapshots.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_snapshot("radar-failed")))
        writer.writeheader()
        writer.writerow(_snapshot("radar-failed"))
    (tmp_path / "data" / "research_cache").mkdir(parents=True)
    (tmp_path / "data" / "research_cache" / "strength_jobs.json").write_text(json.dumps({"runs": {
        "radar-failed": {
            "job_id": "1", "job_name": "run-radar", "job_conclusion": "failure",
            "job_completed_at": "2026-07-06T11:30:00Z", "job_completed_source": "actions_job_completed_at",
        },
    }}), encoding="utf-8")
    (tmp_path / "data" / "research_cache" / "run_times.json").write_text(json.dumps({"runs": {
        signal_run: {
            "created_at": "2026-07-06T12:00:00Z",
            "run_started_at": "2026-07-06T12:00:00Z",
            "updated_at": "2026-07-06T20:05:00Z",
            "job_id": "9",
            "job_name": "scan",
            "job_conclusion": "failure",
            "job_completed_at": "2026-07-06T20:05:00Z",
            "job_completed_source": "actions_job_completed_at",
        },
    }}), encoding="utf-8")
    _write_prices(tmp_path)
    enriched = _with_strength_availability(
        [_snapshot("radar-failed")],
        {"radar-failed": {
            "job_id": "1", "job_name": "run-radar", "job_conclusion": "failure",
            "job_completed_at": "2026-07-06T11:30:00Z", "job_completed_source": "actions_job_completed_at",
        }},
    )
    assert "available_at" not in enriched[0]
    code, coverage = execute(tmp_path, full=False, fetch=False, today=date(2026, 8, 20))
    assert code == 1
    assert "missing_entry_clock:caller-1" in coverage["quality_gate"]
    with (tmp_path / "output" / "research" / "signals.csv").open(encoding="utf-8-sig") as handle:
        signal = next(csv.DictReader(handle))
    assert signal["entry_time"] == ""
    assert signal["asof_sector_status"] == "unknown"
    sim_path = tmp_path / "output" / "research" / "simulations.csv"
    text = sim_path.read_text(encoding="utf-8-sig")
    assert "caller-1" not in text
    stamped = dict(enriched[0])
    stamped.update({
        "available_at": "2026-07-06T11:30:00Z",
        "availability_source": "actions_job_completed_at",
        "job_id": "1",
        "job_name": "run-radar",
        "job_conclusion": "failure",
    })
    calendar = xnys(2026, 2026)
    assert classify_sector(
        {"云软件"}, [stamped], date(2026, 7, 6), datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc), calendar,
    )["asof_sector_status"] == "unknown"
