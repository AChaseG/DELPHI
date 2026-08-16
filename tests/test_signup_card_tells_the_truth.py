"""What the sign-in card is allowed to promise.

The Create-account form carries an invitation-code field described as meaning
"if an operator gave you one, it means you never pay". On an instance that
charges nobody — which is the default, and every self-hosted copy — that is a
promise about a bill that does not exist, and the field is a puzzle rather than
an offer. The markup had an id on the row (`reg-invite-row`) as if something
were meant to hide it, and nothing ever did.

Nothing could, either: the card runs before anybody has an account, and every
route that knows what an instance charges is behind the session token. So
there is one public endpoint that answers just that, and the card reads it.

The same call earns its place twice over: where billing *is* on, the card can
say what it costs before you sign up rather than after, which is the difference
between a trial and a surprise.
"""
import pathlib
import re

from backend.app import billing

APP = (pathlib.Path(__file__).resolve().parent.parent / "frontend" / "js"
       / "app.js").read_text(encoding="utf-8")
HTML = (pathlib.Path(__file__).resolve().parent.parent / "frontend"
        / "index.html").read_text(encoding="utf-8")


def _billing_on(client, db, headers, **over):
    billing.save_settings(db, {"enabled": True, "price_cents": 900,
                               "currency": "usd", "interval": "month",
                               "trial_days": 14, **over})


# ---------- the endpoint ----------

def test_it_answers_without_an_account(client):
    """It is the sign-in card asking, so there is nobody to be."""
    r = client.get("/api/auth/signup-info")
    assert r.status_code == 200, r.text


def test_an_instance_that_charges_nobody_says_so(client):
    body = client.get("/api/auth/signup-info").json()
    assert body["billing_enabled"] is False
    assert body["price_label"] == ""
    assert body["trial_days"] == 0


def test_it_says_the_price_when_there_is_one(client, db, monkeypatch):
    monkeypatch.setattr(billing.stripe_api, "enabled", lambda: True)
    _billing_on(client, db, None)

    body = client.get("/api/auth/signup-info").json()
    assert body["billing_enabled"] is True
    assert body["price_label"] == "$9.00 a month"
    assert body["trial_days"] == 14


def test_it_gives_away_nothing_else(client, db, monkeypatch):
    """Public, so what it carries is what anyone may have. A price is on the
    paywall and on Stripe's own checkout page; nothing about the instance's
    accounts or its keys belongs here."""
    monkeypatch.setattr(billing.stripe_api, "enabled", lambda: True)
    _billing_on(client, db, None)

    body = client.get("/api/auth/signup-info").json()
    assert set(body) == {"billing_enabled", "price_label", "trial_days"}


def test_it_is_reachable_with_the_paywall_up(client, db, monkeypatch):
    """It has to work in exactly the state that makes it matter. /api/auth/* is
    public, but that is a prefix list and this route is new to it."""
    monkeypatch.setattr(billing.stripe_api, "enabled", lambda: True)
    _billing_on(client, db, None, trial_days=0)

    assert client.get("/api/auth/signup-info").status_code == 200


# ---------- and the card uses it ----------

def test_the_invitation_row_goes_away_when_nobody_pays():
    src = APP[APP.index("API.signupInfo()"):]
    src = src[:src.index("on(\"gate-to-register\"")]
    assert 'el("reg-invite-row")' in src
    assert "info.billing_enabled" in src


def test_a_code_in_the_link_keeps_its_field_regardless():
    """Following /?invite=CODE and finding no box to put it in would be worse
    than the confusion this removes — and an operator sending one has clearly
    decided the code means something here."""
    src = APP[APP.index("API.signupInfo()"):]
    src = src[:src.index("on(\"gate-to-register\"")]
    assert "!invited" in src


def test_the_card_says_what_it_costs_before_you_sign_up():
    assert 'id="gate-terms"' in HTML
    src = APP[APP.index("API.signupInfo()"):]
    src = src[:src.index("on(\"gate-to-register\"")]
    assert "info.price_label" in src
    assert "info.trial_days" in src


def test_one_day_is_not_one_days():
    src = APP[APP.index("API.signupInfo()"):]
    src = src[:src.index("on(\"gate-to-register\"")]
    assert 'info.trial_days === 1 ? "" : "s"' in src


def test_a_failed_call_never_blocks_the_gate():
    """This is the only way into the app. A promise about the price is not
    worth a sign-in card that does not appear."""
    src = APP[APP.index("API.signupInfo()"):]
    src = src[:src.index("on(\"gate-to-register\"")]
    assert re.search(r"\.catch\(\(\)\s*=>", src), "an unhandled rejection here is fatal"
    assert "await" not in src.split(".catch")[0], (
        "waiting on it would hold the card behind a network call")
