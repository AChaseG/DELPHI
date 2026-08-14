"""Who may use this Delphi, and what it costs.

Three ways in, checked in this order:

    comped    the operator invited them, or they were here before billing was
              switched on. Free, forever, and it outranks everything else.
    paid      a Stripe subscription that is paid up to a date in the future.
    trial     a new account's first days, however many the operator allows.

Anything else is `expired`, which is the paywall. The states are *derived* from
facts on the account rather than stored as one column, because they are written
from three different places — a webhook, an operator, the clock — and a single
column would have each writer guessing what the other two meant.

Access is granted against `paid_until`, never against Stripe's status word. A
webhook that never arrives, a Stripe outage, or an instance that was offline for
a day cannot cut off somebody who has paid: they keep what they bought until the
date they bought it to. The same rule the other way is deliberate — a cancelled
subscription keeps working until the period ends, because that is what they paid
for.

Prices live in the database so the operator can change them from the console;
the Stripe credentials live in the environment (see stripe_api.py) so an
operator console cannot leak them.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import stripe_api
from .models import InstanceSetting, Invite, User, utcnow

log = logging.getLogger("billing")

SETTINGS_KEY = "billing"

# What an instance charges until somebody says otherwise. Billing off, because
# a self-hosted copy of Delphi that started charging its owner's friends the
# moment they deployed it would be a hostile default.
DEFAULTS: dict = {
    "enabled": False,          # is the paywall on at all?
    "price_cents": 900,        # what a period costs
    "currency": "usd",
    "interval": "month",       # month | year
    "product_name": "D.E.L.P.H.I.",
    "trial_days": 14,          # 0 = no trial, straight to the paywall
    "blurb": "",               # optional line shown on the paywall
}

CURRENCIES = {"usd", "eur", "gbp", "cad", "aud", "nzd", "chf", "sek", "nok",
              "dkk", "jpy", "sgd", "hkd", "pln", "czk", "mxn", "brl", "inr",
              "zar"}
INTERVALS = {"month", "year"}
# Stripe's own floor is about $0.50; below that the fee exceeds the charge.
MIN_PRICE_CENTS = 50
MAX_PRICE_CENTS = 100_000_00
MAX_TRIAL_DAYS = 365

# Currencies quoted in whole units — ¥500 is 500, not 50,000. Getting this
# wrong charges a hundred times too much, so it is a list rather than a guess.
ZERO_DECIMAL = {"jpy", "krw", "vnd", "clp", "isk", "ugx", "xaf", "xof"}


class BillingError(ValueError):
    """Something the operator or the customer needs told in words."""


# ---------- settings ----------

def settings(db: Session) -> dict:
    """The instance's billing settings, with every default filled in."""
    row = db.get(InstanceSetting, SETTINGS_KEY)
    stored = {}
    if row and row.value:
        try:
            stored = json.loads(row.value)
        except json.JSONDecodeError:
            log.warning("billing settings are not JSON; using defaults")
    out = {**DEFAULTS, **(stored if isinstance(stored, dict) else {})}
    # Billing cannot be on without a key to charge with, whatever the row says.
    # Otherwise removing the Stripe secret would lock every account out of an
    # instance that has no way to take their money.
    out["enabled"] = bool(out.get("enabled")) and stripe_api.enabled()
    return out


def save_settings(db: Session, changes: dict) -> dict:
    """Validate and store operator changes. Returns the settings as saved."""
    current = {**DEFAULTS, **_stored(db)}
    for key, value in (changes or {}).items():
        if key not in DEFAULTS:
            continue                       # unknown keys are ignored, not errors
        current[key] = value

    if not isinstance(current["price_cents"], int) or isinstance(current["price_cents"], bool):
        try:
            current["price_cents"] = int(current["price_cents"])
        except (TypeError, ValueError):
            raise BillingError("The price has to be a whole number of cents.")
    if not MIN_PRICE_CENTS <= current["price_cents"] <= MAX_PRICE_CENTS:
        raise BillingError(
            f"The price has to be between {MIN_PRICE_CENTS} and {MAX_PRICE_CENTS} "
            f"cents ({MIN_PRICE_CENTS / 100:.2f}–{MAX_PRICE_CENTS / 100:,.2f}).")
    current["currency"] = str(current["currency"]).strip().lower()
    if current["currency"] not in CURRENCIES:
        raise BillingError(f"{current['currency']!r} is not a currency Delphi knows. "
                           f"Pick one of: {', '.join(sorted(CURRENCIES))}.")
    current["interval"] = str(current["interval"]).strip().lower()
    if current["interval"] not in INTERVALS:
        raise BillingError("Billing is either monthly or yearly.")
    try:
        current["trial_days"] = int(current["trial_days"])
    except (TypeError, ValueError):
        raise BillingError("The trial length has to be a whole number of days.")
    if not 0 <= current["trial_days"] <= MAX_TRIAL_DAYS:
        raise BillingError(f"A trial can run from 0 to {MAX_TRIAL_DAYS} days.")
    current["product_name"] = str(current["product_name"]).strip()[:120] or "D.E.L.P.H.I."
    current["blurb"] = str(current["blurb"]).strip()[:300]
    current["enabled"] = bool(current["enabled"])
    if current["enabled"] and not stripe_api.enabled():
        raise BillingError(
            "There is no Stripe key on this instance, so nothing could be "
            "charged and everyone would be locked out. Set STRIPE_SECRET_KEY "
            "and STRIPE_WEBHOOK_SECRET first (fly secrets set …).")

    row = db.get(InstanceSetting, SETTINGS_KEY)
    if row is None:
        row = InstanceSetting(key=SETTINGS_KEY)
        db.add(row)
    row.value = json.dumps(current)
    row.updated_at = utcnow()
    db.commit()
    return settings(db)


