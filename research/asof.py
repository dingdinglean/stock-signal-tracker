"""What was knowable when a formal push went out.

GitHub records when a workflow run was created and when it started. Neither
field is the email send time. ``updated_at`` is usually completion, which is
after the email, so it is evidence only and never the price clock. A missing
clock stays unknown; the push date is not promoted into a send timestamp.
"""
from __future__ import annotations

from datetime import datetime

from research.market import parse_time


def information_cutoff(meta: dict | None) -> tuple[datetime | None, str, str]:
    """Return ``(cutoff, quality, evidence)``.

    ``quality`` is ``proxy_run_start`` or ``unknown``. The cutoff is the earlier
    of ``created_at`` and ``run_started_at``. The email is at or after the run
    start, so a bar is eligible only when its session close plus the settle
    buffer is already at or before this proxy. A later true send time is not
    invented, and artifact publication time is not a market clock.
    """
    if not meta:
        return None, "unknown", "没有源 workflow 运行元数据，不把 push_date 当成发送时刻。"
    created = parse_time(meta.get("created_at"))
    started = parse_time(meta.get("run_started_at"))
    stamps = [stamp for stamp in (created, started) if stamp is not None]
    if not stamps:
        return None, "unknown", "运行元数据没有 created_at 或 run_started_at。updated_at 是结束时间，不用作 as-of。"
    cutoff = min(stamps)
    return cutoff, "proxy_run_start", (
        "代理时间：GitHub Actions 的 created_at / run_started_at 只说明运行已创建或已启动，"
        "不是邮件实际发送时刻。邮件不早于这个时刻，所以只使用收盘再加 20 分钟仍早于该代理的 K 线。"
        "不补写一个更晚的发送时间。updated_at 只留作证据，不参与排序。"
        "板块快照的发布时间也不是价格时钟。"
    )


def decision_clocks(meta: dict | None) -> dict[str, object]:
    """Bar clock and entry clock are different bounds.

    Bars use the earlier run-start proxy. A close during the run is not treated
    as already known. Entry uses job ``completed_at`` when the jobs payload has
    it, otherwise ``updated_at`` only when ``status`` is ``completed``. That
    later instant is an upper bound, so a run which starts before the open and
    finishes after it does not fill the open in between. If completion cannot
    be checked, the entry time stays unknown.
    """
    if not meta:
        return {
            "bar_cutoff": None, "bar_quality": "unknown", "entry_cutoff": None, "entry_quality": "unknown",
            "evidence": "没有可核验的运行时间。不把 push_date 或 created_at 编成发布时间。",
        }
    bar, bar_quality, evidence = information_cutoff(meta)
    completed = parse_time(meta.get("job_completed_at"))
    entry = None
    entry_quality = "unknown"
    if completed is not None:
        entry = completed
        entry_quality = str(meta.get("job_completed_source") or "job_completed_at")
    elif str(meta.get("status") or "") == "completed":
        updated = parse_time(meta.get("updated_at"))
        if updated is not None:
            entry = updated
            entry_quality = "run_updated_at_completed"
    if entry is None:
        evidence += " 没有可核验的 completed_at，成交时间保持未知。"
    else:
        evidence += " 成交使用完成时间的保守上界，不使用 created_at 作为实际发布时刻。"
    return {
        "bar_cutoff": bar, "bar_quality": bar_quality, "entry_cutoff": entry,
        "entry_quality": entry_quality, "evidence": evidence,
    }
