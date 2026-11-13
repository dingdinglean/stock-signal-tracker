from __future__ import annotations

import json
from pathlib import Path

from dynamic_tracking import strategy_statistics
from notifications import prepare_email_data, timeframe_label

from research.cohort import assign_cohort
from research.run import PROTECTED, _read_csv, _sha

ROOT = Path(__file__).resolve().parents[1]


def test_baseline_hashes_and_every_formal_push_keeps_its_cohort():
    manifest = json.loads((ROOT / "research" / "baseline_manifest.json").read_text(encoding="utf-8"))
    assert manifest["git_sha"] == "6e279606a7efed49a9c1e6a866a4e6c3500372bf"
    assert manifest["signal_rows"] == 71
    for rel in PROTECTED:
        assert _sha(ROOT / rel) == manifest["files"][rel]["sha256"], rel
    rows = _read_csv(ROOT / "data" / "signal_history.csv")
    assert len(rows) == 71
    assert len({row["signal_id"] for row in rows}) == 71
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
    assert sum(assign_cohort(row) == "h4_excluded" for row in rows) == 17
    assert sum(assign_cohort(row) == "monthly_excluded" for row in rows) == 4
    assert sum(assign_cohort(row) == "daily" for row in rows) == 9
    assert sum(assign_cohort(row) == "weekly" for row in rows) == 41


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
    assert stats["total"] == 71
    assert hidden_losses
    assert stats["loss"] >= len(hidden_losses)
    prepared = prepare_email_data([], rows)
    assert all(row.get("display_state") != "hidden" for row in prepared.display_history)
    assert len(prepared.all_history) == 71
