"""Who may use this Delphi, and what it costs.

Money and access, so the tests are about the ways this goes wrong rather than
the way it goes right. Four of them matter more than the rest:

  * **Nobody loses access to an instance that cannot charge them.** Billing off,
    or a Stripe key that is not there, means everybody is in. A paywall that
    engages when payments are broken locks out the operator too — including
    from the console that would switch it off.
  * **Access is granted against a date, not against Stripe's mood.** A webhook
    that never arrives, an outage, a declined card on a Tuesday: none of those
    may cut off somebody inside a period they have paid for.
  * **The webhook is the front door.** It is public because Stripe has no
    account here, so its signature is the only thing between "somebody paid"
    and anybody who can post JSON.
  * **The paywall is default-on for new routes.** Access is checked in the
    middleware against a short exemption list, so an endpoint added next year
    is behind it without anybody remembering to do anything.
"""
import json
import time
from datetime import timedelta

import pytest

from backend.app import billing, main, stripe_api
from backend.app.models import InstanceSetting, Invite, User, utcnow


@pytest.fixture
def paid_instance(db, monkeypatch):
    """An instance that charges: a Stripe key present, billing switched on."""
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    billing.save_settings(db, {"enabled": True, "price_cents": 900,
                               "currency": "usd", "interval": "month",
                               "trial_days": 14})
    return billing.settings(db)


def account(db, name="payer", **kw):
    user = User(username=name, email=f"{name}@example.com", password_hash="x", **kw)
    db.add(user)
    db.commit()
    return user


# ---------- an instance that cannot charge never locks anyone out ----------

def test_billing_is_off_until_an_operator_turns_it_on(db):
    assert billing.settings(db)["enabled"] is False
    assert billing.access_of(account(db), billing.settings(db))["state"] == "open"


def test_billing_cannot_be_on_without_a_way_to_take_money(db, monkeypatch):
    """The failure that locks the operator out of the console that would fix
    it. Stored as on, reported as off, everybody still inside."""
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    billing.save_settings(db, {"enabled": True})
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "")     # key removed
    conf = billing.settings(db)
    assert conf["enabled"] is False
    assert billing.may_use(account(db), conf)


def test_switching_it_on_without_a_key_is_refused_with_a_reason(db, monkeypatch):
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "")
    with pytest.raises(billing.BillingError) as exc:
        billing.save_settings(db, {"enabled": True})
    assert "STRIPE_SECRET_KEY" in str(exc.value)


# ---------- the three ways in ----------

def test_a_comped_account_never_expires(db, paid_instance):
    user = account(db, comped=True, trial_ends_at=utcnow() - timedelta(days=400))
    assert billing.access_of(user, paid_instance)["state"] == "comped"
    assert billing.may_use(user, paid_instance)


def test_a_paid_account_is_in_until_the_period_ends(db, paid_instance):
    user = account(db, paid_until=utcnow() + timedelta(days=3))
    access = billing.access_of(user, paid_instance)
    assert access["state"] == "paid" and access["days_left"] == 3


def test_a_cancelled_subscription_keeps_what_it_paid_for(db, paid_instance):
    """Stripe says canceled; the period runs to Friday. They bought Friday."""
    user = account(db, subscription_status="canceled",
                   paid_until=utcnow() + timedelta(days=2))
    assert billing.may_use(user, paid_instance)


def test_a_declined_card_does_not_lock_somebody_out_on_the_spot(db, paid_instance):
    user = account(db, subscription_status="past_due",
                   paid_until=utcnow() + timedelta(days=6))
    assert billing.may_use(user, paid_instance)


def test_a_trial_runs_out(db, paid_instance):
    user = account(db, trial_ends_at=utcnow() - timedelta(minutes=1))
    assert billing.access_of(user, paid_instance)["state"] == "expired"
    assert not billing.may_use(user, paid_instance)


def test_a_new_account_gets_the_trial_the_operator_set(db, paid_instance):
    user = account(db)
    billing.start_trial(user, paid_instance)
    assert billing.access_of(user, paid_instance)["state"] == "trial"
    assert billing.access_of(user, paid_instance)["days_left"] == 14


def test_a_trial_is_never_handed_out_twice(db, paid_instance):
    """Toggling billing off and on, or any second call, must not extend one."""
    user = account(db, trial_ends_at=utcnow() - timedelta(days=1))
    billing.start_trial(user, paid_instance)
    assert not billing.may_use(user, paid_instance)


def test_zero_trial_days_means_the_paywall_immediately(db, paid_instance, monkeypatch):
    billing.save_settings(db, {"trial_days": 0})
    conf = billing.settings(db)
    user = account(db)
    billing.start_trial(user, conf)
    assert billing.access_of(user, conf)["state"] == "expired"


