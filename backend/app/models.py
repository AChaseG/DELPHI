"""ORM models: sources, articles, feeds, alerts, alert events."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    """Registered account. Feeds/alerts reference it as user_id "acct:<id>"."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), default="", index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # When the account last opened the app (None until the first load) —
    # drives the first-visit FAQ and the what's-new-while-away popup.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # JSON list of changelog-entry fingerprints already shown to this account,
    # so live sessions get a What's-new popup the moment an update ships.
    changelog_seen: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # JSON blob of display/notification preferences (theme, time format,
    # volume, reading language, …) so settings follow the account across
    # browsers, devices, and origin changes — not just this one localStorage.
    settings: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Operator account: may reach the /api/admin/* endpoints. This DB flag is
    # one of two ways in — accounts named in NEWS_ADMIN_USERS are admins even
    # with the flag off (the built-in, config-designated operator), and a
    # promotion by another admin sets this flag. No admin password is ever
    # hardcoded; see main.py:_is_admin.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Suspended by an admin: kept for history but blocked from signing in.
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Which generation of sessions this account is on. Every token carries the
    # number it was minted under; bumping this refuses all of them at once,
    # which is the only way to end a session that has already been issued.
    # Password reset and "sign out everywhere" bump it. See auth.py.
    token_version: Mapped[int] = mapped_column(Integer, default=0)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    rss_url: Mapped[str] = mapped_column(String(500), unique=True)
    homepage: Mapped[str] = mapped_column(String(500), default="")
    country: Mapped[str] = mapped_column(String(2), default="", index=True)  # ISO 3166-1 alpha-2, "" = global
    region: Mapped[str] = mapped_column(String(50), default="")
    language: Mapped[str] = mapped_column(String(8), default="en")
    scope: Mapped[str] = mapped_column(String(20), default="national")  # local | national | international
    categories: Mapped[list] = mapped_column(JSON, default=list)
    platform: Mapped[str] = mapped_column(String(20), default="news")  # news | reddit | mastodon | bluesky | youtube
    # Paywalled outlet: ingest its RSS headlines/summaries but never fetch the
    # (paywalled) article body, and offer readers an archive.ph link instead.
    paywall: Mapped[bool] = mapped_column(Boolean, default=False)
    tier: Mapped[int] = mapped_column(Integer, default=2)  # 1 = major wire/global, 2 = national, 3 = local/niche
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[str] = mapped_column(String(50), default="catalog")  # catalog | user | topic-tracker
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(200), default="")
    last_article_count: Mapped[int] = mapped_column(Integer, default=0)
    # Self-repair bookkeeping: failures in a row decide when to attempt a fix,
    # repaired_from preserves the original URL after an automatic switch.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    repaired_from: Mapped[str] = mapped_column(String(500), default="")
    last_repair_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Consecutive polls that yielded no new articles — used by the rolling
    # poller to lengthen the interval of quiet city feeds (adaptive backoff).
    idle_polls: Mapped[int] = mapped_column(Integer, default=0)
    # What the publisher last gave us to poll conditionally with. Sent back as
    # If-None-Match / If-Modified-Since so an unchanged feed answers 304 with no
    # body — which is what makes polling more often reasonable rather than rude.
    etag: Mapped[str] = mapped_column(String(200), default="")
    last_modified: Mapped[str] = mapped_column(String(80), default="")

    articles = relationship("Article", back_populates="source", cascade="all, delete-orphan")


class DiscoveredDomain(Base):
    """Outcome of probing a publisher domain seen in Google News coverage,
    so auto-discovery never hammers the same outlet twice."""
    __tablename__ = "discovered_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="no-feed")  # added | no-feed
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """A cluster of articles covering the same real-world happening."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")  # headline of the first article seen
    cluster_tokens: Mapped[str] = mapped_column(String(400), default="")
    importance: Mapped[int] = mapped_column(Integer, default=30)
    article_count: Mapped[int] = mapped_column(Integer, default=1)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("url", name="uq_article_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    guid: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")  # fetched article body text
    # When a body fetch was last attempted, whether or not it produced anything.
    # A busy tick brings more articles than one cycle can fetch bodies for, and
    # the rest are picked up later; without this, an article whose page is
    # permanently unreachable would be retried on every tick forever, and the
    # backlog would never advance past it.
    content_tried_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    image_url: Mapped[str] = mapped_column(String(1000), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    country: Mapped[str] = mapped_column(String(2), default="", index=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    # places: [{"name": str, "country": iso2, "lat": float, "lon": float}]
    places: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[int] = mapped_column(Integer, default=30, index=True)
    cluster_tokens: Mapped[str] = mapped_column(String(400), default="")  # for cross-source corroboration
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True, index=True)

    source = relationship("Source", back_populates="articles")
    event = relationship("Event")


class ViewedEvent(Base):
    """Per-user record that an event was opened in Event Focus."""
    __tablename__ = "viewed_events"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_viewed"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ViewedArticle(Base):
    """Per-user record that an article with no event was read.

    Nearly every article belongs to an event, and reading one dims the whole
    story wherever it appears. An article that never got an event — a
    clustering pass that failed leaves a batch of them — had nowhere for that
    fact to live, so it stayed bright no matter how many times it was opened.
    """
    __tablename__ = "viewed_articles"
    __table_args__ = (UniqueConstraint("user_id", "article_id", name="uq_viewed_article"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Translation(Base):
    """Cached machine translation of an article into one target language."""
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("article_id", "lang", name="uq_translation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Pantheon(Base):
    """An organization: members share feeds, alerts, and a common board."""
    __tablename__ = "pantheons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    visibility: Mapped[str] = mapped_column(String(10), default="private")  # private | public
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Access limits, editable by owner/admins:
    #   who_can_invite: "members" | "admins"    who_can_share: "members" | "admins"
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PantheonMember(Base):
    __tablename__ = "pantheon_members"
    __table_args__ = (UniqueConstraint("pantheon_id", "user_id", name="uq_pantheon_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pantheon_id: Mapped[int] = mapped_column(ForeignKey("pantheons.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(10), default="member")  # owner | admin | member
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PantheonInvite(Base):
    __tablename__ = "pantheon_invites"
    __table_args__ = (UniqueConstraint("pantheon_id", "user_id", name="uq_pantheon_invite"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pantheon_id: Mapped[int] = mapped_column(ForeignKey("pantheons.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # invitee
    invited_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FavoriteLocation(Base):
    """A place the user cares about, with a radius around it.

    Any article tagged inside the radius is flagged wherever it appears, and a
    dedicated feed is created alongside so the location has a board of its own.
    Shared into a Pantheon it flags articles for every member.
    """
    __tablename__ = "favorite_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # Set when shared with a Pantheon; shared_by names the sharer for attribution.
    pantheon_id: Mapped[int | None] = mapped_column(ForeignKey("pantheons.id"),
                                                    nullable=True, index=True, default=None)
    shared_by: Mapped[str] = mapped_column(String(32), default="")
    # What the reader called it. Theirs to choose — "Dad's house", "the office"
    # — so it is a label and nothing more.
    name: Mapped[str] = mapped_column(String(120))
    # What the place is actually called, from whatever was picked in the search
    # box. Kept apart from `name` precisely because `name` is a nickname: this
    # is the string worth looking for in a headline and worth asking a news
    # search for, and "Dad's house" is neither. Empty when the point came from
    # a click on the map, which is a real case with no name to have.
    place_name: Mapped[str] = mapped_column(String(120), default="")
    # ISO-2, when the pick knew it. Tells Reading in England from Reading in
    # Pennsylvania, and picks the right Google News edition.
    country: Mapped[str] = mapped_column(String(2), default="")
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float, default=25.0)
    # Colour used for its pin and the badge on flagged articles.
    color: Mapped[str] = mapped_column(String(16), default="gold")
    # The feed created for this location, so deleting the location can clean up.
    feed_id: Mapped[int | None] = mapped_column(ForeignKey("feeds.id"),
                                                nullable=True, default=None)
    # The news-search source that gathers coverage of this place. Held by id so
    # a rename can move it and a deletion can take it away — a source nothing
    # points at any more still gets polled forever otherwise.
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"),
                                                  nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    # Set when this feed lives on a Pantheon's shared board instead of a
    # personal one; shared_by holds the sharer's username for attribution.
    pantheon_id: Mapped[int | None] = mapped_column(ForeignKey("pantheons.id"),
                                                    nullable=True, index=True, default=None)
    shared_by: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(200))
    # criteria keys (all optional): countries[], scopes[], categories[], languages[],
    # source_ids[], keywords[], exclude_keywords[], query (boolean string),
    # min_importance (int), geo (GeoJSON Polygon/MultiPolygon or Circle), hours (recency window)
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[str] = mapped_column(String(20), default="newest")  # newest | importance
    group_events: Mapped[bool] = mapped_column(Boolean, default=False)  # cluster into events
    position: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=1)  # dashboard columns spanned (1-2)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    pantheon_id: Mapped[int | None] = mapped_column(ForeignKey("pantheons.id"),
                                                    nullable=True, index=True, default=None)
    shared_by: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(200))
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)  # same shape as Feed.criteria
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Out-of-app delivery so time-critical hits reach you with the tab closed.
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False)  # email the owner
    webhook_url: Mapped[str] = mapped_column(String(500), default="")   # POST hits here
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events = relationship("AlertEvent", back_populates="alert", cascade="all, delete-orphan")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    seen: Mapped[bool] = mapped_column(Boolean, default=False)

    alert = relationship("Alert", back_populates="events")
    article = relationship("Article")
