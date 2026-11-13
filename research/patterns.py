"""As-of chart patterns. The function only sees bars the caller has already truncated."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from research.hypothesis import DAILY_PARAMS, WEEKLY_PARAMS, PatternParams
from research.market import session_is_complete, week_end_session


@dataclass
class PatternResult:
    status: str
    reason: str
    double_bottom: str
    platform: str
    sharp_stabilize: str
    low1_date: str = ""
    low1_bar_date: str = ""
    low1_price: str = ""
    low2_date: str = ""
    low2_bar_date: str = ""
    low2_price: str = ""
    low_gap_bars: str = ""
    low_price_diff: str = ""
    neckline: str = ""
    neckline_date: str = ""
    mid_rebound: str = ""
    platform_high: str = ""
    platform_low: str = ""
    breakout_state: str = "unknown"
    breakout_date: str = ""
    bars_used: int = 0
    last_bar_date: str = ""

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def _unknown(reason: str, bars: int = 0, last: str = "") -> PatternResult:
    return PatternResult(
        status="unknown",
        reason=reason,
        double_bottom="unknown",
        platform="unknown",
        sharp_stabilize="unknown",
        breakout_state="unknown",
        bars_used=bars,
        last_bar_date=last,
    )


def _date_text(value) -> str:
    return pd.Timestamp(value).date().isoformat()


def _point_date(bars: pd.DataFrame, index: int, column: str) -> str:
    """Actual session of a weekly extreme. The bar index is only the week label."""
    if column in bars.columns:
        value = bars.iloc[index].get(column)
        if value is not None and str(value).strip() and str(value).strip().lower() != "nan":
            return _date_text(value)
    return _date_text(bars.index[index])


def _complete_flags(bars: pd.DataFrame) -> pd.Series:
    if "week_complete" in bars.columns:
        return bars["week_complete"].astype(bool)
    return pd.Series(True, index=bars.index)


def confirmed_lows(bars: pd.DataFrame, left: int, right: int) -> list[int]:
    """A local low counts only when the required bars on its right are already in the frame.

    Incomplete weekly bars never confirm a pivot, including as the right-hand side.
    """
    flags = _complete_flags(bars)
    found: list[int] = []
    if len(bars) < left + right + 1:
        return found
    for index in range(left, len(bars) - right):
        if not bool(flags.iloc[index]):
            continue
        right_slice = bars.iloc[index + 1 : index + 1 + right]
        if not bool(flags.iloc[index + 1 : index + 1 + right].all()):
            continue
        price = float(bars.iloc[index]["low"])
        left_min = float(bars.iloc[index - left : index]["low"].min())
        right_min = float(right_slice["low"].min())
        if price <= left_min and price < right_min:
            found.append(index)
    return found


def _round(value: float) -> str:
    return f"{value:.6f}"


def _double_bottom(bars: pd.DataFrame, params: PatternParams) -> dict | None:
    lows = confirmed_lows(bars, params.pivot_left, params.pivot_right)
    best: dict | None = None
    for second_pos, second in enumerate(lows):
        for first in lows[:second_pos]:
            gap = second - first
            if gap < params.min_low_gap or gap > params.max_low_gap:
                continue
            if second < len(bars) - 1 - params.max_second_low_age:
                continue
            first_price = float(bars.iloc[first]["low"])
            second_price = float(bars.iloc[second]["low"])
            base = min(first_price, second_price)
            if base <= 0:
                continue
            diff = abs(first_price - second_price) / base
            if diff > params.max_low_price_diff:
                continue
            middle = bars.iloc[first + 1 : second]
            if middle.empty:
                continue
            neckline = float(middle["high"].max())
            neckline_at = int(middle["high"].values.argmax()) + first + 1
            higher = max(first_price, second_price)
            rebound = neckline / higher - 1.0
            if rebound < params.min_mid_rebound:
                continue
            candidate = {
                "first": first,
                "second": second,
                "diff": diff,
                "neckline": neckline,
                "rebound": rebound,
                "first_price": first_price,
                "second_price": second_price,
                "gap": gap,
                "neckline_at": neckline_at,
            }
            # The latest confirmed second low is the pattern visible at the as-of bar.
            if best is None or second > best["second"] or (second == best["second"] and diff < best["diff"]):
                best = candidate
    return best


def _close_break(bars: pd.DataFrame, start: int, level: float) -> tuple[bool, str]:
    """Breakout at the as-of bar means the last close is above the level."""
    if not len(bars) or not level:
        return False, ""
    last_close = float(bars.iloc[-1]["close"])
    if last_close <= level:
        return False, ""
    for index in range(start, len(bars)):
        if float(bars.iloc[index]["close"]) > level:
            return True, _point_date(bars, index, "close_date")
    return False, ""


def _platform(bars: pd.DataFrame, params: PatternParams) -> dict | None:
    window = params.platform_window
    # The last bar is the potential breakout bar and is not part of the base.
    if len(bars) < window * 2 + 1:
        return None
    base = bars.iloc[-(window + 1) : -1]
    prior = bars.iloc[-(window * 2 + 1) : -(window + 1)]
    pivot = max(1, params.platform_pivot)
    lows = confirmed_lows(base.reset_index(drop=True), pivot, pivot)
    # confirmed_lows on a reset frame still requires right bars inside the base.
    if len(lows) < 2:
        return None
    low_prices = [float(base.iloc[index]["low"]) for index in lows]
    floor = min(low_prices)
    if floor <= 0 or (max(low_prices) - floor) / floor > params.platform_low_tolerance:
        return None
    base_range = float(base["high"].max()) - float(base["low"].min())
    prior_range = float(prior["high"].max()) - float(prior["low"].min())
    if prior_range <= 0 or base_range > params.platform_contraction * prior_range:
        return None
    return {
        "high": float(base["high"].max()),
        "low": float(base["low"].min()),
        "start": len(bars) - window - 1,
    }


def _sharp(bars: pd.DataFrame, params: PatternParams) -> bool:
    need = params.drop_lookback + params.stabilize_bars
    if len(bars) < need + 1:
        return False
    prior = bars.iloc[-(need) : -params.stabilize_bars]
    recent = bars.iloc[-params.stabilize_bars :]
    start = float(prior.iloc[0]["close"])
    end = float(prior.iloc[-1]["close"])
    if start <= 0:
        return False
    dropped = end / start - 1.0
    extreme = float(prior["low"].min())
    if dropped > params.drop_threshold and not (extreme / start - 1.0 <= params.drop_threshold):
        return False
    if extreme <= 0:
        return False
    if float(recent["low"].min()) < extreme * (1.0 - params.stabilize_undercut):
        return False
    return float(recent.iloc[-1]["close"]) >= extreme * (1.0 + params.rebound_from_low)


def detect_pattern(bars: pd.DataFrame, params: PatternParams | None = None, *, timeframe: str = "daily") -> PatternResult:
    """Classify one truncated OHLC frame. Future bars must not be in ``bars``."""
    params = params or (WEEKLY_PARAMS if timeframe == "weekly" else DAILY_PARAMS)
    if bars is None or len(bars) == 0:
        return _unknown("no_bars")
    last = _date_text(bars.index[-1])
    if len(bars) < params.min_bars:
        return _unknown("insufficient_history", len(bars), last)
    required = {"open", "high", "low", "close"}
    if not required <= set(bars.columns):
        return _unknown("missing_ohlc", len(bars), last)

    double = _double_bottom(bars, params)
    platform = _platform(bars, params)
    sharp = _sharp(bars, params)
    result = PatternResult(
        status="other",
        reason="no_hypothesis_matched",
        double_bottom="yes" if double else "no",
        platform="yes" if platform else "no",
        sharp_stabilize="yes" if sharp else "no",
        breakout_state="not_applicable",
        bars_used=len(bars),
        last_bar_date=last,
    )
    if double:
        result.status = "double_bottom"
        result.reason = "two_confirmed_lows"
        result.low1_bar_date = _date_text(bars.index[double["first"]])
        result.low1_date = _point_date(bars, double["first"], "low_date")
        result.low1_price = _round(double["first_price"])
        result.low2_bar_date = _date_text(bars.index[double["second"]])
        result.low2_date = _point_date(bars, double["second"], "low_date")
        result.low2_price = _round(double["second_price"])
        result.low_gap_bars = str(double["gap"])
        result.low_price_diff = _round(double["diff"])
        result.neckline = _round(double["neckline"])
        result.neckline_date = _point_date(bars, double["neckline_at"], "high_date")
        result.mid_rebound = _round(double["rebound"])
        broken, when = _close_break(bars, double["second"] + 1, double["neckline"])
        result.breakout_state = "yes" if broken else "no"
        result.breakout_date = when
    elif platform:
        result.status = "platform"
        result.reason = "contracting_base"
        result.platform_high = _round(platform["high"])
        result.platform_low = _round(platform["low"])
        broken, when = _close_break(bars, platform["start"], platform["high"])
        result.breakout_state = "yes" if broken else "no"
        result.breakout_date = when
    elif sharp:
        result.status = "sharp_stabilize"
        result.reason = "drop_then_stabilize"
        result.breakout_state = "not_applicable"
    return result


def posterior_breakout(bars: pd.DataFrame, pattern: PatternResult) -> dict[str, str]:
    """First close above the as-of level on bars strictly after the as-of bar.

    This is a later event. It is not written back onto the signal-time pattern.
    """
    blank = {"posterior_breakout_state": "not_applicable", "posterior_breakout_date": ""}
    if pattern.status not in {"double_bottom", "platform"} or not pattern.last_bar_date:
        if pattern.status == "unknown":
            return {"posterior_breakout_state": "unknown", "posterior_breakout_date": ""}
        return blank
    level_text = pattern.neckline or pattern.platform_high
    if not level_text:
        return blank
    level = float(level_text)
    last = pd.Timestamp(pattern.last_bar_date).date()
    for stamp, row in bars.iterrows():
        if pd.Timestamp(stamp).date() <= last:
            continue
        if float(row["close"]) > level:
            return {"posterior_breakout_state": "yes", "posterior_breakout_date": pd.Timestamp(stamp).date().isoformat()}
    return {"posterior_breakout_state": "no", "posterior_breakout_date": ""}


def complete_weeks_only(weekly: pd.DataFrame) -> pd.DataFrame:
    """Drop an unfinished week. Official weekly patterns do not use that bar."""
    if weekly is None or weekly.empty or "week_complete" not in weekly.columns:
        return weekly
    return weekly.loc[weekly["week_complete"].astype(bool)].copy()


def completed_daily(frame: pd.DataFrame, asof, calendar, buffer_minutes: int = 20) -> pd.DataFrame:
    """Drop every session whose official close was not knowable at ``asof``."""
    if frame is None or frame.empty:
        return frame.iloc[0:0].copy() if frame is not None else pd.DataFrame()
    keep = []
    for stamp in frame.index:
        day = pd.Timestamp(stamp).date()
        if session_is_complete(calendar, day, asof, buffer_minutes):
            keep.append(stamp)
    return frame.loc[keep].copy()


def weekly_bars(daily: pd.DataFrame, calendar) -> pd.DataFrame:
    """Build weekly OHLC from the daily sessions actually passed in.

    The current week uses only those sessions. Its date is the last session
    included, not the scheduled Friday, and ``week_complete`` is false until
    that Friday (or the week's last session) is present.
    """
    columns = ["open", "high", "low", "close", "volume", "week_complete", "scheduled_week_end"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=columns)
    grouped: dict[date, list] = {}
    for stamp, row in daily.iterrows():
        day = pd.Timestamp(stamp).date()
        ending = week_end_session(calendar, day)
        if ending is None:
            continue
        grouped.setdefault(ending, []).append((day, row))
    records = []
    index = []
    for ending, items in sorted(grouped.items()):
        items.sort(key=lambda item: item[0])
        last_day = items[-1][0]
        records.append(
            {
                "open": float(items[0][1]["open"]),
                "high": max(float(item[1]["high"]) for item in items),
                "low": min(float(item[1]["low"]) for item in items),
                "close": float(items[-1][1]["close"]),
                "volume": sum(float(item[1].get("volume", 0) or 0) for item in items),
                "week_complete": last_day == ending,
                "scheduled_week_end": ending.isoformat(),
                "low_date": min(items, key=lambda item: float(item[1]["low"]))[0].isoformat(),
                "high_date": max(items, key=lambda item: float(item[1]["high"]))[0].isoformat(),
                "close_date": last_day.isoformat(),
            }
        )
        index.append(last_day)
    frame = pd.DataFrame.from_records(records, index=pd.Index(index, name="date"))
    return frame