def test_days_left_rounds_up(db, paid_instance):
    """"0 days left" beside a working dashboard reads as a bug."""
    user = account(db, trial_ends_at=utcnow() + timedelta(hours=4))
    assert billing.access_of(user, paid_instance)["days_left"] == 1


# ---------- what the operator may set ----------

@pytest.mark.parametrize("bad, message", [
    ({"price_cents": 3}, "between"),
    ({"price_cents": "nine dollars"}, "whole number"),
    ({"currency": "xyz"}, "currency"),
    ({"interval": "fortnight"}, "monthly or yearly"),
    ({"trial_days": -1}, "0 to"),
    ({"trial_days": 4000}, "0 to"),
])
def test_nonsense_pricing_is_refused(db, monkeypatch, bad, message):
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    with pytest.raises(billing.BillingError) as exc:
        billing.save_settings(db, bad)
    assert message in str(exc.value)


def test_settings_survive_a_corrupt_row(db):
    """A hand-edited or half-written row must not take the instance down."""
    db.add(InstanceSetting(key=billing.SETTINGS_KEY, value="{not json"))
    db.commit()
    assert billing.settings(db)["price_cents"] == billing.DEFAULTS["price_cents"]


def test_the_price_reads_like_money(db):
    assert billing.price_label({"price_cents": 900, "currency": "usd",
                                "interval": "month"}) == "$9.00 a month"
    assert billing.price_label({"price_cents": 9000, "currency": "gbp",
                                "interval": "year"}) == "£90.00 a year"


def test_a_zero_decimal_currency_is_not_divided_by_a_hundred():
    """¥500 is 500, not 50,000. Getting this backwards charges a hundred times
    too much, or a hundredth."""
    assert billing.price_label({"price_cents": 500, "currency": "jpy",
                                "interval": "month"}) == "¥500 a month"


# ---------- invitations ----------

def test_an_invitation_comps_the_account_that_uses_it(db, paid_instance):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator, note="Sam at the paper")
    user = account(db, "sam", trial_ends_at=utcnow() - timedelta(days=1))
    assert not billing.may_use(user, paid_instance)

    billing.redeem(db, user, invite.code)
    assert billing.access_of(user, paid_instance)["state"] == "comped"
    assert user.invited_by_code == invite.code
    assert invite.used_count == 1


def test_a_code_is_read_the_way_it_is_typed(db, paid_instance):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator)
    user = account(db, "sam")
    billing.redeem(db, user, f"  {invite.code.lower()} ")
    assert user.comped


def test_codes_avoid_the_characters_people_get_wrong():
    for _ in range(50):
        assert not set(billing.new_code()) & set("IO01")


@pytest.mark.parametrize("setup, message", [
    (lambda inv: setattr(inv, "revoked", True), "withdrawn"),
    (lambda inv: setattr(inv, "expires_at", utcnow() - timedelta(days=1)), "expired"),
    (lambda inv: setattr(inv, "used_count", 1), "as many times"),
])
def test_a_code_that_cannot_be_used_says_which(db, paid_instance, setup, message):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator, max_uses=1)
    setup(invite)
    db.commit()
    with pytest.raises(billing.BillingError) as exc:
        billing.redeem(db, account(db, "sam"), invite.code)
    assert message in str(exc.value)


def test_an_unknown_code_says_so_without_being_a_guessing_game(db):
    with pytest.raises(billing.BillingError) as exc:
        billing.redeem(db, account(db, "sam"), "AAAA-BBBB-CCCC")
    assert "No invitation" in str(exc.value)


def test_a_shared_code_can_be_used_the_number_of_times_it_allows(db, paid_instance):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator, max_uses=2)
    billing.redeem(db, account(db, "one"), invite.code)
    billing.redeem(db, account(db, "two"), invite.code)
    with pytest.raises(billing.BillingError):
        billing.redeem(db, account(db, "three"), invite.code)


def test_redeeming_twice_does_not_spend_two_uses(db, paid_instance):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator, max_uses=1)
    user = account(db, "sam")
    billing.redeem(db, user, invite.code)
    billing.redeem(db, user, invite.code)          # a second click, a reload
    assert invite.used_count == 1


def test_revoking_a_code_leaves_the_people_it_let_in_alone(db, paid_instance):
    operator = account(db, "op", is_admin=True)
    invite = billing.create_invite(db, operator)
    user = account(db, "sam")
    billing.redeem(db, user, invite.code)
    invite.revoked = True
    db.commit()
    assert billing.may_use(user, paid_instance), (
        "revoking a code must not silently evict somebody already using Delphi")