def _stored(db: Session) -> dict:
    row = db.get(InstanceSetting, SETTINGS_KEY)
    if not row or not row.value:
        return {}
    try:
        value = json.loads(row.value)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def price_label(conf: dict) -> str:
    """"$9.00 a month" — what a person needs to see before they click."""
    cents = int(conf.get("price_cents") or 0)
    currency = str(conf.get("currency") or "usd").lower()
    symbol = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥"}.get(currency, "")
    if currency in ZERO_DECIMAL:
        amount = f"{symbol}{cents:,}"
    else:
        amount = f"{symbol}{cents / 100:,.2f}"
    if not symbol:
        amount = f"{amount} {currency.upper()}"
    return f"{amount} a {conf.get('interval', 'month')}"


# ---------- who may use it ----------

def access_of(user: User, conf: dict, *, now: datetime | None = None) -> dict:
    """This account's access, as a state and the date it runs to.

    Pure: it reads the account and the settings and nothing else, so it can be
    asked anywhere — the middleware on every request, the console, a test —
    without a database write or a call to Stripe.
    """
    now = now or utcnow()
    if not conf.get("enabled"):
        return {"state": "open", "until": None, "days_left": None}
    if user.comped:
        return {"state": "comped", "until": None, "days_left": None}
    if user.paid_until and user.paid_until > now:
        return {"state": "paid", "until": user.paid_until,
                "days_left": _days(user.paid_until, now)}
    if user.trial_ends_at and user.trial_ends_at > now:
        return {"state": "trial", "until": user.trial_ends_at,
                "days_left": _days(user.trial_ends_at, now)}
    return {"state": "expired", "until": user.trial_ends_at or user.paid_until,
            "days_left": 0}


def _days(when: datetime, now: datetime) -> int:
    """Days left, rounded up: half a day left is still "1 day", because
    "0 days left" next to a working dashboard reads as a bug."""
    seconds = (when - now).total_seconds()
    return max(0, -(-int(seconds) // 86400))


def may_use(user: User, conf: dict, *, now: datetime | None = None) -> bool:
    return access_of(user, conf, now=now)["state"] != "expired"


def start_trial(user: User, conf: dict, *, now: datetime | None = None) -> None:
    """Put a new account on the clock, if this instance runs trials.

    Only ever sets the date — never moves one that exists. Re-registering, or
    an operator toggling billing off and on, must not hand anybody a second
    trial.
    """
    if user.trial_ends_at is not None or user.comped:
        return
    days = int(conf.get("trial_days") or 0)
    if days <= 0:
        # No trial: the account still gets a date, in the past, so that
        # "expired" is a fact about it rather than the absence of one.
        user.trial_ends_at = (now or utcnow())
        return
    user.trial_ends_at = (now or utcnow()) + timedelta(days=days)


def status_json(user: User, conf: dict) -> dict:
    """Everything the client needs to render the paywall or the trial notice."""
    access = access_of(user, conf)
    out = {
        "state": access["state"],
        "until": access["until"].isoformat() + "Z" if access["until"] else None,
        "days_left": access["days_left"],
        "billing_enabled": bool(conf.get("enabled")),
        "price_label": price_label(conf),
        "price_cents": int(conf.get("price_cents") or 0),
        "currency": conf.get("currency"),
        "interval": conf.get("interval"),
        "product_name": conf.get("product_name"),
        "blurb": conf.get("blurb") or "",
        "trial_days": int(conf.get("trial_days") or 0),
        # A subscription that is cancelled but still inside its period: the
        # difference between "you are paid up" and "this ends on the 4th".
        "cancelling": bool(user.subscription_status == "canceled" and user.paid_until),
        "can_manage": bool(user.stripe_customer_id),
        "comped_note": user.comped_note if user.comped else "",
    }
    return out


# ---------- invitations ----------

# No I, O, 0 or 1: these get read aloud and typed from memory.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Z0-9-]{4,40}$")


def new_code(words: int = 3, size: int = 4) -> str:
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(size))
                    for _ in range(words))


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()


