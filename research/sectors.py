"""Join a frozen industry label to the RSI radar's published top-3 history.

The history is ``data/sector_snapshots.csv``. Each row already carries the
market date and the artifact ``run_created_at``. Those are the observation
times. A git commit date is never treated as a market date, and a later day's
ranking is never copied onto an earlier day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from research.hypothesis import SECTOR_PARAMS, SectorParams
from research.market import following_sessions, parse_time, prior_sessions, session_is_complete, sessions_in


def candidate_labels(row: dict) -> set[str]:
    """Exact labels already frozen on the formal push.

    The fallback ``其他（...）`` display string is not a radar publish label.
    The raw GICS ``industry`` and a curated theme such as ``航空航天`` are.
    There is no fuzzy translation: if neither string was stored, the rank is unknown.
    """
    labels: set[str] = set()
    theme = str(row.get("sector_theme") or "").strip()
    industry = str(row.get("industry") or "").strip()
    if theme and not theme.startswith("其他"):
        labels.add(theme)
    if industry and industry not in {"其他"}:
        labels.add(industry)
    return labels


def _themes(snapshot: dict) -> list[str]:
    return [str(snapshot.get(f"rank{rank}_theme") or "").strip() for rank in (1, 2, 3)]


def _cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _complete_top3(snapshot: dict | None) -> bool:
    """The radar publishes at most three names. A blank slot is not a full list."""
    if snapshot is None:
        return False
    themes = _themes(snapshot)
    return len(themes) == 3 and all(themes)


def _applicable(labels: set[str], themes: list[str]) -> set[str]:
    """Labels that use the same vocabulary the radar printed that day.

    A Chinese theme is comparable only when that snapshot itself contains
    Chinese labels. A GICS string is comparable only when that snapshot
    contains Latin labels. Exact equality is always comparable. There is no
    translation table and today's Wikipedia industry is not applied backward.
    """
    published = [theme for theme in themes if theme]
    scripts = set()
    if any(_cjk(theme) for theme in published):
        scripts.add("cjk")
    if any(not _cjk(theme) for theme in published):
        scripts.add("latin")
    applicable: set[str] = set()
    for label in labels:
        if label in published:
            applicable.add(label)
        elif _cjk(label) and "cjk" in scripts:
            applicable.add(label)
        elif not _cjk(label) and "latin" in scripts:
            applicable.add(label)
    return applicable


def _usable(snapshot: dict, asof: datetime | None, *, allow_later: bool) -> bool:
    published = parse_time(snapshot.get("run_created_at"))
    if published is None:
        return False
    if allow_later or asof is None:
        return allow_later
    return published <= asof


def _on_day(snapshots: list[dict], day: date, asof: datetime | None, *, allow_later: bool) -> dict | None:
    key = day.isoformat()
    choices = [
        row
        for row in snapshots
        if str(row.get("market_date") or "") == key and _usable(row, asof, allow_later=allow_later)
    ]
    if not choices:
        return None
    return max(choices, key=lambda row: str(row.get("run_created_at") or ""))


def _rank_of(labels: set[str], snapshot: dict | None) -> int | None:
    if snapshot is None or not labels or not _complete_top3(snapshot):
        return None
    themes = _themes(snapshot)
    applicable = _applicable(labels, themes)
    hits = [index + 1 for index, theme in enumerate(themes) if theme and theme in applicable]
    return min(hits) if hits else None


def _scored(labels: set[str], snapshot: dict | None) -> bool | None:
    """True only on an exact hit in a complete top 3.

    Absence is not evidence of "not strong": this repository does not have a
    versioned map proving the frozen industry was the radar's label that day.
    """
    if snapshot is None or not labels or not _complete_top3(snapshot):
        return None
    if _rank_of(labels, snapshot) is None:
        return None
    return True


def _window_flag(
    hits: int,
    observed: int,
    expected: int,
    *,
    any_hit: bool,
    need: int | None = None,
    window_closed: bool = True,
) -> str:
    """Tri-state. An unfinished posterior window cannot become the negative class."""
    if expected <= 0:
        return "unknown"
    if any_hit:
        if hits > 0:
            return "yes"
        if window_closed and observed == expected:
            return "no"
        return "unknown"
    # Sustained-strength hypothesis: `need` hits are required.
    assert need is not None
    if hits >= need:
        return "yes"
    if hits + (expected - observed) < need:
        return "no"
    return "unknown"


def _lead_since(snapshots: list[dict], labels: set[str], days: list[date], asof: datetime | None, *, allow_later: bool) -> str:
    """Start of the current contiguous rank-1 streak. Blank when the latest day is not rank 1."""
    if not days or not labels:
        return ""
    latest = _on_day(snapshots, days[-1], asof, allow_later=allow_later)
    if _rank_of(labels, latest) != 1:
        return ""
    start = days[-1]
    for day in reversed(days[:-1]):
        snapshot = _on_day(snapshots, day, asof, allow_later=allow_later)
        if _rank_of(labels, snapshot) != 1:
            break
        start = day
    return start.isoformat()


def _last_rank1(snapshots: list[dict], labels: set[str], days: list[date], asof: datetime | None, *, allow_later: bool) -> str:
    for day in reversed(days):
        if _rank_of(labels, _on_day(snapshots, day, asof, allow_later=allow_later)) == 1:
            return day.isoformat()
    return ""


def classify_sector(
    labels: set[str],
    snapshots: list[dict],
    market_date: date | None,
    asof: datetime | None,
    calendar,
    params: SectorParams | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return as-of fields and, separately, posterior fields."""
    params = params or SECTOR_PARAMS
    result = {
        "sector_label_source": "frozen_label_matches_that_day_vocabulary",
        "mapping_historically_valid": "no",
        "sector_labels": "|".join(sorted(labels)),
        "asof_sector_status": "unknown",
        "asof_sector_reason": "",
        "asof_sector_rank": "",
        "asof_snapshot_run_id": "",
        "asof_snapshot_published_at": "",
        "asof_prior5_top3": "unknown",
        "asof_prior5_hits": "",
        "asof_prior5_observed": "",
        "asof_prior10_sustained": "unknown",
        "asof_prior10_hits": "",
        "asof_prior10_observed": "",
        "asof_prior5_rank": "",
        "asof_rank_change_5d": "",
        "asof_lead_since": "",
        "asof_last_rank1_date": "",
        "posterior_5d_entered_top3": "unknown",
        "posterior_5d_hits": "",
        "posterior_5d_observed": "",
        "posterior_10d_entered_top3": "unknown",
        "posterior_10d_hits": "",
        "posterior_10d_observed": "",
        "posterior_5d_rank": "",
        "posterior_rank_change_5d": "",
        "posterior_lead_since": "",
        "posterior_first_top3_date": "",
        "posterior_first_top3_run_id": "",
        "posterior_first_top3_published_at": "",
        "posterior_first_rank1_date": "",
        "posterior_sessions_to_top3": "",
        "posterior_role": "description_only_from_the_top3_event_not_from_the_push",
    }
    if market_date is None:
        result["asof_sector_reason"] = "missing_push_date"
        return result
    if asof is None:
        result["asof_sector_reason"] = "missing_push_timestamp"
        _fill_posterior(result, labels, snapshots, market_date, calendar, params, now)
        return result
    if not labels:
        result["asof_sector_reason"] = "no_frozen_label"
        _fill_posterior(result, labels, snapshots, market_date, calendar, params, now)
        return result

    same_day = _on_day(snapshots, market_date, asof, allow_later=False)
    if same_day is None:
        result["asof_sector_reason"] = "no_snapshot_known_by_proxy_cutoff"
    else:
        result["asof_snapshot_run_id"] = str(same_day.get("run_id") or "")
        # Evidence of when that radar run started. Not a price timestamp.
        result["asof_snapshot_published_at"] = str(same_day.get("run_created_at") or "")
        result["mapping_historically_valid"] = "no"
        rank = _rank_of(labels, same_day) if _complete_top3(same_day) else None
        if rank is not None:
            result["asof_sector_status"] = "strong"
            result["asof_sector_rank"] = str(rank)
            result["asof_sector_reason"] = "exact_label_in_complete_top3"
            result["mapping_historically_valid"] = "exact_string_only"
        elif not _complete_top3(same_day):
            result["asof_sector_reason"] = "top3_incomplete_and_no_taxonomy_map"
        else:
            result["asof_sector_reason"] = "no_historical_taxonomy_map_not_strong_withheld"

    def _scan(days: list[date], *, allow_later: bool) -> tuple[int, int]:
        hits = 0
        observed = 0
        for day in days:
            scored = _scored(labels, _on_day(snapshots, day, asof, allow_later=allow_later))
            if scored is None:
                continue
            observed += 1
            if scored:
                hits += 1
        return hits, observed

    prior5 = prior_sessions(calendar, market_date, params.prior_short)
    prior10 = prior_sessions(calendar, market_date, params.prior_long)
    hits5, seen5 = _scan(prior5, allow_later=False)
    hits10, seen10 = _scan(prior10, allow_later=False)
    result["asof_prior5_hits"] = str(hits5)
    result["asof_prior5_observed"] = f"{seen5}/{len(prior5)}"
    result["asof_prior5_top3"] = _window_flag(hits5, seen5, len(prior5), any_hit=True)
    result["asof_prior10_hits"] = str(hits10)
    result["asof_prior10_observed"] = f"{seen10}/{len(prior10)}"
    result["asof_prior10_sustained"] = _window_flag(
        hits10, seen10, len(prior10), any_hit=False, need=params.sustain_hits
    )
    if len(prior5) == params.prior_short:
        # Rank on the session five trading days before the push, not an average.
        older_rank = _rank_of(labels, _on_day(snapshots, prior5[0], asof, allow_later=False))
        if older_rank is not None:
            result["asof_prior5_rank"] = str(older_rank)
        if older_rank is not None and result["asof_sector_rank"]:
            result["asof_rank_change_5d"] = str(older_rank - int(result["asof_sector_rank"]))
    history_days = prior_sessions(calendar, market_date, 40)
    # Include the signal day itself when its snapshot was already published.
    asof_days = [*history_days, market_date]
    result["asof_lead_since"] = _lead_since(snapshots, labels, asof_days, asof, allow_later=False)
    result["asof_last_rank1_date"] = _last_rank1(snapshots, labels, asof_days, asof, allow_later=False)
    _fill_posterior(result, labels, snapshots, market_date, calendar, params, now)
    return result


