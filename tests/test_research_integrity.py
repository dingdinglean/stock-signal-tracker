from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dynamic_tracking import strategy_statistics
from notifications import prepare_email_data, timeframe_label

from research.cohort import assign_cohort
from research.run import PROTECTED, _read_csv, _sha

ROOT = Path(__file__).resolve().parents[1]


def test_baseline_hashes_and_every_formal_push_keeps_its_cohort():
    manifest = json.loads((ROOT / "research" / "baseline_manifest.json").read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "research" / "baseline_frozen_ids.json").read_text(encoding="utf-8"))
    assert manifest["git_sha"] == "6e279606a7efed49a9c1e6a866a4e6c3500372bf"
    assert manifest["signal_rows"] == 71
    mutable = {"data/signal_history.csv", "data/sector_snapshots.csv", "data/state.json", "data/sector_metadata_cache.json", "data/events.csv"}
    for rel in PROTECTED:
        if rel in mutable:
            continue
        assert _sha(ROOT / rel) == manifest["files"][rel]["sha256"], rel
    rows = _read_csv(ROOT / "data" / "signal_history.csv")
    assert len(rows) >= 71
    assert len({row["signal_id"] for row in rows}) == len(rows)
    by_id = {row["signal_id"]: row for row in rows}
    assert set(frozen["rows"]) <= set(by_id)
    for signal_id, digest in frozen["rows"].items():
        payload = "|".join(by_id[signal_id].get(field, "") for field in frozen["fields"])
        assert hashlib.sha256(payload.encode()).hexdigest() == digest, signal_id
    hidden_in_study = 0
    for row in rows:
        cohort = assign_cohort(row)
        label = timeframe_label(row)
        if label == "日线":
            assert cohort == "daily"
        elif label == "周线":
            assert cohort == "weekly"
        elif label == "月线":
            assert cohort == "monthly_excluded"
        else:
            assert cohort in {"h4_excluded", "excluded_other"}
        if cohort in {"daily", "weekly"} and row.get("display_state") == "hidden":
            hidden_in_study += 1
        assert row["push_price"]
    assert hidden_in_study > 0
    baseline = [by_id[signal_id] for signal_id in frozen["rows"]]
    expected = {"h4_excluded": 17, "monthly_excluded": 4, "daily": 9, "weekly": 41}
    for cohort, count in expected.items():
        assert sum(assign_cohort(row) == cohort for row in baseline) == count
        assert sum(assign_cohort(row) == cohort for row in rows) >= count
    milestones = json.loads((ROOT / "research" / "baseline_milestones.json").read_text(encoding="utf-8"))
    assert_frozen_milestones(rows, milestones)
    assert set(milestones["rows"]) == set(frozen["rows"])
    assert int(milestones["filled_cells"]) > 0
    assert int(milestones["empty_cells"]) > 0


def assert_frozen_milestones(rows: list[dict], milestones: dict) -> None:
    """Non-empty baseline T+N / MFE / MAE stay put. Empty cells may mature. New ids are kept."""
    by_id = {row["signal_id"]: row for row in rows}
    assert set(milestones["rows"]) <= set(by_id)
    for signal_id, pinned in milestones["rows"].items():
        current = by_id[signal_id]
        for field, expected in pinned.items():
            assert current.get(field, "") == expected, (signal_id, field, expected, current.get(field, ""))


def test_empty_milestones_may_mature_and_new_ids_stay():
    milestones = {
        "fields": ["return_5d", "return_10d", "mfe_10d", "mae_10d"],
        "rows": {"old": {"return_5d": "1.25"}},
        "filled_cells": 1,
        "empty_cells": 3,
    }
    current = [
        {"signal_id": "old", "return_5d": "1.25", "return_10d": "6.496", "mfe_10d": "9.136", "mae_10d": "-1.534", "latest_return": "8.0", "display_state": "hidden"},
        {"signal_id": "new-scheduled", "return_5d": "3.0", "latest_return": "3.0", "latest_price": "120"},
    ]
    assert_frozen_milestones(current, milestones)
    changed = [dict(current[0], return_5d="9.9"), current[1]]
    try:
        assert_frozen_milestones(changed, milestones)
    except AssertionError as exc:
        assert "return_5d" in str(exc)
    else:
        raise AssertionError("a filled milestone was rewritten")


def test_research_runner_and_workflows_do_not_send_mail_or_retune_dxdx():
    source = (ROOT / "research" / "run.py").read_text(encoding="utf-8")
    assert "send_email" not in source
    assert "smtplib" not in source
    workflow = (ROOT / ".github" / "workflows" / "research.yml").read_text(encoding="utf-8")
    assert "SMTP" not in workflow
    assert "research_dry_run" in workflow
    assert "cron:" in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "research-price-v1-" in workflow
    tracker = (ROOT / ".github" / "workflows" / "tracker.yml").read_text(encoding="utf-8")
    assert "python tracker.py" in tracker
    assert "research.run" not in tracker


def test_real_ledger_statistics_keep_hidden_losses_and_do_not_mail():
    rows = _read_csv(ROOT / "data" / "signal_history.csv")
    stats = strategy_statistics(rows)
    hidden = [row for row in rows if row.get("display_state") == "hidden"]
    hidden_losses = [row for row in hidden if row.get("latest_return") and float(row["latest_return"]) < 0]
    assert stats["total"] == len(rows) >= 71
    assert hidden_losses
    assert stats["loss"] >= len(hidden_losses)
    prepared = prepare_email_data([], rows)
    assert all(row.get("display_state") != "hidden" for row in prepared.display_history)
    assert len(prepared.all_history) == len(rows)