def create_invite(db: Session, operator: User, *, note: str = "",
                  max_uses: int = 1, expires_days: int | None = None) -> Invite:
    try:
        max_uses = int(max_uses)
    except (TypeError, ValueError):
        raise BillingError("Uses has to be a whole number (0 for unlimited).")
    if max_uses < 0 or max_uses > 10_000:
        raise BillingError("Uses has to be between 0 (unlimited) and 10,000.")
    expires_at = None
    if expires_days:
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            raise BillingError("Expiry has to be a whole number of days.")
        if not 1 <= days <= 3650:
            raise BillingError("An invitation can last from 1 to 3,650 days.")
        expires_at = utcnow() + timedelta(days=days)
    invite = Invite(code=new_code(), note=(note or "").strip()[:200],
                    created_by=operator.id, max_uses=max_uses,
                    expires_at=expires_at)
    db.add(invite)
    db.commit()
    return invite


def invite_state(invite: Invite, *, now: datetime | None = None) -> str:
    now = now or utcnow()
    if invite.revoked:
        return "revoked"
    if invite.expires_at and invite.expires_at <= now:
        return "expired"
    if invite.max_uses and invite.used_count >= invite.max_uses:
        return "used up"
    return "open"


def redeem(db: Session, user: User, code: str, *,
           now: datetime | None = None) -> Invite:
    """Comp this account against a code, or say exactly why not.

    The reasons are separated on purpose. "That code has already been used" and
    "there is no such code" are different problems for the person holding it,
    and telling them "invalid" for both is how a mistyped character becomes a
    message to the operator.
    """
    cleaned = normalize_code(code)
    if not cleaned or not _CODE_RE.match(cleaned):
        raise BillingError("That does not look like an invitation code.")
    invite = db.scalar(select(Invite).where(Invite.code == cleaned))
    if invite is None:
        raise BillingError("No invitation with that code. Check it for typos — "
                           "codes never contain the letters I or O, or the "
                           "digits 0 or 1.")
    if user.comped:
        # They already have what this grants. Checked before the code's own
        # state so a second click, a reload, or a link followed twice is a
        # no-op rather than "that invitation has already been used" — which it
        # has, by them, a second ago. No use is spent either.
        return invite
    state = invite_state(invite, now=now)
    if state == "revoked":
        raise BillingError("That invitation was withdrawn by the operator.")
    if state == "expired":
        raise BillingError("That invitation has expired.")
    if state == "used up":
        raise BillingError("That invitation has already been used as many "
                           "times as it allows.")
    user.comped = True
    user.comped_note = invite.note or f"invited ({invite.code})"
    user.invited_by_code = invite.code
    invite.used_count += 1
    db.commit()
    log.info("account %s comped by invite %s", user.id, invite.code)
    return invite


# ---------- what Stripe tells us ----------

def apply_subscription(db: Session, user: User, sub: dict) -> None:
    """Record a subscription object from Stripe against an account."""
    user.subscription_id = str(sub.get("id") or "")[:64]
    user.subscription_status = str(sub.get("status") or "")[:24]
    customer = sub.get("customer")
    if isinstance(customer, str) and customer:
        user.stripe_customer_id = customer[:64]
    end = sub.get("current_period_end")
    if isinstance(end, (int, float)) and end > 0:
        user.paid_until = datetime.utcfromtimestamp(int(end))
    if user.subscription_status in ("canceled", "incomplete_expired") and not end:
        user.paid_until = None
    db.commit()


def user_for_event(db: Session, obj: dict) -> User | None:
    """The account a Stripe object belongs to.

    Three ways, in order of how much they can be trusted: the id Delphi put on
    the object when it created the checkout, the customer id already recorded
    against an account, and — last — the email. The email is last because it is
    the customer's to change at Stripe, and matching on it alone would let
    somebody claim another account's subscription by editing a field.
    """
    for key in ("client_reference_id",):
        raw = obj.get(key)
        if raw and str(raw).isdigit():
            user = db.get(User, int(raw))
            if user:
                return user
    meta = obj.get("metadata") or {}
    raw = meta.get("delphi_account")
    if raw and str(raw).isdigit():
        user = db.get(User, int(raw))
        if user:
            return user
    customer = obj.get("customer")
    if isinstance(customer, str) and customer:
        user = db.scalar(select(User).where(User.stripe_customer_id == customer))
        if user:
            return user
    return None
