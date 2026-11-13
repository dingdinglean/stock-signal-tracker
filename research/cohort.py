"""Study membership. Four-hour and monthly history stay in the file and out of the two cohorts."""
from __future__ import annotations

from notifications import timeframe_label


def assign_cohort(row: dict) -> str:
    """Use the email period label without applying the loss-hiding display filter.

    ``timeframe_label`` already drops pure 4h aliases. Daily+4h stays in the
    daily cohort only when the stored daily DXDX flag is true. Monthly rows
    are retained as ``monthly_excluded``. Anything else, including 4h, is
    ``h4_excluded`` or ``excluded_other`` and is still listed in the research file.
    """
    label = timeframe_label(row)
    if label == "日线":
        return "daily"
    if label == "周线":
        return "weekly"
    if label == "月线":
        return "monthly_excluded"
    raw = str(row.get("source_timeframe") or "").strip().lower()
    level = str(row.get("source_signal_level") or "").strip().lower()
    if raw in {"4h", "4hr", "4hrs", "4hour", "4hours"} or level in {"a", "4h"}:
        return "h4_excluded"
    return "excluded_other"
