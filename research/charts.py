"""Candlestick charts for real cached bars. A missing series produces an error, not a drawing."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd

from research.hypothesis import STUDY_PARAMS
from research.market import parse_day

FONT = Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")


def _style():
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    if FONT.exists():
        font_manager.fontManager.addfont(str(FONT))
        plt.rcParams["font.family"] = "WenQuanYi Micro Hei"
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def _ema(series: pd.Series, span: int) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    averaged = values.ewm(span=span, adjust=False).mean()
    averaged.iloc[: span - 1] = pd.NA
    return averaged


def chart_dates(row: dict) -> dict[str, object]:
    """DXDX date comes only from ``signal_date``. An empty field stays unknown.

    ``push_date`` is never copied into the DXDX mark. When both dates exist and
    fall on the same session, that is one line, not two events pretending to
    be different days.
    """
    raw_signal = str(row.get("signal_date") or "").strip()
    dxdx = parse_day(raw_signal) if raw_signal else None
    push = parse_day(row.get("push_date"))
    return {
        "dxdx": dxdx,
        "push": push,
        "signal_time": str(row.get("signal_time") or "").strip(),
        "same_day": bool(dxdx and push and dxdx == push),
    }


def ohlc_audit_rows(frame: pd.DataFrame, row: dict, pattern: dict) -> list[dict[str, str]]:
    """One OHLC row per plotted session so a chart can be checked against the cache."""
    if frame is None or frame.empty:
        return []
    dates = chart_dates(row)
    anchor = dates["dxdx"] or dates["push"]
    if anchor is None:
        return []
    start = chart_window_start(list(frame.index), anchor, pattern if pattern.get("status") == "double_bottom" else {})
    if start is None:
        return []
    drawn = pattern.get("status") == "double_bottom"
    rows = []
    for day, bar in frame.loc[frame.index >= start].iterrows():
        rows.append({
            "signal_id": str(row.get("signal_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "cohort": str(row.get("cohort") or ""),
            "bar_date": pd.Timestamp(day).date().isoformat(),
            "open": str(bar.get("open", "")),
            "high": str(bar.get("high", "")),
            "low": str(bar.get("low", "")),
            "close": str(bar.get("close", "")),
            "volume": str(bar.get("volume", "")),
            "dxdx_date": dates["dxdx"].isoformat() if dates["dxdx"] else "",
            "push_date": dates["push"].isoformat() if dates["push"] else "",
            "relation": "before_dxdx" if dates["dxdx"] and pd.Timestamp(day).date() < dates["dxdx"] else (
                "dxdx_day" if dates["dxdx"] and pd.Timestamp(day).date() == dates["dxdx"] else "after_dxdx_or_push"
            ),
            "pattern_status": str(pattern.get("status") or ""),
            "draws_double_bottom": "yes" if drawn else "no",
            "price_basis": "Yahoo auto_adjust=False OHLC price return after splits; not Adj Close; not dividend total return",
        })
    return rows


def chart_window_start(days: list, anchor, pattern: dict):
    """First plotted session: at least 60 bars before ``anchor``, and any earlier low."""
    before = [day for day in days if day < anchor]
    if len(before) < STUDY_PARAMS.chart_lookback:
        return None
    start = before[-STUDY_PARAMS.chart_lookback]
    for key in ("low1_date", "low2_date"):
        marked = parse_day(pattern.get(key))
        if marked is not None and marked in days and marked < start:
            start = marked
    return start


def chart_markers(dates: dict, pattern: dict) -> list[tuple[object, str]]:
    """Signal-time marks and a separately labeled posterior breakout."""
    marks: list[tuple[object, str]] = []
    signal = dates.get("dxdx")
    push = dates.get("push")
    if signal is None and push is not None:
        marks.append((push, "推送"))
    elif dates.get("same_day"):
        marks.append((signal, "DXDX与推送同日"))
    else:
        if signal is not None:
            marks.append((signal, "DXDX"))
        if push is not None:
            marks.append((push, "推送"))
    if pattern.get("status") == "double_bottom" or pattern.get("double_bottom") == "yes":
        for key, label in (("low1_date", "低点1"), ("low2_date", "低点2")):
            marked = parse_day(pattern.get(key))
            if marked is not None:
                marks.append((marked, label))
    marked = parse_day(pattern.get("breakout_date"))
    if marked is not None and pattern.get("breakout_state") == "yes":
        marks.append((marked, "信号时突破"))
    posterior = parse_day(pattern.get("posterior_breakout_date"))
    if posterior is not None:
        marks.append((posterior, "后验突破"))
    return marks


def render_chart(daily: pd.DataFrame, row: dict, pattern: dict) -> tuple[bytes | None, str]:
    """Draw one formal push. Pattern levels come from the as-of result already computed."""
    if daily is None or daily.empty:
        return None, "无日线缓存，不绘制"
    dates = chart_dates(row)
    signal = dates["dxdx"]
    push = dates["push"]
    anchor = signal or push
    if anchor is None:
        return None, "没有独立的 DXDX 日期，也没有推送日，不绘制"
    days = list(daily.index)
    before_anchor = [day for day in days if day < anchor]
    if len(before_anchor) < STUDY_PARAMS.chart_lookback:
        return None, f"信号前交易日 {len(before_anchor)} 根，不足 60，不绘制案例图"
    if len(before_anchor) < 89:
        return None, f"信号之前只有 {len(before_anchor)} 根日 K，EMA89 在信号处尚未完成 warmup，不绘制"
    window_start = chart_window_start(days, anchor, pattern)
    view = daily.loc[daily.index >= window_start].copy()
    if view.empty:
        return None, "窗口内没有K线"
    plt = _style()
    figure, (price_ax, volume_ax) = plt.subplots(
        2, 1, sharex=True, figsize=(11.5, 7.2), gridspec_kw={"height_ratios": [3.2, 1]}
    )
    positions = list(range(len(view)))
    width = 0.6
    for position, (_, bar) in enumerate(view.iterrows()):
        open_ = float(bar["open"])
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        color = "#c0392b" if close >= open_ else "#1f6f4a"
        price_ax.vlines(position, low, high, color=color, linewidth=0.8)
        price_ax.bar(position, abs(close - open_) or (high - low) * 0.02, bottom=min(open_, close), width=width, color=color, align="center")
        volume_ax.bar(position, float(bar.get("volume") or 0), width=width, color=color, align="center")
    warmed = _ema(daily["close"], 23).reindex(view.index)
    slow = _ema(daily["close"], 89).reindex(view.index)
    price_ax.plot(positions, warmed.to_numpy(dtype=float), color="#d68910", linewidth=1, label="EMA23")
    price_ax.plot(positions, slow.to_numpy(dtype=float), color="#1a5276", linewidth=1, label="EMA89")
    index_of = {day: position for position, day in enumerate(view.index)}

    def _mark(day, text, color):
        if day in index_of:
            price_ax.axvline(index_of[day], color=color, linestyle="--", linewidth=0.9)
            price_ax.text(index_of[day], price_ax.get_ylim()[1], text, color=color, fontsize=8, rotation=90, va="top")

    for marked, label in chart_markers(dates, pattern):
        color = "#0e6655" if label == "后验突破" else "#6c3483"
        _mark(marked, label, color)
    level = pattern.get("neckline") or pattern.get("platform_high")
    if level:
        try:
            price_ax.axhline(float(level), color="#7b241c", linewidth=0.8, label="颈线/平台高")
        except ValueError:
            pass
    cohort = {"daily": "日线记录", "weekly": "周线记录"}.get(row.get("cohort"), row.get("cohort", ""))
    title = (
        f"{row.get('symbol')} {cohort} 形态={pattern.get('status')} 突破={pattern.get('breakout_state')} "
        f"板块={row.get('sector_status')} 排名={row.get('asof_sector_rank') or '无'}"
    )
    dxdx_text = "未知" if signal is None else f"{signal.isoformat()} {dates['signal_time']}".strip()
    subtitle = (
        f"DXDX {dxdx_text}｜推送 {push.isoformat() if push else '未知'}｜冻结价 {row.get('push_price')} "
        f"后10日 {row.get('return_10d') or '未成熟'} 后20日 {row.get('return_20d') or '未成熟'}"
    )
    price_ax.set_title(title + "\n" + subtitle, fontsize=10)
    price_ax.legend(loc="upper left", fontsize=8)
    price_ax.set_ylabel("价格")
    volume_ax.set_ylabel("成交量")
    step = max(1, len(positions) // 8)
    volume_ax.set_xticks(positions[::step])
    volume_ax.set_xticklabels([view.index[i].isoformat() for i in positions[::step]], rotation=30, ha="right", fontsize=8)
    figure.tight_layout()
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=120)
    plt.close(figure)
    return buffer.getvalue(), ""
