"""Detect, prepare, and render one-time tracker email notifications."""
from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dynamic_tracking import display_allowed, number, strategy_statistics, quote_current


MILESTONE_FIELDS = ("return_1d", "return_3d", "return_5d", "return_10d", "return_20d")
EFFECTIVE_VALUES = {"有效", "无效", "中性", "无法判定"}
PENDING_VALUES = {"", "—", "pending", "待观察", "none", "null"}
TIMEFRAME_ORDER = {"日线": 0, "周线": 1, "月线": 2}
TIMEFRAME_ALIASES = {
    "day": "日线",
    "daily": "日线",
    "1d": "日线",
    "d": "日线",
    "日": "日线",
    "日线": "日线",
    "week": "周线",
    "weekly": "周线",
    "1w": "周线",
    "w": "周线",
    "周": "周线",
    "周线": "周线",
    "month": "月线",
    "monthly": "月线",
    "1mo": "月线",
    "1mth": "月线",
    "月": "月线",
    "月线": "月线",
}
FOUR_HOUR_ALIASES = {
    "4h", "4hr", "4hrs", "4hour", "4hours", "240m", "240min", "240mins",
    "240minute", "240minutes", "4小时", "四小时",
}


@dataclass(frozen=True)
class EmailData:
    """Email-only views of the complete, independently persisted history."""

    new_changes: tuple[dict[str, Any], ...]
    update_changes: tuple[dict[str, Any], ...]
    history: tuple[dict[str, str], ...]
    display_history: tuple[dict[str, str], ...]
    eliminated_count: int
    strong_count: int
    non_strong_count: int
    performance_update_count: int
    all_history: tuple[dict[str, Any], ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.new_changes or self.update_changes)

