"""Unlimited end-of-day tracking. Presentation eligibility never changes the ledger."""
from __future__ import annotations

import calendar
import json
import math
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

DYNAMIC_FIELDS = [
    "latest_price", "comparable_price", "latest_return", "price_as_of", "tracking_sessions",
    "quote_status", "quote_error", "quote_checked_at", "display_state", "visibility_events",
]


@lru_cache(maxsize=32)
def market_calendar(start_year: int, end_year: int):
    return xcals.get_calendar("XNYS", start=f"{start_year}-01-01", end=f"{end_year}-12-31")


def latest_completed_session(now: datetime | None = None) -> date:
    stamp = pd.Timestamp(now or datetime.now(timezone.utc))
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    cal = market_calendar(stamp.year - 1, stamp.year + 1)
    # Allow Yahoo's daily close to settle, including early closing sessions.
    ready = cal.schedule[cal.schedule["close"] + pd.Timedelta(minutes=20) <= stamp]
    return ready.index[-1].date()


def trading_days(start: date, end: date) -> int:
    if end <= start:
        return 0
    cal = market_calendar(start.year, end.year)
    return len(cal.sessions_in_range(start + timedelta(days=1), end))


def month_anniversary(start: date) -> date:
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def number(value: Any, *, positive: bool = False) -> float | None:
    try:
        result = float(str(value).strip().removesuffix("%"))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


def quote_current(row: dict[str, Any], cutoff: date | None = None) -> bool:
    return row.get("quote_status") == "ok" and row.get("price_as_of") == (cutoff or latest_completed_session()).isoformat()


def display_allowed(row: dict[str, Any], today: date | None = None, cutoff: date | None = None) -> bool:
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    try:
        mature = today >= month_anniversary(date.fromisoformat(row["signal_date"][:10]))
    except (ValueError, KeyError, TypeError):
        return row.get("display_state") != "hidden"
    if not mature:
        return True
    value = number(row.get("latest_return"))
    # Unknown/stale data must preserve the last valid visibility decision.
    if not quote_current(row, cutoff) or value is None:
        return row.get("display_state") != "hidden"
    return value >= 0


def mark_visibility(row: dict[str, Any], today: date, checked_at: str) -> None:
    state = "visible" if display_allowed(row, today, latest_completed_session(datetime.fromisoformat(checked_at))) else "hidden"
    previous = row.get("display_state")
    if previous != state:
        events = json.loads(row.get("visibility_events") or "[]")
        events.append({"date": today.isoformat(), "state": state, "price_as_of": row.get("price_as_of", ""),
                       "return": row.get("latest_return", ""), "checked_at": checked_at})
        row["visibility_events"] = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    row["display_state"] = state


def dynamic_quote(frame: pd.DataFrame, event: dict[str, Any], cutoff: date) -> dict[str, Any]:
    """Translate adjusted prices onto the immutable original pushed-price scale.

    Yahoo Close is already split-adjusted. Undo post-push splits on today's
    close, and use Adj Close / Close ratios for dividend total-return factors.
    The raw latest close and the comparable value are stored separately.
    """
    if frame.empty:
        raise ValueError("行情缺失／代码变更、停牌或退市待核实")
    dates = pd.Index([pd.Timestamp(x).date() for x in frame.index])
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError("行情日期重复或乱序")
    frame = frame.loc[dates <= cutoff].copy()
    dates = dates[dates <= cutoff]
    if frame.empty or dates[-1] != cutoff:
        raise ValueError("行情过期／停牌、退市或代码变更待核实")
    base = number(event.get("push_price") or event.get("signal_price"), positive=True)
    if base is None:
        raise ValueError("原始推送基准缺失")
    push_date = date.fromisoformat(event.get("push_date") or event["signal_date"])
    if push_date > cutoff:
        raise ValueError("推送日尚未收盘")
    anchors = frame.loc[dates <= push_date]
    if anchors.empty:
        raise ValueError("缺少基准日行情，无法核实复权")
    cal = market_calendar(push_date.year, cutoff.year)
    expected = {x.date() for x in cal.sessions_in_range(push_date, cutoff)}
    available = set(dates[dates >= push_date])
    if available != expected:
        raise ValueError("交易日行情不完整／停牌待核实")
    close = number(frame.iloc[-1].get("close"), positive=True)
    anchor = number(anchors.iloc[-1].get("close"), positive=True)
    adj = number(frame.iloc[-1].get("adj close"), positive=True)
    anchor_adj = number(anchors.iloc[-1].get("adj close"), positive=True)
    if None in (close, anchor, adj, anchor_adj):
        raise ValueError("价格或复权数据无效")
    if str(frame.attrs.get("currency", "USD")).upper() != "USD":
        raise ValueError("行情货币不是 USD")
    split_factor = 1.0
    if "stock splits" in frame:
        for value in frame.loc[dates > push_date, "stock splits"]:
            split = number(value)
            if split is None or split < 0:
                raise ValueError("拆股数据异常")
            if split:
                split_factor *= split
    comparable = close * split_factor * (adj / close) / (anchor_adj / anchor)
    # Compare the split-normalized anchor with the frozen source price. A large
    # disagreement often means a wrong ticker, currency/unit issue or merger.
    if not 0.5 < anchor * split_factor / base < 2.0:
        raise ValueError("基准价与行情不匹配，需核实代码／公司行动")
    series = pd.to_numeric(frame["adj close"], errors="coerce")
    ratios = series / series.shift(1)
    if ((ratios < 0.25) | (ratios > 4)).any():
        raise ValueError("异常行情跳变，需核实公司行动")
    if not math.isfinite(comparable) or comparable <= 0:
        raise ValueError("可比价格无效")
    value = (comparable / base - 1) * 100
    return {"latest_price": repr(close), "comparable_price": repr(comparable),
            "latest_return": repr(value), "price_as_of": cutoff.isoformat(),
            "tracking_sessions": trading_days(date.fromisoformat(event["signal_date"]), cutoff),
            "quote_status": "ok", "quote_error": ""}


def strategy_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [v for row in rows if (v := number(row.get("latest_return"))) is not None]
    profit, loss = sum(x > 0 for x in values), sum(x < 0 for x in values)
    return {"total": len(rows), "priced": len(values), "fresh": sum(quote_current(r) for r in rows),
            "profit": profit, "loss": loss, "flat": sum(x == 0 for x in values),
            "win_rate": profit / len(values) * 100 if values else None,
            "average_return": sum(values) / len(values) if values else None}
