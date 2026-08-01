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


# What mail has been doing lately.
#
# Sending happens in a background task, deliberately: the reply to "reset my
# password" must not reveal whether the address exists, and a stalled relay
# must not hang the caller. The cost is that a failure had nowhere to go but
# the log — a reader waits for a link that will never arrive, and the operator
# has no reason to go looking. This record is surfaced in the operator console
# and /api/ingest/status so a broken relay is visible without reading logs.
status: dict = {"sent": 0, "failures": 0, "last_error": None, "last_error_at": None,
                "last_sent_at": None}


def _explain(exc: Exception) -> str:
    """The SMTP failure in terms of what to go and change.

    smtplib's own text names a code and a server string, which says nothing
    about which of the six settings is wrong."""
    name = type(exc).__name__
    detail = str(exc).strip()[:200]
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"the mail server rejected the credentials in NEWS_SMTP_USER / "
                f"NEWS_SMTP_PASS ({detail}). Providers that require an app "
                f"password reject an account password here.")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"the mail server refused the From address {FROM!r} "
                f"(NEWS_SMTP_FROM) — most relays only accept an address they "
                f"host ({detail}).")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"the mail server refused the recipient address ({detail})."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return (f"the mail server does not support {TLS!r} on port {PORT} — "
                f"try NEWS_SMTP_TLS=ssl on 465, or starttls on 587 ({detail}).")
    if isinstance(exc, (TimeoutError, OSError)) and "timed out" in detail.lower():
        return (f"connecting to {HOST}:{PORT} timed out — the host or port may "
                f"be wrong, or outbound SMTP may be blocked.")
    if isinstance(exc, OSError):
        return (f"could not connect to {HOST}:{PORT} ({name}: {detail}) — check "
                f"NEWS_SMTP_HOST and NEWS_SMTP_PORT.")
    return f"{name}: {detail}"


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
        status["sent"] += 1
        status["last_sent_at"] = _now()
        return True
    except Exception as exc:
        why = _explain(exc)
        status["failures"] += 1
        status["last_error"] = why
        status["last_error_at"] = _now()
        # The address is not logged: a failed reset would otherwise put the
        # email address of whoever asked into the log in plain text.
        log.error("mail send failed — %s", why)
        return False


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def send_duplicate_registration(to: str, username: str, reset_link: str) -> bool:
    """Someone tried to register with an address that already has an account.

    Registration cannot say "that email is taken" without telling whoever asked
    whether an address is registered here, which is worth knowing about a
    news-monitoring tool — it says something about the person. So the sign-up
    form answers identically either way and this goes to the address itself,
    where only its owner can read it. It is also genuinely useful: if you had
    forgotten you had an account, this is how you find out, and the reset link
    is the way back in.
    """
    return send(
        to, "You already have a D.E.L.P.H.I. account",
        f"Hello {username},\n\n"
        f"Someone just tried to create a D.E.L.P.H.I. account with this email address, "
        f"but you already have one — the username is “{username}”.\n\n"
        f"If that was you and you have forgotten your password, set a new one here:\n\n"
        f"{reset_link}\n\n"
        f"The link is valid for 1 hour. If it wasn't you, there is nothing to do: no "
        f"second account was created and your existing one is untouched.\n",
    )


def send_password_reset(to: str, username: str, link: str) -> bool:
    return send(
        to, "Reset your D.E.L.P.H.I. password",
        f"Hello {username},\n\n"
        f"Someone (hopefully you) requested a password reset. Set a new password here:\n\n{link}\n\n"
        f"The link is valid for 1 hour. If you didn't request this, you can safely ignore it —\n"
        f"your current password keeps working.\n",
    )
