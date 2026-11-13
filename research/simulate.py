"""Trade simulation that does not write the verifier's frozen push price.

Entry is the next NYSE open at or after the push timestamp. A later session
that opens through a resting barrier fills at that open. The entry print itself
is the fill, so it is not also treated as a gap through a barrier measured
from that same print.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from research.market import next_entry_session, session_offset, sessions_in


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and pd.notna(number) else None


def _row_on(bars: pd.DataFrame, day: date):
    if day not in bars.index:
        return None
    return bars.loc[day]


def apply_costs(entry: float, exit_price: float, fee_bps: float, slippage_bps: float) -> float:
    """Buy higher and sell lower. Fees are charged on both notionals."""
    entry_cost = entry * (1.0 + slippage_bps / 10_000.0) * (1.0 + fee_bps / 10_000.0)
    proceeds = exit_price * (1.0 - slippage_bps / 10_000.0) * (1.0 - fee_bps / 10_000.0)
    return proceeds / entry_cost - 1.0


def _pack(**fields) -> dict:
    return fields


def simulate_trade(
    bars: pd.DataFrame,
    asof: datetime,
    calendar,
    *,
    mode: str,
    take_profit: float = 0.05,
    stop_loss: float = 0.05,
    hold_sessions: int | None = None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> dict:
    """Simulate one long. Ambiguous same-bar barrier touches keep both ends of the range.

    Primary ``gross_return`` is the conservative end (stop first). ``optimistic_gross_return``
    is the target-first end. They are equal when the path determines the order,
    including an overnight gap through one barrier.
    """
    entry_day = next_entry_session(calendar, asof)
    base = {
        "status": "missing_entry",
        "entry_date": "",
        "exit_date": "",
        "entry_price": "",
        "exit_price": "",
        "optimistic_exit_price": "",
        "gross_return": "",
        "net_return": "",
        "optimistic_gross_return": "",
        "optimistic_net_return": "",
        "ambiguous": "",
        "exit_reason": "",
        "overnight_gap_pct": "",
        "mfe": "",
        "mae": "",
        "highwater_drawdown": "",
        "entry_rule": "next NYSE open at or after the supplied information time; barriers are measured from that open",
        "censored": "",
    }
    if entry_day is None or bars is None or bars.empty:
        base["exit_reason"] = "no_tradable_open"
        return base
    entry = _row_on(bars, entry_day)
    if entry is None:
        base["entry_date"] = entry_day.isoformat()
        base["exit_reason"] = "entry_session_missing"
        return base
    entry_price = _num(entry["open"])
    if entry_price is None:
        base["entry_date"] = entry_day.isoformat()
        base["status"] = "missing_entry"
        base["exit_reason"] = "entry_open_missing"
        return base
    prior = bars.loc[[day for day in bars.index if day < entry_day]]
    if not prior.empty and _num(prior.iloc[-1]["close"]):
        base["overnight_gap_pct"] = f"{entry_price / float(prior.iloc[-1]['close']) - 1.0:.6f}"
    future = bars.loc[[day for day in bars.index if day >= entry_day]]
    target = entry_price * (1.0 + take_profit)
    stop = entry_price * (1.0 - stop_loss)
    base["entry_date"] = entry_day.isoformat()
    base["entry_price"] = f"{entry_price:.6f}"

    if mode == "hold":
        if not hold_sessions or hold_sessions < 1:
            raise ValueError("hold mode needs hold_sessions")
        # Day 1 is the entry session. session_offset counts sessions strictly after it.
        exit_day = entry_day if hold_sessions == 1 else session_offset(calendar, entry_day, hold_sessions - 1)
        if exit_day is None or exit_day not in bars.index:
            base["status"] = "censored"
            base["censored"] = "yes"
            base["exit_reason"] = "hold_window_incomplete"
            _excursions(base, future, entry_price)
            return base
        exit_price = _num(bars.loc[exit_day]["close"])
        if exit_price is None:
            base["status"] = "censored"
            base["censored"] = "yes"
            base["exit_reason"] = "exit_close_missing"
            return base
        window = bars.loc[[day for day in bars.index if entry_day <= day <= exit_day]]
        _finish(base, exit_day, exit_price, exit_price, False, "hold_close", entry_price, fee_bps, slippage_bps, window)
        return base

    if mode != "barrier":
        raise ValueError(f"unknown mode {mode}")
    for offset, (day, row) in enumerate(future.iterrows()):
        high = _num(row["high"])
        low = _num(row["low"])
        open_price = _num(row["open"])
        if high is None or low is None or open_price is None:
            base["status"] = "censored"
            base["censored"] = "yes"
            base["exit_reason"] = "bar_incomplete"
            return base
        gap_exit = None
        gap_reason = ""
        if offset > 0 and open_price >= target:
            gap_exit, gap_reason = open_price, "gap_target"
        elif offset > 0 and open_price <= stop:
            gap_exit, gap_reason = open_price, "gap_stop"
        if gap_exit is not None:
            window = future.loc[[item for item in future.index if item <= day]]
            _finish(base, day, gap_exit, gap_exit, False, gap_reason, entry_price, fee_bps, slippage_bps, window)
            return base
        hit_target = high >= target
        hit_stop = low <= stop
        if hit_target and hit_stop:
            window = future.loc[[item for item in future.index if item <= day]]
            _finish(base, day, stop, target, True, "same_bar_both", entry_price, fee_bps, slippage_bps, window)
            return base
        if hit_target:
            window = future.loc[[item for item in future.index if item <= day]]
            _finish(base, day, target, target, False, "target", entry_price, fee_bps, slippage_bps, window)
            return base
        if hit_stop:
            window = future.loc[[item for item in future.index if item <= day]]
            _finish(base, day, stop, stop, False, "stop", entry_price, fee_bps, slippage_bps, window)
            return base
    base["status"] = "censored"
    base["censored"] = "yes"
    base["exit_reason"] = "neither_barrier_hit"
    _excursions(base, future, entry_price)
    return base


def _excursions(result: dict, window: pd.DataFrame, entry_price: float) -> None:
    if window is None or window.empty:
        return
    highs = pd.to_numeric(window["high"], errors="coerce")
    lows = pd.to_numeric(window["low"], errors="coerce")
    if highs.notna().any():
        result["mfe"] = f"{float(highs.max()) / entry_price - 1.0:.6f}"
    if lows.notna().any():
        result["mae"] = f"{float(lows.min()) / entry_price - 1.0:.6f}"
    peak = entry_price
    worst = 0.0
    for _, row in window.iterrows():
        high = _num(row["high"])
        low = _num(row["low"])
        if high:
            peak = max(peak, high)
        if low and peak > 0:
            worst = min(worst, low / peak - 1.0)
    result["highwater_drawdown"] = f"{worst:.6f}"


def _finish(result, day, conservative, optimistic, ambiguous, reason, entry, fee_bps, slippage_bps, window) -> None:
    result["status"] = "closed"
    result["censored"] = "no"
    result["exit_date"] = day.isoformat() if hasattr(day, "isoformat") else str(day)
    result["exit_price"] = f"{conservative:.6f}"
    result["optimistic_exit_price"] = f"{optimistic:.6f}"
    result["ambiguous"] = "yes" if ambiguous else "no"
    result["exit_reason"] = reason
    result["gross_return"] = f"{conservative / entry - 1.0:.6f}"
    result["optimistic_gross_return"] = f"{optimistic / entry - 1.0:.6f}"
    result["net_return"] = f"{apply_costs(entry, conservative, fee_bps, slippage_bps):.6f}"
    result["optimistic_net_return"] = f"{apply_costs(entry, optimistic, fee_bps, slippage_bps):.6f}"
    _excursions(result, window, entry)


def benchmark_return(index_bars: pd.DataFrame | None, entry_day: date | None, exit_day: date | None) -> tuple[str, str]:
    """S&P 500 index price return from entry open to exit close.

    ``^GSPC`` is the index, not the SPY ETF, and this is not a dividend total return.
    A barrier that fills during the session is aligned to that session's index close.
    """
    if index_bars is None or index_bars.empty or entry_day is None or exit_day is None:
        return "", "benchmark_missing"
    if entry_day not in index_bars.index or exit_day not in index_bars.index:
        return "", "benchmark_session_missing"
    start = _num(index_bars.loc[entry_day]["open"])
    end = _num(index_bars.loc[exit_day]["close"])
    if start is None or end is None:
        return "", "benchmark_price_missing"
    return f"{end / start - 1.0:.6f}", "^GSPC open-to-close price return; not SPY; not total return; intraday barrier exits use the session close"


def ledger_benchmark(index_bars: pd.DataFrame | None, push_day: date, sessions: int, calendar) -> dict[str, str]:
    """^GSPC close-to-close percent over the ledger's T+N window.

    The frozen stock return is already a percent: push price to the close of the
    Nth NYSE session strictly after ``push_date``. The index uses that same end
    session and the ^GSPC close on ``push_date``. The result is percent, so it
    can be subtracted from ``return_Nd``. It is not a decimal, not SPY, and not
    a dividend total return. The stock anchor remains the frozen print.
    """
    blank = {"percent": "", "start": "", "end": "", "unit": "percent"}
    if index_bars is None or index_bars.empty:
        return blank
    end = session_offset(calendar, push_day, sessions)
    if end is None or push_day not in index_bars.index or end not in index_bars.index:
        return blank
    start = _num(index_bars.loc[push_day]["close"])
    finish = _num(index_bars.loc[end]["close"])
    if start is None or finish is None:
        return blank
    return {
        "percent": f"{(finish / start - 1.0) * 100.0:.6f}",
        "start": push_day.isoformat(),
        "end": end.isoformat(),
        "unit": "percent",
    }


def ledger_index_return(index_bars: pd.DataFrame | None, push_day: date, sessions: int, calendar) -> str:
    """Percent index move. See ``ledger_benchmark`` for the window."""
    return ledger_benchmark(index_bars, push_day, sessions, calendar)["percent"]


def _dated_window(bars: pd.DataFrame | None, start_day: date, sessions: int, calendar):
    """Sessions strictly after ``start_day`` through the Nth one. Complete means every one is cached."""
    if bars is None or bars.empty or sessions < 1 or start_day not in bars.index:
        return None, [], False
    end = session_offset(calendar, start_day, sessions)
    if end is None:
        return None, [], False
    expected = sessions_in(calendar, start_day + timedelta(days=1), end)
    present = [day for day in expected if day in bars.index]
    return end, present, len(expected) == sessions and len(present) == sessions


def _drawdown_percent(bars: pd.DataFrame, start_day: date, days: list[date]) -> str:
    """Peak starts at the start close. Intraday order is unknown, so high is applied before low."""
    start_price = _num(bars.loc[start_day]["close"])
    if start_price is None or not days:
        return ""
    peak = start_price
    worst = 0.0
    for day in days:
        high = _num(bars.loc[day].get("high"))
        low = _num(bars.loc[day].get("low"))
        if high:
            peak = max(peak, high)
        if low and peak > 0:
            worst = min(worst, low / peak - 1.0)
    return f"{worst * 100.0:.6f}"


def path_highwater_percent(bars: pd.DataFrame | None, start_day: date, sessions: int, calendar) -> str:
    """Mature N-session high-water drawdown, in percent. Blank when the window is short.

    This is not MAE. A partial window is not returned here.
    """
    _end, present, complete = _dated_window(bars, start_day, sessions, calendar)
    if not complete:
        return ""
    return _drawdown_percent(bars, start_day, present)


def path_highwater_todate(bars: pd.DataFrame | None, start_day: date, sessions: int, calendar) -> str:
    """Drawdown using only sessions already cached, including a short window."""
    _end, present, _complete = _dated_window(bars, start_day, sessions, calendar)
    if not present:
        return ""
    return _drawdown_percent(bars, start_day, present)


def forward_close_return_percent(bars: pd.DataFrame | None, start_day: date, sessions: int, calendar) -> str:
    """Percent close-to-close move starting at ``start_day``. Blank unless all N sessions exist."""
    end, _present, complete = _dated_window(bars, start_day, sessions, calendar)
    if not complete or end is None:
        return ""
    start = _num(bars.loc[start_day]["close"])
    finish = _num(bars.loc[end]["close"])
    if start is None or finish is None:
        return ""
    return f"{(finish / start - 1.0) * 100.0:.6f}"


def open_to_later_close_percent(bars: pd.DataFrame | None, entry_day: date, sessions: int, calendar) -> str:
    """Percent from the entry open to the close of the Nth holding session. Blank if short."""
    if bars is None or bars.empty or entry_day not in bars.index or sessions < 1:
        return ""
    end = entry_day if sessions == 1 else session_offset(calendar, entry_day, sessions - 1)
    if end is None or end not in bars.index:
        return ""
    expected = sessions_in(calendar, entry_day, end)
    if len(expected) != sessions or any(day not in bars.index for day in expected):
        return ""
    start = _num(bars.loc[entry_day]["open"])
    finish = _num(bars.loc[end]["close"])
    if start is None or finish is None:
        return ""
    return f"{(finish / start - 1.0) * 100.0:.6f}"


def scale_warning(bars: pd.DataFrame, push_day: date, push_price: float) -> tuple[str, str]:
    """Compare the frozen print with Yahoo's split-adjusted close, undoing later splits.

    The simulation still fills on the Yahoo open/high/low/close scale. It does not
    replace the frozen push price. A large residual is reported and that trade is
    left out of the simulation average.
    """
    if bars is None or bars.empty or not push_price:
        return "yes", "no_anchor"
    eligible = [day for day in bars.index if day <= push_day]
    if not eligible:
        return "yes", "no_bar_on_or_before_push"
    anchor_day = eligible[-1]
    anchor = _num(bars.loc[anchor_day]["close"])
    if anchor is None:
        return "yes", "anchor_close_missing"
    factor = 1.0
    if "stock_splits" in bars.columns:
        for day in bars.index:
            if day <= push_day:
                continue
            split = bars.loc[day, "stock_splits"]
            try:
                value = float(split)
            except (TypeError, ValueError):
                continue
            if pd.notna(value) and value > 0:
                factor *= value
    comparable = anchor * factor
    ratio = comparable / push_price
    if not 0.5 < ratio < 2.0:
        return "yes", f"anchor_ratio={ratio:.4f}"
    return "no", f"anchor_ratio={ratio:.4f}"