def _pending(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in PENDING_VALUES


def _retention_reason(row: dict[str, Any]) -> str | None:
    if not display_allowed(row):
        return None
    return "追踪中" if quote_current(row) else "待更新"


def _true(value: Any) -> bool:
    """Parse booleans as stored by current and legacy signal artifacts."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "是"}


def _daily_signal_confirmed(row: dict[str, Any]) -> bool:
    """Use the upstream final daily signal, never a timeframe-name shortcut.

    In the source radar, daily_dxdx is assigned only when both the existing
    BLUE_ABOVE_YELLOW trend check and the existing daily DXDX bottom signal
    are true.  This verifier reuses that persisted result and does not
    recalculate or alter either indicator.
    """
    return _true(row.get("daily_dxdx"))


def changes_between(old_rows: list[dict[str, str]], new_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Return each changed signal at most once, with its newly material fields."""
    old = {row.get("signal_id", ""): row for row in old_rows if row.get("signal_id")}
    changes: list[dict[str, Any]] = []
    for row in new_rows:
        signal_id = row.get("signal_id", "")
        previous = old.get(signal_id)
        if not previous:
            changes.append({"record": row, "new_signal": True, "milestones": list(MILESTONE_FIELDS), "effectiveness": bool(row.get("effectiveness") in EFFECTIVE_VALUES)})
            continue
        milestones = [field for field in MILESTONE_FIELDS if _pending(previous.get(field)) and not _pending(row.get(field))]
        became_effective = _pending(previous.get("effectiveness")) and row.get("effectiveness") in EFFECTIVE_VALUES
        if milestones or became_effective:
            changes.append({"record": row, "new_signal": False, "milestones": milestones, "effectiveness": became_effective})
    return changes


def timeframe_label(row: dict[str, Any]) -> str | None:
    """Map an email-eligible row to its Chinese period label.

    Unknown values and pure four-hour aliases are deliberately excluded. A
    legacy empty source_timeframe may fall back to source_signal_level.
    """
    raw = str(row.get("source_timeframe") or "").strip().lower().replace(" ", "")
    if raw in FOUR_HOUR_ALIASES:
        return None
    if raw == "daily+4h":
        return "日线" if _daily_signal_confirmed(row) else None
    if raw in TIMEFRAME_ALIASES:
        label = TIMEFRAME_ALIASES[raw]
        return label if label != "日线" or _daily_signal_confirmed(row) else None
    if raw:
        return None

    legacy = str(row.get("source_signal_level") or "").strip().lower().replace(" ", "")
    if legacy in FOUR_HOUR_ALIASES or legacy == "a":
        return None
    if legacy in TIMEFRAME_ALIASES:
        label = TIMEFRAME_ALIASES[legacy]
        return label if label != "日线" or _daily_signal_confirmed(row) else None
    if legacy in {"b", "s"}:
        return "日线" if _daily_signal_confirmed(row) else None
    return None


def _signal_sort_value(change: dict[str, Any]) -> tuple[str, str]:
    row = change["record"]
    return str(row.get("signal_date") or ""), str(row.get("signal_time") or "")


def _update_sort_value(change: dict[str, Any]) -> tuple[str, str, str]:
    row = change["record"]
    return (
        str(row.get("last_updated") or ""),
        str(row.get("signal_date") or ""),
        str(row.get("signal_time") or ""),
    )


def _sort_changes(changes: list[dict[str, Any]], *, updates: bool) -> tuple[dict[str, Any], ...]:
    by_recency = sorted(changes, key=_update_sort_value if updates else _signal_sort_value, reverse=True)
    return tuple(sorted(by_recency, key=lambda item: TIMEFRAME_ORDER[timeframe_label(item["record"]) or "日线"]))


def prepare_email_data(changes: list[dict[str, Any]], history: list[dict[str, str]]) -> EmailData:
    """Filter 4H/unknown rows once, then recompute every email statistic."""
    filtered_history = tuple(row for row in history if timeframe_label(row) is not None)
    display_history = tuple(row for row in filtered_history if _retention_reason(row) is not None)
    filtered_changes = [item for item in changes if timeframe_label(item["record"]) is not None]
    new_changes = [item for item in filtered_changes if bool(item.get("new_signal"))]
    update_changes = [item for item in filtered_changes if not bool(item.get("new_signal"))]
    eliminated_count = len(filtered_history) - len(display_history)
    checked_at = max((row.get("quote_checked_at") or "" for row in history), default="")
    updated_count = sum(row.get("quote_checked_at") == checked_at and quote_current(row) for row in filtered_history) if checked_at else len(update_changes)
    return EmailData(
        new_changes=_sort_changes(new_changes, updates=False),
        update_changes=_sort_changes(update_changes, updates=True),
        history=filtered_history,
        display_history=display_history,
        eliminated_count=eliminated_count,
        strong_count=sum(row.get("strong_sector") == "是" for row in filtered_history),
        non_strong_count=sum(row.get("strong_sector") == "否" for row in filtered_history),
        performance_update_count=updated_count,
        all_history=tuple(history),
    )


def _pct(value: Any) -> str:
    if _pending(value):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return html.escape(str(value))
    if not math.isfinite(number):
        return "—"
    return f"{number:+.1f}%"


def _label(field: str) -> str:
    return {"return_1d": "T+1", "return_3d": "T+3", "return_5d": "T+5", "return_10d": "T+10", "return_20d": "T+20"}[field]


def _price(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"${number:.2f}"


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%m/%d")
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%m/%d")
        except ValueError:
            return html.escape(text)


def _date_cell(row: dict[str, Any]) -> str:
    signal_raw = str(row.get("signal_date") or "").strip()
    push_raw = str(row.get("push_date") or signal_raw).strip()
    signal = _date(signal_raw)
    push = _date(push_raw)
    if not push_raw or push_raw[:10] == signal_raw[:10]:
        return signal
    return f"{signal}→{push}"


def _update_date_cell(row: dict[str, Any]) -> str:
    """Compact signal/push dates specifically for the history update table."""
    signal_raw = str(row.get("signal_date") or "").strip()
    push_raw = str(row.get("push_date") or signal_raw).strip()
    try:
        signal = datetime.fromisoformat(signal_raw.replace("Z", "+00:00")).date()
        push = datetime.fromisoformat(push_raw.replace("Z", "+00:00")).date()
    except ValueError:
        return _date_cell(row)
    if signal == push:
        return signal.strftime("%m/%d")
    if signal.year == push.year and signal.month == push.month:
        return f'{signal.strftime("%m/%d")}→{push.strftime("%d")}'
    if signal.year == push.year:
        return f'{signal.strftime("%m/%d")}→{push.strftime("%m/%d")}'
    return f'{signal.strftime("%y/%m/%d")}→{push.strftime("%y/%m/%d")}'


def _latest_performance(row: dict[str, Any]) -> str:
    value = number(row.get("latest_return"))
    if value is None:
        return "—"
    color = "#16803c" if value >= 0 else "#c62828"
    suffix = " 待更新" if not quote_current(row) else ""
    return f'<span style="color:{color};font-weight:600;">{_pct(value)}</span>{suffix}'


def _strong(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in {"是", "否"} else "—"


CELL = "border:1px solid #d9dde3;padding:6px 4px;text-align:left;vertical-align:middle;font-size:13px;line-height:1.3;"
HEAD = CELL + "background:#f5f7f9;font-weight:600;white-space:nowrap;"
NOWRAP = CELL + "white-space:nowrap;"
NEW_CELL = "border:1px solid #d9dde3;padding:5px 4px;text-align:left;vertical-align:middle;font-size:12.5px;line-height:1.3;"
NEW_HEAD = NEW_CELL + "background:#f5f7f9;font-weight:600;white-space:nowrap;"
NEW_NOWRAP = NEW_CELL + "white-space:nowrap;"
SECTOR_CELL = NEW_CELL + "white-space:normal;word-break:normal;overflow-wrap:break-word;hyphens:none;"
UPDATE_CELL = "border:1px solid #d9dde3;padding:5px 5px;text-align:left;vertical-align:middle;font-size:14px;line-height:1.25;"
UPDATE_HEAD = UPDATE_CELL + "background:#f5f7f9;font-size:13px;font-weight:600;white-space:nowrap;"
UPDATE_NOWRAP = UPDATE_CELL + "white-space:nowrap;"
MILESTONE_CELL = UPDATE_CELL + "padding-left:3px;padding-right:3px;white-space:nowrap;"
UPDATE_TIMEFRAME = "font-size:12px;line-height:1.15;color:#5f6368;font-weight:400;white-space:nowrap;"
SUMMARY_HEAD = "border:1px solid #e3e6ea;padding:4px 3px;text-align:center;vertical-align:middle;font-size:12px;line-height:1.25;background:#fafbfc;font-weight:500;"
SUMMARY_VALUE = "border:1px solid #e3e6ea;padding:4px 3px;text-align:center;vertical-align:middle;font-size:13px;line-height:1.2;font-weight:700;white-space:nowrap;"
TABLE = "width:100%;max-width:100%;border-collapse:collapse;border-spacing:0;"
NEW_TABLE = TABLE + "table-layout:fixed;"
UPDATE_TABLE = TABLE + "table-layout:fixed;"
SUMMARY_TABLE = TABLE + "table-layout:fixed;"


def _td(value: Any, style: str = CELL) -> str:
    return f'<td style="{style}">{value}</td>'


def _th(value: str, style: str = HEAD) -> str:
    return f'<th scope="col" style="{style}">{html.escape(value)}</th>'


def _summary_table(data: EmailData) -> str:
    stats = strategy_statistics(list(data.display_history))
    headers = ("本次新增", "绩效更新", "当前展示", "展示盈利", "展示亏损", "展示均收", "满月负收益隐藏", "数据待更新")
    values = (len(data.new_changes), data.performance_update_count, len(data.display_history), stats["profit"],
              stats["loss"], _pct(stats["average_return"]), data.eliminated_count,
              sum(not quote_current(r) for r in data.display_history))
    groups = []
    for offset in (0, 4):
        groups.append("<tr>" + "".join(_th(x, SUMMARY_HEAD) for x in headers[offset:offset+4]) + "</tr>")
        groups.append("<tr>" + "".join(_td(x, SUMMARY_VALUE) for x in values[offset:offset+4]) + "</tr>")
    return f'<table style="{SUMMARY_TABLE}" aria-label="邮件展示统计"><tbody>' + "".join(groups) + "</tbody></table>"


def _retention_summary(data: EmailData) -> str:
    dates = sorted({str(r.get("price_as_of")) for r in data.display_history if r.get("price_as_of")})
    cutoff = "—" if not dates else dates[0] if len(dates) == 1 else f"{dates[0]}～{dates[-1]}"
    return f'<p style="margin:6px 0 0;font-size:11px;color:#5f6368;">行情截至 {cutoff} · 展示统计含待更新记录的上次有效收益</p>'


def _new_table(data: EmailData) -> str:
    headers = ("代码", "周期", "信号日", "推进价", "板块", "强势", "最新绩效")
    rows: list[str] = []
    for change in data.new_changes:
        row = change["record"]
        symbol = html.escape(str(row.get("symbol") or "—"))
        sector = html.escape(str(row.get("sector_theme") or "—"))
        cells = (
            _td(f"<strong>{symbol}</strong>", NEW_NOWRAP),
            _td(timeframe_label(row) or "—", NEW_NOWRAP),
            _td(f'<span title="{_date_cell(row)}">{_date(row.get("signal_date"))}</span>', NEW_NOWRAP),
            _td(_price(row.get("push_price") or row.get("signal_price")), NEW_NOWRAP + "font-size:10.5px;"),
            _td(sector, SECTOR_CELL),
            _td(_strong(row.get("strong_sector")), NEW_NOWRAP),
            _td(_latest_performance(row), NEW_NOWRAP),
        )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        rows.append(f'<tr><td colspan="{len(headers)}" style="{CELL}text-align:center;color:#666;">—</td></tr>')
    return (
        f'<table style="{NEW_TABLE}" aria-label="本次新增">'
        '<colgroup><col style="width:10%;"><col style="width:9%;"><col style="width:13%;">'
        '<col style="width:14%;"><col style="width:28%;"><col style="width:8%;"><col style="width:18%;"></colgroup>'
        "<thead><tr>"
        + "".join(_th(item, NEW_HEAD) for item in headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _update_table(data: EmailData) -> str:
    headers = ("代码", "周期", "信号日", "推进价", "最新价", "跟踪天数", "最新收益", "状态")
    rows: list[str] = []
    history_rows = sorted(data.display_history, key=lambda r: (number(r.get("latest_return")) is not None,
                           number(r.get("latest_return")) if number(r.get("latest_return")) is not None else float("-inf")), reverse=True)
    cell = "border-bottom:1px solid #e3e6ea;padding:7px 1px;text-align:center;vertical-align:middle;font-size:10.5px;line-height:1.35;overflow-wrap:break-word;"
    for row in history_rows:
        symbol = html.escape(str(row.get("symbol") or "—"))
        status = _retention_reason(row) or "—"
        detail = html.escape(str(row.get("quote_error") or ""))
        as_of = html.escape(str(row.get("price_as_of") or "—"))
        values = [f'<strong>{symbol}</strong>', timeframe_label(row) or "—", _date(row.get("signal_date")),
                  _price(row.get("push_price") or row.get("signal_price")), _price(row.get("latest_price")),
                  f'T+{row["tracking_sessions"]}' if str(row.get("tracking_sessions", "")) != "" else "—",
                  _latest_performance(row), f'<span title="{detail}">{status}</span><br><small>{as_of}</small>']
        rows.append("<tr>" + "".join(_td(x, cell) for x in values) + "</tr>")
    if not rows:
        rows.append(f'<tr><td colspan="8" style="{CELL}text-align:center;">—</td></tr>')
    columns = "".join(f'<col style="width:{width}%;">' for width in (10, 7, 10, 13, 13, 10, 13, 14))
    return (f'<table style="{UPDATE_TABLE}" aria-label="历史绩效更新"><colgroup>{columns}</colgroup><thead><tr>'
            + "".join(_th(x, cell + "font-weight:600;background:#f5f7f9;") for x in headers)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def build_subject(data: EmailData) -> str:
    return f"【美股信号】新增 {len(data.new_changes)}｜更新 {data.performance_update_count}"


def build_body(data: EmailData) -> str:
    """Build a complete, mobile-friendly HTML email document."""
    title = html.escape(build_subject(data))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
</head>
<body style="margin:0;padding:10px 8px;background:#ffffff;color:#202124;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:15px;line-height:1.4;">
  <div style="width:100%;max-width:100%;margin:0 auto;box-sizing:border-box;">
    <h1 style="margin:0 0 10px;font-size:19px;line-height:1.3;">{title}</h1>
    {_summary_table(data)}
    {_retention_summary(data)}
    <h2 style="margin:15px 0 6px;font-size:16px;line-height:1.3;">本次新增</h2>
    <div style="width:100%;max-width:100%;">{_new_table(data)}</div>
    <h2 style="margin:15px 0 6px;font-size:16px;line-height:1.3;">历史绩效更新</h2>
    <div style="width:100%;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;">{_update_table(data)}</div>
  </div>
</body>
</html>
"""

