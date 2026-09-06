from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import tracker
from email_sender import EmailDeliveryError, send_email
from notifications import build_body, changes_between
from performance import calculate_performance


def _frame(closes, highs=None, lows=None):
    dates = pd.bdate_range("2026-09-01", periods=len(closes))
    return pd.DataFrame({
        "close": closes,
        "high": highs or closes,
        "low": lows or closes,
    }, index=dates)


class FakeArtifacts:
    def __init__(self, report: str, signals: str, run_id=10):
        self.run_id = run_id
        self.archive = {"output/dxdx_report.txt": report.encode(), "output/dxdx_signals.csv": signals.encode()}

    def workflow_runs(self, *_args, **_kwargs):
        return [{"id": self.run_id, "created_at": "2026-09-08T22:30:00Z"}]

    def artifact_archive(self, *_args):
        return self.archive


SIGNALS = "symbol,signal_level,daily_dxdx,h4_dxdx,daily_signal_time,h4_signal_time,close\nAMD,S,是,是,2026-09-08T00:00:00-04:00,2026-09-08T16:00:00-04:00,100\n"


def _state():
    return {"processed_gupiao_run_ids": [], "processed_strength_run_ids": [], "tracking_start_utc": "2026-09-06T00:00:00Z"}


def test_email_not_sent_does_not_enter_formal_history():
    history = tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：否", SIGNALS), _state(), [])
    assert history == []


def test_email_sent_enters_formal_history():
    history = tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：是", SIGNALS), _state(), [])
    assert len(history) == 1
    assert history[0]["signal_price"] == "100"
    assert history[0]["daily_dxdx"] == "是"


def test_same_symbol_different_run_creates_two_pushes():
    first = tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：是", SIGNALS, 10), _state(), [])
    second = tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：是", SIGNALS, 11), _state(), first)
    assert len(second) == 2


def test_same_signal_id_is_not_duplicated():
    state = _state()
    initial = tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：是", SIGNALS, 10), state, [])
    assert tracker.ingest_formal_pushes(FakeArtifacts("邮件是否发送：是", SIGNALS, 10), state, initial) == initial


def _event(symbol="AMD"):
    item = {field: "" for field in tracker.HISTORY_FIELDS}
    item.update({"symbol": symbol, "signal_date": "2026-09-08", "strong_sector": "", "effectiveness": "pending"})
    return item


def _snapshots():
    return [{"market_date": "2026-09-08", "run_id": "7", "rank1_theme": "半导体", "rank2_theme": "云软件", "rank3_theme": "能源"}]


def test_top1_is_strong_sector():
    event = _event("AMD")
    tracker.freeze_sector_context([event], _snapshots(), {"AMD": {"industry": "Semiconductors", "sector_theme": "半导体"}})
    assert (event["strong_sector"], event["sector_rank"]) == ("是", "1")


def test_top2_and_top3_are_strong_sector():
    for symbol, theme, rank in (("CRM", "云软件", "2"), ("XOM", "能源", "3")):
        event = _event(symbol)
        tracker.freeze_sector_context([event], _snapshots(), {symbol: {"industry": theme, "sector_theme": theme}})
        assert (event["strong_sector"], event["sector_rank"]) == ("是", rank)


def test_top4_or_missing_is_not_strong_sector():
    event = _event("JPM")
    tracker.freeze_sector_context([event], _snapshots(), {"JPM": {"industry": "Banks", "sector_theme": "银行"}})
    assert event["strong_sector"] == "否"


def test_returns_use_future_trading_sessions_and_signal_price():
    frame = _frame([99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110])
    result = calculate_performance(frame, date(2026, 9, 2), 100)
    assert result["return_1d"] == 1.0
    assert result["return_3d"] == 3.0


def test_mfe_mae_use_daily_high_low():
    frame = _frame([100] + [100] * 10, [100, 103, 108] + [101] * 8, [100, 98, 94] + [99] * 8)
    result = calculate_performance(frame, date(2026, 9, 1), 100)
    assert result["mfe_10d"] == 8.0
    assert result["mae_10d"] == -6.0


def test_target_first_is_effective():
    frame = _frame([100] + [100] * 10, [100, 106] + [101] * 9, [100, 99] + [99] * 9)
    assert calculate_performance(frame, date(2026, 9, 1), 100)["effectiveness"] == "有效"


def test_stop_first_is_invalid():
    frame = _frame([100] + [100] * 10, [100, 101] + [106] * 9, [100, 94] + [99] * 9)
    assert calculate_performance(frame, date(2026, 9, 1), 100)["effectiveness"] == "无效"


def test_neither_threshold_is_neutral():
    frame = _frame([100] + [100] * 10, [100] + [104] * 10, [100] + [96] * 10)
    assert calculate_performance(frame, date(2026, 9, 1), 100)["effectiveness"] == "中性"


