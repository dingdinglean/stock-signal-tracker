from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research.hypothesis import DAILY_PARAMS
from research.market import xnys
from research.patterns import completed_daily, detect_pattern, posterior_breakout, weekly_bars


def _bars(closes, lows=None, highs=None, start="2026-01-05"):
    index = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            "open": list(map(float, closes)),
            "high": list(map(float, highs if highs is not None else [value + 1 for value in closes])),
            "low": list(map(float, lows if lows is not None else closes)),
            "close": list(map(float, closes)),
            "volume": 1.0,
        },
        index=list(index.date),
    )


def _double_bottom_frame():
    close = [109.0] * 60
    low = [108.0] * 60
    high = [110.0] * 60
    for index in range(22, 29):
        low[index] = 106
    low[25] = 100
    high[25] = 104
    close[25] = 102
    high[32] = 112
    low[32] = 110
    close[32] = 111
    low[40] = 101
    high[40] = 105
    close[40] = 103
    return _bars(close, low, high)


def test_double_bottom_uses_only_confirmed_lows_and_ignores_the_future():
    bars = _double_bottom_frame()
    early = detect_pattern(bars.iloc[:43], DAILY_PARAMS)
    assert early.status != "unknown"
    assert early.double_bottom == "no"
    found = detect_pattern(bars.iloc[:50], DAILY_PARAMS)
    assert found.status == "double_bottom"
    assert found.breakout_state == "no"
    assert found.low1_price.startswith("100")
    assert found.low2_price.startswith("101")
    assert abs(float(found.low_price_diff)) <= 0.03
    nxt = (pd.Timestamp(bars.index[-1]) + pd.offsets.BDay(1)).date().isoformat()
    extended = pd.concat([bars, _bars([120, 125, 130, 128, 131], [118, 124, 127, 126, 129], [122, 126, 132, 129, 133], start=nxt)])
    extended.iloc[-2, extended.columns.get_loc("low")] = 1
    again = detect_pattern(extended.iloc[:50], DAILY_PARAMS)
    assert again.as_dict() == found.as_dict()
    posted = posterior_breakout(extended, found)
    assert posted["posterior_breakout_state"] == "yes"
    assert posted["posterior_breakout_date"] > found.last_bar_date
    stale_start = (pd.Timestamp(bars.index[-1]) + pd.offsets.BDay(1)).date().isoformat()
    stale = pd.concat([bars, _bars([109] * 45, [108] * 45, [110] * 45, start=stale_start)])
    assert detect_pattern(stale, DAILY_PARAMS).double_bottom == "no"


def test_completed_daily_drops_the_unfinished_session_and_later_bars():
    calendar = xnys(2026, 2026)
    index = [datetime.fromisoformat(day).date() for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")]
    frame = pd.DataFrame({"open": 10, "high": 11, "low": 9, "close": [10, 20, 30, 40], "volume": 1}, index=index)
    asof = datetime(2026, 9, 8, 20, 20, tzinfo=timezone.utc)
    kept = completed_daily(frame, asof, calendar)
    assert list(kept.index) == [index[0]]
    frame.loc[index[-1], "close"] = 999
    assert float(completed_daily(frame, asof, calendar).iloc[-1]["close"]) == 10
    too_early = datetime(2026, 9, 8, 20, 10, tzinfo=timezone.utc)
    assert completed_daily(frame, too_early, calendar).empty


def test_partial_week_does_not_become_fridays_bar():
    calendar = xnys(2026, 2026)
    days = [datetime.fromisoformat(day).date() for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")]
    frame = pd.DataFrame({"open": [1, 1, 1, 1], "high": [2, 2, 2, 8], "low": [1, 1, 1, 1], "close": [1, 1, 1, 7], "volume": 1}, index=days)
    asof = datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)
    partial = weekly_bars(completed_daily(frame, asof, calendar), calendar)
    assert list(partial.index) == [days[1]]
    assert float(partial.iloc[0]["close"]) == 1
    assert bool(partial.iloc[0]["week_complete"]) is False
    finished = weekly_bars(completed_daily(frame, datetime(2026, 9, 11, 21, tzinfo=timezone.utc), calendar), calendar)
    assert bool(finished.iloc[0]["week_complete"]) is True
    assert float(finished.iloc[0]["high"]) == 8