# ---------- the webhook is the front door ----------

def _signed(payload: bytes, secret="whsec_x", when=None):
    import hashlib
    import hmac
    ts = int(when if when is not None else time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_a_signed_event_is_read(monkeypatch):
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    body = json.dumps({"type": "customer.subscription.updated"}).encode()
    assert stripe_api.verify_webhook(body, _signed(body))["type"] == \
        "customer.subscription.updated"


@pytest.mark.parametrize("header, reason", [
    ("", "timestamp"),
    ("t=abc,v1=deadbeef", "timestamp"),
    ("v1=deadbeef", "timestamp"),
])
def test_a_malformed_signature_is_refused(monkeypatch, header, reason):
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    with pytest.raises(ValueError) as exc:
        stripe_api.verify_webhook(b"{}", header)
    assert reason in str(exc.value)


def test_a_forged_signature_is_refused(monkeypatch):
    """Anybody can post to this URL. This is the only thing stopping them."""
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    body = b'{"type":"customer.subscription.updated"}'
    with pytest.raises(ValueError) as exc:
        stripe_api.verify_webhook(body, _signed(body, secret="whsec_attacker"))
    assert "does not match" in str(exc.value)


def test_a_captured_request_cannot_be_replayed_tomorrow(monkeypatch):
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    body = b"{}"
    old = time.time() - stripe_api.SIGNATURE_TOLERANCE_S - 60
    with pytest.raises(ValueError) as exc:
        stripe_api.verify_webhook(body, _signed(body, when=old))
    assert "tolerance" in str(exc.value)


def test_a_changed_body_breaks_the_signature(monkeypatch):
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_x")
    body = b'{"amount": 900}'
    header = _signed(body)
    with pytest.raises(ValueError):
        stripe_api.verify_webhook(b'{"amount": 1}', header)


def test_without_a_webhook_secret_nothing_is_accepted(monkeypatch):
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "")
    with pytest.raises(ValueError) as exc:
        stripe_api.verify_webhook(b"{}", "t=1,v1=x")
    assert "no webhook secret" in str(exc.value)


# ---------- attaching a payment to the right person ----------

def test_a_subscription_lands_on_the_account_that_checked_out(db):
    user = account(db, "sam")
    found = billing.user_for_event(db, {"client_reference_id": str(user.id)})
    assert found is user


def test_metadata_carries_it_when_the_reference_does_not(db):
    user = account(db, "sam")
    assert billing.user_for_event(
        db, {"metadata": {"delphi_account": str(user.id)}}) is user


def test_a_known_customer_id_is_enough(db):
    user = account(db, "sam", stripe_customer_id="cus_123")
    assert billing.user_for_event(db, {"customer": "cus_123"}) is user


def test_an_email_alone_claims_nothing(db):
    """The customer can change their email at Stripe. Matching on it would let
    somebody claim another account's subscription by editing a field."""
    account(db, "sam")
    assert billing.user_for_event(
        db, {"customer_email": "sam@example.com"}) is None


def test_applying_a_subscription_records_what_matters(db):
    user = account(db, "sam")
    ends = int(time.time()) + 30 * 86400
    billing.apply_subscription(db, user, {
        "id": "sub_1", "status": "active", "customer": "cus_9",
        "current_period_end": ends})
    assert user.subscription_id == "sub_1"
    assert user.stripe_customer_id == "cus_9"
    assert user.subscription_status == "active"
    assert user.paid_until and user.paid_until > utcnow()


# ---------- the gate itself ----------

def test_an_expired_account_is_refused_with_402(client, register, db, monkeypatch):
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    headers = register("expired")
    billing.save_settings(db, {"enabled": True, "trial_days": 14})
    user = db.scalar(main.select(User).where(User.username == "expired"))
    user.comped = False
    user.trial_ends_at = utcnow() - timedelta(days=1)
    db.commit()

    res = client.get("/api/feeds", headers=headers)
    assert res.status_code == 402
    body = res.json()
    assert body["code"] == "payment_required"
    assert body["billing"]["state"] == "expired"
    assert body["billing"]["price_label"]


def test_an_expired_account_can_still_pay_and_read_the_notice(client, register, db,
                                                              monkeypatch):
    """The exemptions exist so somebody who has run out is not locked away from
    the page that takes their money."""
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    headers = register("expired2")
    billing.save_settings(db, {"enabled": True})
    user = db.scalar(main.select(User).where(User.username == "expired2"))
    user.comped = False
    user.trial_ends_at = utcnow() - timedelta(days=1)
    db.commit()

    assert client.get("/api/billing/status", headers=headers).status_code == 200
    assert client.post("/api/session/hello", headers=headers).status_code == 200


