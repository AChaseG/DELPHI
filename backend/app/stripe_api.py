"""The parts of Stripe's API Delphi uses, over httpx.

No SDK. Four calls and one signature check is not a dependency's worth of
surface, and this way the whole conversation with the payment processor is
readable in one file — which is the file most worth being able to read.

Configuration is environment-only, never the database:

    STRIPE_SECRET_KEY        sk_live_… / sk_test_…
    STRIPE_WEBHOOK_SECRET    whsec_…

A key belongs in `fly secrets`, not in a table an admin endpoint can print. The
*prices* are the operator's to set from the console (see billing.py); the
credentials are not, and keeping them apart means an operator console leak
cannot become a payments leak.

With no key set, `enabled()` is False and Delphi behaves as it always has: a
self-hosted instance charges nobody.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

import httpx

log = logging.getLogger("stripe")

API = "https://api.stripe.com/v1"
TIMEOUT = 20.0

# How far apart the webhook's timestamp and our clock may be. Stripe's own
# libraries use five minutes; the point is that a captured request cannot be
# replayed tomorrow.
SIGNATURE_TOLERANCE_S = 300


def secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def enabled() -> bool:
    """Can this instance take a payment at all?"""
    return bool(secret_key())


def live_mode() -> bool:
    """True when the key charges real cards. Worth saying out loud in the
    console: `sk_test_` keys take fake ones and look identical otherwise."""
    return secret_key().startswith("sk_live")


class StripeError(RuntimeError):
    """Stripe said no. The message is Stripe's own, which is usually the
    clearest available — it names the parameter it did not like."""


def _post(path: str, form: dict) -> dict:
    if not enabled():
        raise StripeError("Payments are not configured on this instance.")
    try:
        resp = httpx.post(f"{API}{path}", data=form, timeout=TIMEOUT,
                          auth=(secret_key(), ""))
    except httpx.HTTPError as exc:
        raise StripeError(f"Could not reach Stripe: {exc}") from exc
    if resp.status_code >= 400:
        try:
            message = resp.json()["error"]["message"]
        except Exception:
            message = f"Stripe returned {resp.status_code}"
        log.warning("stripe %s -> %s: %s", path, resp.status_code, message)
        raise StripeError(message)
    return resp.json()


def _get(path: str) -> dict:
    if not enabled():
        raise StripeError("Payments are not configured on this instance.")
    try:
        resp = httpx.get(f"{API}{path}", timeout=TIMEOUT, auth=(secret_key(), ""))
    except httpx.HTTPError as exc:
        raise StripeError(f"Could not reach Stripe: {exc}") from exc
    if resp.status_code >= 400:
        raise StripeError(f"Stripe returned {resp.status_code} for {path}")
    return resp.json()


def create_checkout_session(*, price_cents: int, currency: str, interval: str,
                            product_name: str, account_id: int, email: str,
                            customer_id: str, success_url: str,
                            cancel_url: str) -> dict:
    """A hosted Checkout page for one subscription.

    The price is sent inline (`price_data`) rather than referring to a Price
    object created in Stripe's dashboard. That is the whole reason the operator
    can set what Delphi costs from Delphi's own console: changing the number in
    settings changes what the next checkout charges, with nothing to keep in
    step on the other side.

    `client_reference_id` carries the account id through Stripe and back in the
    webhook, so a completed payment is attached to the right person even if
    they paid with an email Delphi has never seen.
    """
    form = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(account_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": str(int(price_cents)),
        "line_items[0][price_data][recurring][interval]": interval,
        "line_items[0][price_data][product_data][name]": product_name,
        # Stripe emails the receipt and handles the card details; Delphi never
        # sees a card number, which is the point of using a hosted page.
        "subscription_data[metadata][delphi_account]": str(account_id),
        "metadata[delphi_account]": str(account_id),
        "allow_promotion_codes": "true",
    }
    if customer_id:
        form["customer"] = customer_id
    elif email:
        form["customer_email"] = email
    return _post("/checkout/sessions", form)


def create_portal_session(*, customer_id: str, return_url: str) -> dict:
    """Stripe's own page for changing a card or cancelling.

    Cancellation lives there rather than here on purpose: whatever Delphi built
    would be a worse version of a page the customer's bank statement already
    points them at, and a subscription somebody cannot find the end of is a
    dark pattern however it is worded.
    """
    return _post("/billing_portal/sessions",
                 {"customer": customer_id, "return_url": return_url})


def get_subscription(subscription_id: str) -> dict:
    return _get(f"/subscriptions/{subscription_id}")


def verify_webhook(payload: bytes, header: str, *, now: float | None = None) -> dict:
    """The event in `payload`, if this really came from Stripe.

    Everything about a subscription arrives this way, and the endpoint has to
    be public for Stripe to reach it, so this signature is the only thing
    standing between "somebody paid" and anybody who can post JSON. It follows
    Stripe's scheme exactly: v1 = HMAC-SHA256 of "<timestamp>.<body>" under the
    endpoint's signing secret, compared in constant time, with a timestamp
    inside the tolerance so a captured request cannot be replayed later.

    Raises ValueError with a reason. Never returns a partially-checked event.
    """
    if not webhook_secret():
        raise ValueError("no webhook secret configured")
    # One pass, kept as pairs rather than a dict: Stripe sends several v1
    # signatures during a secret rotation, and a dict would keep only the last
    # of them — which is the one that fails while the old secret is still in
    # use.
    pairs = [piece.split("=", 1) for piece in (header or "").split(",") if "=" in piece]
    stamps = [v for k, v in pairs if k == "t"]
    try:
        timestamp = int(stamps[0])
    except (IndexError, ValueError):
        raise ValueError("no timestamp in the signature header")
    signatures = [v for k, v in pairs if k == "v1"]
    if not signatures:
        raise ValueError("no v1 signature in the header")

    age = abs((time.time() if now is None else now) - timestamp)
    if age > SIGNATURE_TOLERANCE_S:
        raise ValueError(f"signature is {int(age)}s old, outside the tolerance")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(webhook_secret().encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, given) for given in signatures):
        raise ValueError("signature does not match")

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"body is not JSON: {exc}") from exc
