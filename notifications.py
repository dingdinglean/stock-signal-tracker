"""Detect and render one-time, material tracker changes."""
from __future__ import annotations

from typing import Any


MILESTONE_FIELDS = ("return_1d", "return_3d", "return_5d", "return_10d", "return_20d")
EFFECTIVE_VALUES = {"有效", "无效", "中性", "无法判定"}
PENDING_VALUES = {"", "pending", "待观察", "none", "null"}


def _pending(value: Any) -> bool:
    return str(value or "").strip().lower() in PENDING_VALUES


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


def _pct(value: Any) -> str:
    if value in (None, ""):
        return "pending"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:+.1f}%"


def _label(field: str) -> str:
    return {"return_1d": "T+1", "return_3d": "T+3", "return_5d": "T+5", "return_10d": "T+10", "return_20d": "T+20"}[field]


def build_subject(changes: list[dict[str, Any]]) -> str:
    new_count = sum(bool(item["new_signal"]) for item in changes)
    update_count = len(changes) - new_count
    if new_count and update_count:
        return f"【美股信号验证器】新增 {new_count} 条｜更新 {update_count} 条"
    if new_count:
        symbols = " ".join(str(item["record"].get("symbol", "")) for item in changes[:3]).strip()
        return f"【美股信号验证器】新增 {symbols}｜正式推送 {new_count}"
    return f"【美股信号验证器】{update_count} 条信号有新结果"


def build_body(changes: list[dict[str, Any]], history: list[dict[str, str]]) -> str:
    new_count = sum(bool(item["new_signal"]) for item in changes)
    update_count = len(changes) - new_count
    strong = sum(row.get("strong_sector") == "是" for row in history)
    non_strong = sum(row.get("strong_sector") == "否" for row in history)
    lines = [
        "【美股信号验证器】", "", f"本次新增正式推送：{new_count}", f"本次绩效更新：{update_count}",
        f"当前正式历史：{len(history)}", f"强势板块内：{strong}", f"非强势板块：{non_strong}", "",
    ]
    for change in changes:
        row = change["record"]
        timeframe = {"weekly": "周线", "monthly": "月线"}.get(str(row.get("source_timeframe", "")), row.get("source_timeframe") or "日/4H")
        lines.extend([
            str(row.get("symbol", "")), f"周期：{timeframe}", f"信号K：{row.get('signal_date', '')}", f"推送日：{row.get('push_date') or row.get('signal_date', '')}", f"推送价：{row.get('push_price') or row.get('signal_price', '')}",
            f"板块：{row.get('sector_theme') or '未知'}", f"板块排名：{row.get('sector_rank') or '-'}", f"强势板块：{row.get('strong_sector') or '未知'}", "",
            "本次新增：",
        ])
        if change["new_signal"]:
            lines.append("正式推送已入库")
        for field in change["milestones"]:
            if not _pending(row.get(field)):
                lines.append(f"{_label(field)}：{_pct(row.get(field))}")
        if change["effectiveness"]:
            lines.append(f"有效性：{row.get('effectiveness')}")
        lines.extend(["", "---", ""])
    return "\n".join(lines).rstrip() + "\n"
