"""Outbound email (stdlib smtplib — no dependencies).

Configure via environment:
  NEWS_SMTP_HOST     e.g. smtp.resend.com / smtp.mailgun.org / email-smtp.us-east-1.amazonaws.com
  NEWS_SMTP_PORT     default 587
  NEWS_SMTP_USER     SMTP username (often the API-key name)
  NEWS_SMTP_PASS     SMTP password / API key
  NEWS_SMTP_FROM     From address, e.g. "D.E.L.P.H.I. <delphi@yourdomain.com>"
  NEWS_SMTP_TLS      starttls (default) | ssl | none

When NEWS_SMTP_HOST is unset, mail is disabled: accounts auto-verify at
registration (self-host mode) and any would-be message is logged instead,
including its action link — so flows remain testable from the server log.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("mailer")

HOST = os.environ.get("NEWS_SMTP_HOST", "")
PORT = int(os.environ.get("NEWS_SMTP_PORT", "587"))
USER = os.environ.get("NEWS_SMTP_USER", "")
PASSWORD = os.environ.get("NEWS_SMTP_PASS", "")
FROM = os.environ.get("NEWS_SMTP_FROM", USER or "delphi@localhost")
TLS = os.environ.get("NEWS_SMTP_TLS", "starttls").lower()


def enabled() -> bool:
    return bool(HOST)


def send(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success. Never raises."""
    if not enabled():
        log.info("mail disabled — would send to %s: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if TLS == "ssl":
            server = smtplib.SMTP_SSL(HOST, PORT, timeout=20)
        else:
            server = smtplib.SMTP(HOST, PORT, timeout=20)
        with server:
            if TLS == "starttls":
                server.starttls()
            if USER:
                server.login(USER, PASSWORD)
            server.send_message(msg)
        return True
    except Exception as exc:
        log.error("send to %s failed: %s", to, exc)
        return False


def send_verification(to: str, username: str, link: str) -> bool:
    return send(
        to, "Verify your D.E.L.P.H.I. account",
        f"Hello {username},\n\n"
        f"Confirm this email address to activate your D.E.L.P.H.I. account:\n\n{link}\n\n"
        f"The link is valid for 48 hours. If you didn't create this account, ignore this message.\n",
    )


def send_alert_digest(to: str, alert_name: str, hits: list[dict]) -> bool:
    """One email per alert per ingest cycle, listing the new matching stories."""
    lines = [f"{h['title']}\n  {h.get('url', '')}" for h in hits]
    n = len(hits)
    return send(
        to, f"🔔 D.E.L.P.H.I. alert: {alert_name} ({n} new)",
        f"Your alert “{alert_name}” matched {n} new "
        f"{'story' if n == 1 else 'stories'}:\n\n" + "\n\n".join(lines) +
        "\n\nOpen D.E.L.P.H.I. to see the full context, map, and timeline.\n",
    )


def send_password_reset(to: str, username: str, link: str) -> bool:
    return send(
        to, "Reset your D.E.L.P.H.I. password",
        f"Hello {username},\n\n"
        f"Someone (hopefully you) requested a password reset. Set a new password here:\n\n{link}\n\n"
        f"The link is valid for 1 hour. If you didn't request this, you can safely ignore it —\n"
        f"your current password keeps working.\n",
    )
