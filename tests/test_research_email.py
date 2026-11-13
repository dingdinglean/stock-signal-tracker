from __future__ import annotations

import smtplib

from email_sender import send_email
from notifications import prepare_email_data

from research.cohort import assign_cohort


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, username, password):
        self.username = username
        self.password = password

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


def test_dry_run_smtp_records_recipient_and_body_without_a_network_call(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "not-a-real-secret")
    monkeypatch.setenv("EMAIL_TO", "recipient@example.invalid")
    send_email("研究干跑", "<p>日线样本含亏损</p>")
    message = _FakeSMTP.sent[0]
    assert message["To"] == "recipient@example.invalid"
    assert message["From"] == "sender@example.invalid"
    assert "日线样本含亏损" in message.get_body(preferencelist=("html",)).get_content()
    hidden = {"signal_id": "h", "display_state": "hidden", "source_timeframe": "weekly", "signal_date": "2026-09-04", "latest_return": "-3", "quote_status": "ok", "price_as_of": "2026-11-11"}
    visible = {"signal_id": "v", "display_state": "visible", "source_timeframe": "weekly", "signal_date": "2026-10-01", "latest_return": "2", "quote_status": "pending"}
    prepared = prepare_email_data([], [hidden, visible])
    assert hidden not in prepared.display_history
    assert any(row["signal_id"] == "h" for row in prepared.all_history)
    assert assign_cohort(hidden) == "weekly"