def _fill_posterior(
    result: dict[str, str],
    labels: set[str],
    snapshots: list[dict],
    market_date: date,
    calendar,
    params: SectorParams,
    now: datetime | None,
) -> None:
    """Later rankings. Callers must not copy these into the as-of status."""
    if not labels:
        return

    def _scan(days: list[date]) -> tuple[int, int]:
        hits = 0
        observed = 0
        for day in days:
            scored = _scored(labels, _on_day(snapshots, day, None, allow_later=True))
            if scored is None:
                continue
            observed += 1
            if scored:
                hits += 1
        return hits, observed

    def _closed(days: list[date]) -> bool:
        if now is None or len(days) < 1:
            return False
        return session_is_complete(calendar, days[-1], now, 20)

    post5 = following_sessions(calendar, market_date, params.posterior_short)
    post10 = following_sessions(calendar, market_date, params.posterior_long)
    hits5, seen5 = _scan(post5)
    hits10, seen10 = _scan(post10)
    result["posterior_5d_hits"] = str(hits5)
    result["posterior_5d_observed"] = f"{seen5}/{len(post5)}"
    result["posterior_5d_entered_top3"] = _window_flag(
        hits5, seen5, len(post5), any_hit=True, window_closed=_closed(post5) and len(post5) == params.posterior_short
    )
    result["posterior_10d_hits"] = str(hits10)
    result["posterior_10d_observed"] = f"{seen10}/{len(post10)}"
    result["posterior_10d_entered_top3"] = _window_flag(
        hits10, seen10, len(post10), any_hit=True, window_closed=_closed(post10) and len(post10) == params.posterior_long
    )
    if len(post5) == params.posterior_short:
        later = _rank_of(labels, _on_day(snapshots, post5[-1], None, allow_later=True))
        if later is not None:
            result["posterior_5d_rank"] = str(later)
        if later is not None and result.get("asof_sector_rank"):
            result["posterior_rank_change_5d"] = str(int(result["asof_sector_rank"]) - later)
    later_days = following_sessions(calendar, market_date, 40)
    for day in later_days:
        if _rank_of(labels, _on_day(snapshots, day, None, allow_later=True)) == 1:
            result["posterior_first_rank1_date"] = day.isoformat()
            break
    for day in later_days:
        snapshot = _on_day(snapshots, day, None, allow_later=True)
        if _rank_of(labels, snapshot) is not None:
            result["posterior_first_top3_date"] = day.isoformat()
            result["posterior_lead_since"] = day.isoformat()
            result["posterior_first_top3_run_id"] = str((snapshot or {}).get("run_id") or "")
            result["posterior_first_top3_published_at"] = str((snapshot or {}).get("run_created_at") or "")
            result["posterior_sessions_to_top3"] = str(len(sessions_in(calendar, market_date + timedelta(days=1), day)))
            break