def test_same_bar_double_threshold_is_ambiguous():
    frame = _frame([100] + [100] * 10, [100, 106] + [101] * 9, [100, 94] + [99] * 9)
    assert calculate_performance(frame, date(2026, 9, 1), 100)["effectiveness"] == "无法判定"


def test_insufficient_sessions_stays_pending_and_blank():
    frame = _frame([100, 101, 102, 103])
    result = calculate_performance(frame, date(2026, 9, 1), 100)
    assert result["effectiveness"] == "pending"
    assert result["return_5d"] == ""


def test_zero_history_writes_normal_report(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(tracker, "REPORT_CSV", tmp_path / "tracker_report.csv")
    monkeypatch.setattr(tracker, "REPORT_TXT", tmp_path / "tracker_report.txt")
    tracker.write_reports([])
    assert "历史正式推送总数：0" in (tmp_path / "tracker_report.txt").read_text(encoding="utf-8")
    assert (tmp_path / "tracker_report.csv").exists()


def _notification_row(signal_id="one", **updates):
    row = {field: "" for field in tracker.HISTORY_FIELDS}
    row.update({"signal_id": signal_id, "symbol": "AMD", "signal_date": "2026-09-08", "signal_price": "100", "sector_theme": "半导体", "strong_sector": "是", "effectiveness": "pending"})
    row.update(updates)
    return row


def test_no_history_change_does_not_notify():
    row = _notification_row(return_5d="3.2")
    assert changes_between([row], [dict(row)]) == []


def test_new_signal_notifies_once():
    changes = changes_between([], [_notification_row()])
    assert len(changes) == 1 and changes[0]["new_signal"]


@pytest.mark.parametrize("field", ["return_1d", "return_3d", "return_5d", "return_10d", "return_20d"])
def test_first_return_milestone_notifies(field):
    old, new = _notification_row(), _notification_row(**{field: "1.5"})
    changes = changes_between([old], [new])
    assert changes[0]["milestones"] == [field]


def test_first_effectiveness_result_notifies():
    changes = changes_between([_notification_row()], [_notification_row(effectiveness="有效")])
    assert changes[0]["effectiveness"] is True


def test_existing_milestone_does_not_repeat_notification():
    old = _notification_row(return_5d="3.2")
    assert changes_between([old], [_notification_row(return_5d="3.2")]) == []


def test_multiple_changes_for_one_signal_render_once():
    old = _notification_row()
    new = _notification_row(return_1d="1.2", return_5d="3.8", effectiveness="有效")
    changes = changes_between([old], [new])
    body = build_body(changes, [new])
    assert len(changes) == 1
    assert body.count("AMD\n推送日") == 1
    assert "T+1：+1.2%" in body and "T+5：+3.8%" in body and "有效性：有效" in body


def test_multiple_signal_changes_are_combined_into_one_body():
    first = _notification_row("one")
    updated_first = _notification_row("one", return_1d="1.2")
    second = _notification_row("two", symbol="NVDA", return_1d="2")
    changes = changes_between([first], [updated_first, second])
    body = build_body(changes, [updated_first, second])
    assert len(changes) == 2
    assert "AMD" in body and "NVDA" in body and "本次新增正式推送：1" in body


def test_test_email_only_does_not_run_tracker(monkeypatch):
    sent = []
    monkeypatch.setattr(tracker, "send_connectivity_test", lambda: sent.append(True))
    monkeypatch.setattr(tracker, "run_tracker", lambda: (_ for _ in ()).throw(AssertionError("tracker must not run")))
    tracker.main(["--test-email"])
    assert sent == [True]


def test_smtp_failure_is_raised(monkeypatch):
    import email_sender

    class BrokenSMTP:
        def __init__(self, *_args, **_kwargs):
            raise OSError("offline")

    for key, value in {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "secret", "EMAIL_TO": "to@example.com"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", BrokenSMTP)
    with pytest.raises(EmailDeliveryError):
        send_email("subject", "body")


def test_gmail_587_uses_starttls(monkeypatch):
    import email_sender

    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def ehlo(self):
            events.append(("ehlo",))
        def starttls(self):
            events.append(("starttls",))
        def login(self, _user, _password):
            events.append(("login",))
        def send_message(self, _message):
            events.append(("send",))

    for key, value in {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "secret", "EMAIL_TO": "to@example.com"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    send_email("subject", "body")
    assert ("connect", "smtp.gmail.com", 587, 30) in events
    assert ("starttls",) in events and ("login",) in events and ("send",) in events


def test_no_change_main_succeeds_without_smtp(monkeypatch, tmp_path):
    report = tmp_path / "tracker_report.txt"
    report.write_text("normal report\n", encoding="utf-8")
    monkeypatch.setattr(tracker, "REPORT_TXT", report)
    monkeypatch.setattr(tracker, "run_tracker", lambda: ([], []))
    monkeypatch.setattr(tracker, "send_email", lambda *_args: (_ for _ in ()).throw(AssertionError("SMTP must not connect")))
    tracker.main([])
