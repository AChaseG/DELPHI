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


def send_attachment(to: str, subject: str, body: str, *, filename: str,
                    data: bytes, mime: str = "application/json") -> bool:
    """Send a plain-text email carrying one file. Returns True on success.

    Separate from send() rather than an optional argument to it, because the
    reason to attach something is different from the reason to write something:
    everything send() carries is a link the reader is meant to click, and this
    carries a file they are meant to keep.
    """
    if not enabled():
        log.info("mail disabled — would send %s (%d bytes) to %s: %s",
                 filename, len(data), to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    maintype, _, subtype = mime.partition("/")
    msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                       filename=filename)
    try:
        if TLS == "ssl":
            server = smtplib.SMTP_SSL(HOST, PORT, timeout=30)
        else:
            server = smtplib.SMTP(HOST, PORT, timeout=30)
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


KM_PER_MILE = 1.609344


def format_km(km: float, units: str = "km") -> str:
    """A distance in whichever unit the account reads in.

    The server stores and compares kilometres and always will; this converts on
    the way into a sentence, the same discipline the client follows. An email
    is the one place the reader cannot flip a toggle to reinterpret, so it has
    to arrive in the unit they chose.
    """
    value = float(km or 0)
    return (f"{value / KM_PER_MILE:.1f} miles" if units == "mi"
            else f"{value:.1f} km")


def send_hazard_digest(to: str, place_name: str, hits: list[dict],
                       units: str = "km") -> bool:
    """One email per watched place per hazard poll, listing the fires near it.

    Worded carefully about distance. WFIGS reports an incident *point*, not a
    perimeter, and a hundred-thousand-acre fire's point can sit twenty
    kilometres inside its own edge — so this says "reported N km away" rather
    than "N km away", which would be a claim about the fire's nearest flame
    that nobody here is in a position to make.
    """
    lines = []
    for h in hits:
        facts = []
        if h.get("acres") is not None:
            facts.append(f"{float(h['acres']):,.0f} acres")
        if h.get("containment") is not None:
            facts.append(f"{h['containment']}% contained")
        detail = f" — {', '.join(facts)}" if facts else ""
        grew = " (grown since we last told you)" if h.get("again") else ""
        lines.append(f"{h['name']}{detail}\n"
                     f"  reported {format_km(h['distance_km'], units)} "
                     f"from {place_name}{grew}")
    n = len(hits)
    return send(
        to, f"🔥 D.E.L.P.H.I.: {n} wildfire{'' if n == 1 else 's'} near {place_name}",
        f"{'A wildfire is' if n == 1 else f'{n} wildfires are'} burning within the "
        f"distance you set for “{place_name}”:\n\n" + "\n\n".join(lines) +
        "\n\nDistances are to the incident point the agency reported, which for a "
        "large fire can be well inside its own perimeter. Open D.E.L.P.H.I. and "
        "switch on Typhon to see them on the map.\n\n"
        "To stop these, turn off the wildfire distance on that place.\n",
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


def send_device_release(to: str, username: str, link: str, in_use: int) -> bool:
    """The way back in for someone the device limit has locked out.

    They cannot reach the app to ask, so the request is made from the sign-in
    screen with their address alone — which means this mail has to be
    self-explanatory to somebody who did not ask for it, and has to say what
    the link will do before they follow it.
    """
    return send(
        to, "Sign out of your other D.E.L.P.H.I. devices",
        f"Hello {username},\n\n"
        f"Someone (hopefully you) could not open D.E.L.P.H.I. because the account is "
        f"already in use on {in_use} {'device' if in_use == 1 else 'devices'}, which is "
        f"its limit.\n\n"
        f"Following this link signs the account out everywhere and clears the list, so "
        f"you can\nsign in again on the device you are holding:\n\n{link}\n\n"
        f"The link is valid for 1 hour and can be used once. Every device, including any "
        f"you are\nstill using, will need to sign in again.\n\n"
        f"If you didn't request this, ignore it — nothing changes unless the link is "
        f"followed, and\nit does not reveal your password or let anyone in.\n",
    )
