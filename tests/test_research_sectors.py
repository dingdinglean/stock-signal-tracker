from __future__ import annotations

from datetime import date, datetime, timezone

from research.market import xnys
from research.sectors import candidate_labels, classify_sector


def _snap(day, published, *themes, run_id="1", created=None):
    row = {
        "market_date": day, "run_id": run_id, "run_created_at": created or published,
        "available_at": published, "availability_source": "actions_job_completed_at",
        "job_id": run_id, "job_name": "scan", "job_conclusion": "success",
    }
    for index, theme in enumerate(themes, start=1):
        row[f"rank{index}_theme"] = theme
    return row


def test_later_snapshot_does_not_fill_the_signal_day_and_missing_is_unknown():
    calendar = xnys(2026, 2026)
    snapshots = [
        _snap("2026-09-08", "2026-09-08T22:00:00Z", "能源", "云软件", "生物医药", run_id="early"),
        _snap("2026-09-09", "2026-09-09T23:00:00Z", "半导体", "云软件", "大型科技", run_id="next"),
        _snap("2026-09-04", "2026-09-10T23:00:00Z", "云软件", "能源", "半导体", run_id="backdated"),
    ]
    asof = datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc)
    known = classify_sector({"云软件"}, snapshots, asof.date(), asof, calendar)
    assert known["asof_sector_status"] == "strong"
    assert known["asof_sector_rank"] == "2"
    assert known["asof_snapshot_run_id"] == "early"
    assert known["posterior_5d_entered_top3"] in {"yes", "unknown", "no"}
    late_clock = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    withheld = classify_sector({"云软件"}, snapshots, late_clock.date(), late_clock, calendar)
    assert withheld["asof_sector_status"] == "unknown"
    assert withheld["asof_sector_reason"] == "no_snapshot_known_by_proxy_cutoff"
    absent = classify_sector({"工业"}, snapshots, asof.date(), asof, calendar)
    assert absent["asof_sector_status"] == "unknown"
    assert absent["asof_sector_reason"] == "no_historical_taxonomy_map_not_strong_withheld"
    unlabeled = classify_sector(set(), snapshots, asof.date(), asof, calendar)
    assert unlabeled["asof_sector_status"] == "unknown"
    assert unlabeled["posterior_5d_entered_top3"] == "unknown"
    # A top-3 print that exists only after the push stays in the posterior fields.
    turned = classify_sector({"半导体"}, snapshots, asof.date(), asof, calendar)
    assert turned["asof_sector_status"] == "unknown"
    assert turned["posterior_first_top3_date"] == "2026-09-09"
    assert turned["posterior_lead_since"] == "2026-09-09"
    assert turned["posterior_first_rank1_date"] == "2026-09-09"
    assert turned["asof_sector_rank"] == ""


def test_run_start_before_the_open_does_not_make_the_ranking_tradable():
    from research.market import next_entry_session
    calendar = xnys(2026, 2026)
    started = "2026-07-06T13:00:00Z"
    finished = "2026-07-06T14:00:00Z"
    snap = _snap("2026-07-06", finished, "云软件", "能源", "半导体", created=started)
    during = datetime(2026, 7, 6, 13, 20, tzinfo=timezone.utc)
    assert classify_sector({"云软件"}, [snap], date(2026, 7, 6), during, calendar)["asof_sector_status"] == "unknown"
    after = datetime(2026, 7, 6, 14, 5, tzinfo=timezone.utc)
    assert classify_sector({"云软件"}, [snap], date(2026, 7, 6), after, calendar)["asof_sector_status"] == "strong"
    bare = {"market_date": "2026-07-06", "run_id": "9", "run_created_at": started, "rank1_theme": "云软件", "rank2_theme": "能源", "rank3_theme": "半导体"}
    assert classify_sector({"云软件"}, [bare], date(2026, 7, 6), after, calendar)["asof_sector_status"] == "unknown"
    assert next_entry_session(calendar, datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)).isoformat() == "2026-07-06"
    assert next_entry_session(calendar, datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)).isoformat() == "2026-07-07"


def test_labels_use_the_frozen_strings_only():
    assert candidate_labels({"sector_theme": "其他（Building Products）", "industry": "Building Products"}) == {"Building Products"}
    assert "航空航天" in candidate_labels({"sector_theme": "航空航天", "industry": "Aerospace & Defense"})
    assert candidate_labels({"sector_theme": "", "industry": ""}) == set()
