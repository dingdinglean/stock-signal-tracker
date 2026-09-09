"""交易日收益、MFE/MAE 与 10 日有效性计算。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


TARGET_PCT = 5.0
STOP_PCT = -5.0
NEW_YORK = ZoneInfo("America/New_York")


def fetch_history(symbol: str, signal_date: date, today: date | None = None) -> pd.DataFrame:
    """Fetch unadjusted daily OHLC after a formal signal was sent."""
    import yfinance as yf

    today = today or date.today()
    frame = yf.Ticker(symbol).history(
        start=(signal_date - timedelta(days=7)).isoformat(),
        end=(today + timedelta(days=2)).isoformat(),
        interval="1d",
        auto_adjust=False,
        prepost=False,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.rename(columns=str.lower)
    keep = [column for column in ("open", "high", "low", "close", "volume") if column in frame.columns]
    return frame[keep].dropna(subset=["close"])


def freeze_rth_push_price(symbol: str, delivered_at: datetime) -> float | None:
    """Freeze the latest RTH daily close knowable when a legacy alert was sent.

    A same-day manual email before 16:20 ET cannot use that still-forming
    session's close. This deliberately selects the preceding completed RTH
    day in that case, avoiding a retrospective/future-data baseline.
    """
    timestamp = delivered_at.replace(tzinfo=NEW_YORK) if delivered_at.tzinfo is None else delivered_at.astimezone(NEW_YORK)
    cutoff = timestamp.date()
    if (timestamp.hour, timestamp.minute) < (16, 20):
        cutoff -= timedelta(days=1)
    frame = fetch_history(symbol, cutoff - timedelta(days=14), today=cutoff)
    if frame.empty:
        return None
    eligible = [position for position, stamp in enumerate(frame.index) if pd.Timestamp(stamp).date() <= cutoff]
    if not eligible:
        return None
    try:
        close = float(frame.iloc[eligible[-1]]["close"])
    except (TypeError, ValueError):
        return None
    return close if close > 0 else None


def _signal_close(frame: pd.DataFrame, signal_date: date) -> float | None:
    if frame.empty:
        return None
    eligible = [i for i, stamp in enumerate(frame.index) if pd.Timestamp(stamp).date() <= signal_date]
    if not eligible:
        return None
    value = float(frame.iloc[eligible[-1]]["close"])
    return value if value > 0 else None


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _pct(base: float, value: float) -> float:
    return (value / base - 1.0) * 100.0


def calculate_performance(frame: pd.DataFrame, signal_date: date, signal_price: object = None) -> dict[str, Any]:
    """Calculate results with actual future trading sessions only.

    The DXDX artifact's recorded close is intentionally the baseline.  Yahoo
    supplies later sessions and their OHLC ranges; it never rewrites the
    original pushed price.
    """
    result: dict[str, Any] = {
        "return_1d": "", "return_3d": "", "return_5d": "", "return_10d": "", "return_20d": "",
        "mfe_10d": "", "mae_10d": "", "mfe_20d": "", "mae_20d": "",
        "effectiveness": "pending", "sessions_observed": 0,
    }
    base = _number(signal_price) or _signal_close(frame, signal_date)
    if base is None:
        return result

    future = frame[[pd.Timestamp(index).date() > signal_date for index in frame.index]].copy()
    result["sessions_observed"] = len(future)
    for sessions in (1, 3, 5, 10, 20):
        if len(future) >= sessions:
            result[f"return_{sessions}d"] = round(_pct(base, float(future.iloc[sessions - 1]["close"])), 3)

    for sessions in (10, 20):
        window = future.head(sessions)
        if len(window) >= sessions:
            highs = window["high"] if "high" in window else window["close"]
            lows = window["low"] if "low" in window else window["close"]
            result[f"mfe_{sessions}d"] = round(_pct(base, float(highs.max())), 3)
            result[f"mae_{sessions}d"] = round(_pct(base, float(lows.min())), 3)

    if len(future) < 10:
        return result
    for _, row in future.head(10).iterrows():
        # With daily OHLC, a same-bar target and stop hit has no knowable order.
        hit_target = _pct(base, float(row.get("high", row["close"]))) >= TARGET_PCT
        hit_stop = _pct(base, float(row.get("low", row["close"]))) <= STOP_PCT
        if hit_target and hit_stop:
            result["effectiveness"] = "无法判定"
            return result
        if hit_target:
            result["effectiveness"] = "有效"
            return result
        if hit_stop:
            result["effectiveness"] = "无效"
            return result
    result["effectiveness"] = "中性"
    return result
