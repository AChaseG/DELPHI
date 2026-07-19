"""Out-of-app alert delivery: config round-trips, webhook validation, and the
email/webhook fan-out from a batch of hits (batched per alert, non-fatal)."""
import asyncio

from backend.app import ingest, mailer
from backend.app.models import Alert, User, utcnow


def test_alert_delivery_config_round_trips(client, register):
    hdr = register("alice")
    aid = client.post("/api/alerts", headers=hdr, json={
        "name": "Quake", "criteria": {"keywords": ["earthquake"], "auto_coverage": False},
        "notify_email": True, "webhook_url": "https://hooks.example.com/x"}).json()["id"]
    row = next(a for a in client.get("/api/alerts", headers=hdr).json() if a["id"] == aid)
    assert row["notify_email"] is True and row["webhook_url"] == "https://hooks.example.com/x"


def test_webhook_url_must_be_http(client, register):
    hdr = register("bob")
    r = client.post("/api/alerts", headers=hdr, json={
        "name": "Bad", "criteria": {"auto_coverage": False},
        "webhook_url": "file:///etc/passwd"})
    assert r.status_code == 422


def test_deliver_alerts_fans_out_email_and_webhook(db, monkeypatch):
    user = User(username="carol", email="carol@example.com", password_hash="x")
    db.add(user); db.flush()

    emails, posts = [], []
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "send_alert_digest",
                        lambda to, name, hits: emails.append((to, name, len(hits))) or True)

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json): posts.append((url, json))
    monkeypatch.setattr(ingest.httpx, "AsyncClient", lambda *a, **k: FakeClient())

    hits = [
        {"alert_id": 1, "alert_name": "Quake", "user_id": f"acct:{user.id}",
         "notify_email": True, "webhook_url": "https://hooks.test/x",
         "title": "Big one", "url": "https://n/1", "importance": 90, "country": "JP", "article_id": 1},
        {"alert_id": 1, "alert_name": "Quake", "user_id": f"acct:{user.id}",
         "notify_email": True, "webhook_url": "https://hooks.test/x",
         "title": "Aftershock", "url": "https://n/2", "importance": 70, "country": "JP", "article_id": 2},
        {"alert_id": 2, "alert_name": "Quiet", "user_id": f"acct:{user.id}",
         "notify_email": False, "webhook_url": "", "title": "x", "url": "https://n/3",
         "importance": 10, "country": "", "article_id": 3},
    ]
    delivered = asyncio.run(ingest.deliver_alerts(db, hits))

    # alert 1 emails once (both hits batched) and webhooks once; alert 2 opts out
    assert emails == [("carol@example.com", "Quake", 2)]
    assert len(posts) == 1 and posts[0][0] == "https://hooks.test/x"
    assert posts[0][1]["count"] == 2 and len(posts[0][1]["hits"]) == 2
    assert delivered == 2


def test_delivery_skipped_when_no_channel_configured(db):
    hits = [{"alert_id": 1, "alert_name": "x", "user_id": "acct:1",
             "notify_email": False, "webhook_url": "", "title": "t", "url": "u",
             "importance": 1, "country": "", "article_id": 1}]
    assert asyncio.run(ingest.deliver_alerts(db, hits)) == 0
