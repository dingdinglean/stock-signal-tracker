"""Incremental daily-bar cache. An empty or failed download never replaces a saved file."""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "adj_close", "volume", "dividends", "stock_splits"]


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def cache_path(root: Path, symbol: str) -> Path:
    safe = symbol.upper().replace("^", "_idx_").replace("/", "_")
    return root / f"{safe}.csv"


def normalize_yahoo(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(columns=str.lower)
    out = pd.DataFrame(index=renamed.index)
    mapping = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "adj close": "adj_close",
        "volume": "volume",
        "dividends": "dividends",
        "stock splits": "stock_splits",
    }
    for source, target in mapping.items():
        out[target] = pd.to_numeric(renamed[source], errors="coerce") if source in renamed.columns else pd.NA
    dates = []
    for value in out.index:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("America/New_York")
        dates.append(stamp.date())
    out.index = pd.Index(dates, name="date")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["close"])
    return out[COLUMNS]


def read_cache(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return empty_frame()
    frame = pd.read_csv(path)
    if "date" not in frame.columns or frame.empty:
        return empty_frame()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame = frame.set_index("date")
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[COLUMNS].sort_index()


def write_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.sort_index().to_csv(temporary, index_label="date", float_format="%.10g")
    os.replace(temporary, path)


def merge_cache(path: Path, fresh: pd.DataFrame | None) -> tuple[pd.DataFrame, bool]:
    """Merge by session date. A failed or empty download leaves the file untouched."""
    current = read_cache(path)
    if fresh is None or fresh.empty:
        return current, False
    incoming = fresh.copy()
    incoming = incoming.dropna(subset=["close"])
    if incoming.empty:
        return current, False
    for column in COLUMNS:
        if column not in incoming.columns:
            incoming[column] = pd.NA
    incoming = incoming[COLUMNS]
    combined = incoming.combine_first(current) if not current.empty else incoming
    # combine_first prefers the caller. incoming should win on overlapping dates.
    combined = incoming.combine_first(current) if not current.empty else incoming
    combined = combined[COLUMNS].sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    write_cache(path, combined)
    return read_cache(path), True


def incremental_start(cached: pd.DataFrame, full_start: str, overlap_days: int = 10) -> str:
    """Request only the overlap after the last cached session, not the whole history."""
    if cached is None or cached.empty:
        return full_start
    last = cached.index[-1]
    start = last - timedelta(days=overlap_days)
    floor = date.fromisoformat(full_start)
    if start < floor:
        start = floor
    return start.isoformat()


def fetch_symbol(symbol: str, start: str, end: str) -> tuple[pd.DataFrame | None, str]:
    """Download one symbol. The error string is the raw failure, never a synthetic price."""
    import yfinance as yf

    last_error = f"{symbol}: 未请求"
    for _attempt in range(2):
        try:
            frame = yf.Ticker(symbol).history(
                start=start,
                end=end,
                interval="1d",
                auto_adjust=False,
                prepost=False,
                actions=True,
                repair=True,
                timeout=30,
            )
        except Exception as exc:
            last_error = f"{symbol}: {exc.__class__.__name__}: {exc}"
            continue
        if frame is None or frame.empty:
            last_error = f"{symbol}: yfinance 返回空表"
            continue
        try:
            normalized = normalize_yahoo(frame)
        except Exception as exc:
            last_error = f"{symbol}: 行情解析失败 {exc.__class__.__name__}: {exc}"
            continue
        if normalized.empty:
            last_error = f"{symbol}: 行情解析后无收盘价"
            continue
        return normalized, ""
    return None, last_error