def test_a_trial_account_uses_delphi_normally(client, register, db, monkeypatch):
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    headers = register("triallist")
    billing.save_settings(db, {"enabled": True, "trial_days": 14})
    user = db.scalar(main.select(User).where(User.username == "triallist"))
    user.comped = False
    user.trial_ends_at = utcnow() + timedelta(days=3)
    db.commit()
    assert client.get("/api/feeds", headers=headers).status_code == 200


def test_new_routes_are_behind_the_paywall_by_default():
    """The exemption list is short, and it is a list rather than a decorator so
    that forgetting to do anything leaves an endpoint gated. Forgetting the
    other way round gives the product away."""
    assert main._paywall_exempt("/api/billing/checkout")
    assert main._paywall_exempt("/api/session/hello")
    assert not main._paywall_exempt("/api/feeds")
    assert not main._paywall_exempt("/api/articles/search")
    assert not main._paywall_exempt("/api/admin/users")
    assert len(main._PAYWALL_EXEMPT) <= 6, (
        "every addition here is something an expired account can still do")


def test_an_operator_is_never_locked_out_of_their_own_console(client, register, db,
                                                              monkeypatch):
    """The console is where the price is set, invitations are made and billing
    is switched off. An operator on the wrong side of the paywall cannot reach
    any of it, and there is no other way in."""
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    headers = register("lockedop")
    user = db.scalar(main.select(User).where(User.username == "lockedop"))
    user.is_admin = True
    user.comped = False
    user.trial_ends_at = utcnow() - timedelta(days=30)
    db.commit()
    billing.save_settings(db, {"enabled": True})

    assert client.get("/api/admin/billing", headers=headers).status_code == 200
    assert client.get("/api/feeds", headers=headers).status_code == 200


def test_the_webhook_needs_no_account_but_proves_itself(client):
    """Public, because Stripe has no session — so an unsigned POST must fail."""
    res = client.post("/api/stripe/webhook", json={"type": "customer.subscription.updated"})
    assert res.status_code == 400
    assert "Signature" in res.json()["detail"]


def test_a_redeemed_invitation_restores_access_over_the_api(client, register, db,
                                                            monkeypatch):
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_x")
    admin_headers = register("adminop")
    admin = db.scalar(main.select(User).where(User.username == "adminop"))
    admin.is_admin = True
    db.commit()
    billing.save_settings(db, {"enabled": True})

    invite = client.post("/api/admin/invites", headers=admin_headers,
                         json={"note": "Sam", "max_uses": 1}).json()

    headers = register("guest")
    guest = db.scalar(main.select(User).where(User.username == "guest"))
    guest.comped = False
    guest.trial_ends_at = utcnow() - timedelta(days=1)
    db.commit()
    assert client.get("/api/feeds", headers=headers).status_code == 402

    res = client.post("/api/billing/redeem", headers=headers,
                      json={"code": invite["code"]})
    assert res.status_code == 200 and res.json()["state"] == "comped"
    assert client.get("/api/feeds", headers=headers).status_code == 200


def test_only_an_operator_may_price_or_invite(client, register):
    headers = register("nobody")
    assert client.get("/api/admin/billing", headers=headers).status_code == 403
    assert client.post("/api/admin/invites", headers=headers, json={}).status_code == 403
    assert client.put("/api/admin/billing", headers=headers,
                      json={"settings": {}}).status_code == 403


def test_the_console_never_hands_back_the_stripe_keys(client, register, db,
                                                      monkeypatch):
    """An operator console is a page in a browser. A secret it displays is a
    secret in a screenshot."""
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_live_SECRETVALUE")
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_SECRETVALUE")
    headers = register("adminop2")
    admin = db.scalar(main.select(User).where(User.username == "adminop2"))
    admin.is_admin = True
    db.commit()
    body = client.get("/api/admin/billing", headers=headers).text
    assert "SECRETVALUE" not in body
    assert json.loads(body)["stripe"] == {"configured": True,
                                          "webhook_configured": True,
                                          "live_mode": True}


# ---------- the accounts that were already here ----------

def test_existing_accounts_are_grandfathered_by_the_migration():
    """The deploy that adds billing must not take access from anybody using
    Delphi that morning — the operator included, who would otherwise be locked
    out of the console that turns it off."""
    import inspect
    src = inspect.getsource(main._ensure_schema)
    assert '"comped" in added' in src
    assert "UPDATE users SET comped = 1" in src
    # It has to hang off the column appearing, not run on every boot, or every
    # account would be comped forever.
    assert src.index("added = set()") < src.index('"comped" in added')
