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


def render_chart(daily: pd.DataFrame, row: dict, pattern: dict) -> tuple[bytes | None, str]:
    """Draw one formal push. Pattern levels come from the as-of result already computed."""
    if daily is None or daily.empty:
        return None, "无日线缓存，不绘制"
    signal = parse_day(row.get("signal_date"))
    push = parse_day(row.get("push_date"))
    anchor = signal or push
    if anchor is None:
        return None, "没有信号日或推送日"
    days = [day for day in daily.index if True]
    if anchor not in days:
        earlier = [day for day in days if day <= anchor]
        if not earlier:
            return None, f"{anchor.isoformat()} 之前没有日线，不绘制"
    start_pool = [day for day in days if day <= anchor]
    if len(start_pool) < STUDY_PARAMS.chart_lookback:
        window_start = start_pool[0] if start_pool else days[0]
        short = True
    else:
        window_start = start_pool[-STUDY_PARAMS.chart_lookback]
        short = False
    view = daily.loc[(daily.index >= window_start)].copy()
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

    _mark(signal, "DXDX", "#6c3483")
    _mark(push, "推送", "#1a5276")
    for key, label in (("low1_date", "低点1"), ("low2_date", "低点2"), ("breakout_date", "突破")):
        marked = parse_day(pattern.get(key))
        if marked is None:
            continue
        _mark(marked, label, "#b03a2e")
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
    subtitle = (
        f"推送 {row.get('push_date')} 冻结价 {row.get('push_price')} "
        f"后10日 {row.get('return_10d') or '未成熟'} 后20日 {row.get('return_20d') or '未成熟'}"
    )
    if short:
        subtitle += " 前窗不足60个交易日"
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
