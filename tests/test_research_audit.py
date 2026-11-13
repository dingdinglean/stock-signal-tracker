"""Regression tests for the NO-GO audit of 2026-11-13. Thresholds stay at N>=20."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from research.asof import decision_clocks
from research.charts import chart_markers, chart_window_start
from research.market import xnys
from research.patterns import complete_weeks_only
from research.prices import incremental_start
from research.simulate import ledger_benchmark, path_highwater_percent, path_highwater_todate, simulate_trade


def test_frozen_percent_is_not_subtracted_from_a_decimal_index():
    calendar = xnys(2026, 2026)
    # 4.6315% stock versus about -0.7475% index is +5.379 percentage points, not 4.639.
    index = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0], "close": [100.0, 99.2524833]},
        index=[date(2026, 9, 8), date(2026, 9, 9)],
    )
    bench = ledger_benchmark(index, date(2026, 9, 8), 1, calendar)
    assert bench["unit"] == "percent"
    assert bench["start"] == "2026-09-08"
    assert bench["end"] == "2026-09-09"
    assert float(bench["percent"]) == pytest_approx(-0.7475167)
    excess = 4.6315 - float(bench["percent"])
    assert excess == pytest_approx(5.3790167)
    wrong = 4.6315 - (99.2524833 / 100.0 - 1.0)
    assert abs(excess - wrong) > 0.5


def test_highwater_drawdown_is_not_mae():
    calendar = xnys(2026, 2026)
    bars = pd.DataFrame(
        {"open": [100, 108], "high": [101, 110], "low": [99, 105], "close": [100, 109]},
        index=[date(2026, 9, 8), date(2026, 9, 9)],
    )
    drawdown = float(path_highwater_percent(bars, date(2026, 9, 8), 1, calendar))
    mae_percent = (105 / 100 - 1) * 100
    assert drawdown == pytest_approx((105 / 110 - 1) * 100)
    assert drawdown < 0 < mae_percent


def test_entry_after_completion_skips_an_open_the_run_crossed():
    calendar = xnys(2026, 2026)
    days = [date(2026, 7, 6), date(2026, 7, 7)]
    bars = pd.DataFrame(
        {"open": [100, 101], "high": [102, 103], "low": [99, 100], "close": [101, 102], "volume": 1, "stock_splits": 0},
        index=days,
    )
    before_open = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    after_open = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    assert simulate_trade(bars, before_open, calendar, mode="hold", hold_sessions=1)["entry_date"] == "2026-07-06"
    assert simulate_trade(bars, after_open, calendar, mode="hold", hold_sessions=1)["entry_date"] == "2026-07-07"
    clocks = decision_clocks({
        "created_at": "2026-07-06T13:00:00Z", "run_started_at": "2026-07-06T13:05:00Z",
        "status": "completed", "updated_at": "2026-07-06T14:00:00Z",
        "job_completed_at": "2026-07-06T14:00:00Z", "job_completed_source": "actions_job_completed_at",
        "job_id": "99", "job_name": "scan", "job_conclusion": "success",
    })
    assert clocks["bar_cutoff"] == datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    assert clocks["entry_cutoff"] == datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    assert decision_clocks({
        "created_at": "2026-07-06T13:00:00Z", "run_started_at": "2026-07-06T13:05:00Z",
        "job_completed_at": "2026-07-06T14:00:00Z", "job_completed_source": "actions_job_completed_at",
        "job_id": "99", "job_name": "scan", "job_conclusion": "failure",
    })["entry_cutoff"] is None
    assert decision_clocks({"created_at": "2026-07-06T13:00:00Z", "status": "completed", "updated_at": "2026-07-06T14:00:00Z"})["entry_cutoff"] is None
    assert "source-run created_at" not in simulate_trade(bars, after_open, calendar, mode="hold", hold_sessions=1)["entry_rule"]


def test_chart_has_sixty_bars_before_the_signal_and_marks_posterior_breakout():
    days = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(400)]
    anchor = days[200]
    pattern = {
        "status": "double_bottom", "double_bottom": "yes",
        "low1_date": days[100].isoformat(), "low2_date": days[180].isoformat(),
        "breakout_date": "", "breakout_state": "no", "posterior_breakout_date": days[250].isoformat(),
    }
    start = chart_window_start(days, anchor, pattern)
    assert start == days[100]
    assert len([day for day in days if start <= day < anchor]) >= 60
    labels = [label for _day, label in chart_markers({"dxdx": anchor, "push": days[210], "same_day": False}, pattern)]
    assert "低点1" in labels and "后验突破" in labels
    assert "信号时突破" not in labels


def test_incremental_fetch_does_not_restart_at_the_history_floor():
    cached = pd.DataFrame({"close": [1.0]}, index=[date(2026, 1, 5)])
    assert incremental_start(cached, "2024-06-01") == "2025-12-26"
    assert incremental_start(pd.DataFrame(), "2024-06-01") == "2024-06-01"


def test_incomplete_week_is_not_an_official_weekly_bar():
    weekly = pd.DataFrame(
        {"open": [1, 2], "high": [2, 9], "low": [1, 2], "close": [1, 8], "volume": 1, "week_complete": [True, False]},
        index=[date(2026, 9, 4), date(2026, 9, 8)],
    )
    official = complete_weeks_only(weekly)
    assert list(official.index) == [date(2026, 9, 4)]
    assert float(official.iloc[-1]["close"]) == 1


def test_overlapping_flags_are_not_reported_as_the_exclusive_label():
    from research.metrics import six_answers
    winners = [{
        "cohort": "daily", "symbol": "CB", "push_date": "2026-09-18", "return_20d": "1",
        "pattern_status": "double_bottom", "daily_double_bottom": "yes", "daily_platform": "yes",
        "daily_sharp_stabilize": "no", "sector_status": "unknown", "breakout_state": "no",
    }]
    text = six_answers([], winners)[0]["answer"]
    assert "主分类（互斥，双底优先）双底 1/1" in text
    assert "平台 0/1" in text
    assert "daily_platform 1/1" in text
    assert "可重叠检测可以同时为 yes" in text


def test_weekly_low_marker_uses_the_session_that_printed_the_low():
    from research.patterns import weekly_bars
    calendar = xnys(2026, 2026)
    days = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
    lows = [110.0, 106.54, 109.0, 108.0, 108.80]
    frame = pd.DataFrame({"open": lows, "high": [v + 1 for v in lows], "low": lows, "close": lows, "volume": 1}, index=days)
    weekly = weekly_bars(frame, calendar)
    assert weekly.index[0] == date(2026, 6, 12)
    assert float(weekly.iloc[0]["low"]) == 106.54
    assert weekly.iloc[0]["low_date"] == "2026-06-09"
    assert weekly.iloc[0]["low_date"] != "2026-06-12"


def test_unidentified_pattern_does_not_draw_a_double_bottom():
    labels = [label for _day, label in chart_markers(
        {"dxdx": date(2026, 10, 1), "push": date(2026, 10, 2), "same_day": False},
        {"status": "other", "double_bottom": "no", "low1_date": "2026-06-12", "low2_date": "2026-07-01"},
    )]
    assert "低点1" not in labels and "低点2" not in labels


def test_short_window_is_not_a_mature_20_day_drawdown():
    calendar = xnys(2026, 2026)
    days = [date(2026, 9, 8), date(2026, 9, 9)]
    bars = pd.DataFrame({"open": 1, "high": 2, "low": 1, "close": 1, "volume": 1}, index=days)
    assert path_highwater_percent(bars, date(2026, 9, 8), 20, calendar) == ""
    assert path_highwater_todate(bars, date(2026, 9, 8), 20, calendar) != ""


def test_frozen_push_price_is_not_the_same_start_as_the_close_to_close_window():
    from performance import calculate_performance
    calendar = xnys(2026, 2026)
    sessions = list(calendar.sessions_in_range("2026-09-08", "2026-10-10"))
    days = [item.date() for item in sessions]
    closes = [100.0 + index for index in range(len(days))]
    bars = pd.DataFrame({"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes], "close": closes}, index=days)
    push = days[0]
    frozen = 90.0
    perf = calculate_performance(bars, push, frozen)
    end = days[20]
    assert bars.index[20] == end
    frozen_percent = (float(bars.iloc[20]["close"]) / frozen - 1.0) * 100.0
    assert float(perf["return_20d"]) == pytest_approx(frozen_percent, abs=0.01)
    close_to_close = (float(bars.iloc[20]["close"]) / float(bars.iloc[0]["close"]) - 1.0) * 100.0
    assert abs(float(perf["return_20d"]) - close_to_close) > 1
    bench = ledger_benchmark(bars, push, 20, calendar)
    assert bench["start"] == push.isoformat() and bench["end"] == end.isoformat()
    assert float(bench["percent"]) == pytest_approx(close_to_close)


def test_price_return_ignores_the_dividend_column():
    calendar = xnys(2026, 2026)
    days = [date(2026, 9, 8), date(2026, 9, 9)]
    bars = pd.DataFrame(
        {"open": [100, 100], "high": [101, 101], "low": [99, 99], "close": [100, 110], "adj_close": [100, 111], "dividends": [0, 1]},
        index=days,
    )
    assert float(ledger_benchmark(bars, days[0], 1, calendar)["percent"]) == pytest_approx(10.0)
    assert float(ledger_benchmark(bars, days[0], 1, calendar)["percent"]) != pytest_approx(11.0)


def test_scan_job_is_required_and_updated_at_is_not_a_fill_clock():
    from research.asof import choose_scan_job
    assert choose_scan_job([{"id": 7, "name": "scan", "completed_at": "2026-07-06T14:00:00Z", "conclusion": "success"}])["id"] == 7
    assert choose_scan_job([
        {"id": 1, "name": "lint", "completed_at": "2026-07-06T13:00:00Z"},
        {"id": 2, "name": "docs", "completed_at": "2026-07-06T15:00:00Z"},
    ]) is None
    assert choose_scan_job([]) is None
    assert choose_scan_job([{"id": 7, "name": "scan", "completed_at": "2026-07-06T14:00:00Z", "conclusion": "failure"}]) is None
    assert choose_scan_job([{"id": 8, "name": "scan", "completed_at": "2026-07-06T14:00:00Z", "conclusion": "cancelled"}]) is None


def test_a_newly_appended_formal_push_is_not_dropped():
    from publish_changes import merge_history_rows
    import tracker
    old = {field: "" for field in tracker.HISTORY_FIELDS}
    old["signal_id"] = "old"
    new = {field: "" for field in tracker.HISTORY_FIELDS}
    new["signal_id"] = "new-scheduled"
    merged = merge_history_rows([old], [old, new], [old])
    assert {row["signal_id"] for row in merged} == {"old", "new-scheduled"}


def pytest_approx(value, abs=1e-4):
    import pytest
    return pytest.approx(value, abs=abs)
