"""Minimal Gmail-compatible SMTP delivery for tracker notifications."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


class EmailDeliveryError(RuntimeError):
    """Raised when an expected tracker notification cannot be delivered."""


def _config() -> tuple[str, int, str, str, str]:
    host = os.getenv("SMTP_HOST", "").strip()
    port_text = os.getenv("SMTP_PORT", "").strip()
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    recipient = os.getenv("EMAIL_TO", "").strip()
    missing = [name for name, value in (("SMTP_HOST", host), ("SMTP_PORT", port_text), ("SMTP_USER", username), ("SMTP_PASSWORD", password), ("EMAIL_TO", recipient)) if not value]
    if missing:
        raise EmailDeliveryError(f"Missing SMTP configuration: {', '.join(missing)}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise EmailDeliveryError("SMTP_PORT must be a number") from exc
    return host, port, username, password, recipient


def send_email(subject: str, body: str) -> None:
    """Send once through STARTTLS on 587; never log configuration or secrets."""
    host, port, username, password, recipient = _config()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = recipient
    message.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as client:
            client.ehlo()
            if port == 587:
                client.starttls()
                client.ehlo()
            client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError(f"Tracker SMTP delivery failed: {exc.__class__.__name__}") from exc


def send_connectivity_test() -> None:
    send_email("【美股信号验证器】邮件连通性测试", "美股信号验证器邮件发送正常。")
