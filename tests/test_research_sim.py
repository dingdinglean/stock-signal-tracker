from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from research.market import is_half_day, is_session, xnys
from research.metrics import assign_splits
from research.simulate import apply_costs, simulate_trade


def _frame(days, opens, highs, lows, closes):
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1, "stock_splits": 0},
        index=days,
    )


def test_holiday_entry_gap_through_stop_and_same_bar_range():
    calendar = xnys(2026, 2026)
    assert is_session(calendar, datetime.fromisoformat("2026-07-03").date()) is False
    assert is_half_day(calendar, datetime.fromisoformat("2026-11-27").date()) is True
    days = [
        datetime.fromisoformat(day).date()
        for day in ("2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08")
    ]
    bars = _frame(days, [100, 100, 90, 90], [101, 103, 91, 91], [99, 98, 88, 88], [100, 101, 89, 89])
    after_close = datetime(2026, 7, 2, 20, 30, tzinfo=timezone.utc)
    trade = simulate_trade(bars, after_close, calendar, mode="barrier")
    assert trade["entry_date"] == "2026-07-06"
    assert trade["exit_date"] == "2026-07-07"
    assert trade["exit_reason"] == "gap_stop"
    assert trade["ambiguous"] == "no"
    assert float(trade["gross_return"]) < -0.05
    assert trade["gross_return"] == trade["optimistic_gross_return"]

    ambiguous_days = [datetime.fromisoformat("2026-07-06").date()]
    both = _frame(ambiguous_days, [100], [106], [94], [100])
    before_open = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    both_trade = simulate_trade(both, before_open, calendar, mode="barrier", fee_bps=5, slippage_bps=5)
    assert both_trade["entry_date"] == "2026-07-06"
    assert both_trade["ambiguous"] == "yes"
    assert both_trade["exit_reason"] == "same_bar_both"
    assert float(both_trade["gross_return"]) == -0.05
    assert float(both_trade["optimistic_gross_return"]) == 0.05
    assert float(both_trade["net_return"]) < float(both_trade["gross_return"])
    assert apply_costs(100, 110, 0, 0) == pytest.approx(0.1)


def test_hold_censor_and_costs_and_purge():
    calendar = xnys(2026, 2026)
    days = list(calendar.sessions_in_range("2026-07-06", "2026-07-24"))
    days = [day.date() for day in days]
    bars = _frame(days, [100] * len(days), [101] * len(days), [99] * len(days), [100 + index for index in range(len(days))])
    asof = datetime(2026, 7, 2, 20, 30, tzinfo=timezone.utc)
    short = simulate_trade(bars.iloc[:5], asof, calendar, mode="hold", hold_sessions=10)
    assert short["status"] == "censored"
    held = simulate_trade(bars, asof, calendar, mode="hold", hold_sessions=10, fee_bps=0, slippage_bps=0)
    assert held["status"] == "closed"
    assert held["exit_reason"] == "hold_close"
    costly = simulate_trade(bars, asof, calendar, mode="hold", hold_sessions=10, fee_bps=5, slippage_bps=5)
    assert float(costly["net_return"]) < float(held["gross_return"])
    rows = [
        {"cohort": "weekly", "push_date": day.isoformat(), "signal_id": str(index)}
        for index, day in enumerate(days)
    ]
    assign_splits(rows, calendar)
    train = [row for row in rows if row["sample_split"] == "train"]
    oos = [row for row in rows if row["sample_split"] == "oos"]
    purged = [row for row in rows if row["sample_split"] == "purged"]
    assert purged
    cut = rows[0]["split_cut"]
    assert all(row["push_date"] < cut for row in train + purged)
    assert all(row["push_date"] >= cut for row in oos)
    from research.market import session_offset
    from datetime import date

    for row in train:
        window_end = session_offset(calendar, date.fromisoformat(row["push_date"]), 20)
        assert window_end is not None and window_end.isoformat() < cut
