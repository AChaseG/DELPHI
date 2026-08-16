"""ORM models: sources, articles, feeds, alerts, alert events."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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
    # How many devices this account may be *in use on* at once. NULL means
    # "whatever the server default is", so raising or lowering the default
    # moves everyone who has not been given a number of their own.
    device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # ---- access: paid, invited, or on trial (see billing.py) ----
    #
    # Four facts, not one state. State is derived from them (billing.access_of)
    # because they change independently and from different places — a webhook
    # moves the subscription, an operator moves `comped`, the clock moves the
    # trial — and a single column would have each of those writers guessing
    # what the others meant.
    #
    # Free forever, granted by the operator: an invited account, or one that
    # predates billing being switched on. Outranks everything else, and is the
    # only state that never expires.
    comped: Mapped[bool] = mapped_column(Boolean, default=False)
    comped_note: Mapped[str] = mapped_column(String(200), default="")
    # Which invitation let them in, when one did. Kept so an operator can see
    # what a code actually did, and revoke the people it let in.
    invited_by_code: Mapped[str] = mapped_column(String(40), default="")
    # End of the free trial. NULL on an account that never had one — comped, or
    # created while billing was switched off entirely.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                           default=None)
    stripe_customer_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    subscription_id: Mapped[str] = mapped_column(String(64), default="")
    # Stripe's own word for it: active, trialing, past_due, canceled, unpaid.
    subscription_status: Mapped[str] = mapped_column(String(24), default="")
    # Paid up to here — Stripe's current_period_end. Access is granted against
    # this date rather than against the status, so a webhook that never arrives
    # cannot cut off somebody who has paid: they keep what they bought until it
    # runs out, and a card that stops working ends access at the period's end
    # rather than the moment Stripe first mentions it.
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                        default=None)


class Invite(Base):
    """An operator's invitation to use Delphi without paying.

    A code, not an emailed link to one address: the operator hands it out
    however they like — a link, a message, read aloud — and it comps whoever
    redeems it. Limits are the operator's: how many may use it, and until when.
    """
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    note: Mapped[str] = mapped_column(String(200), default="")   # who it is for
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 0 = as many as anyone likes. A code for one person is max_uses=1.
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                        default=None)
    # Revoked rather than deleted: the accounts it let in name it, and an
    # operator asking "where did this person come from" deserves an answer.
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class InstanceSetting(Base):
    """Operator-wide configuration, as JSON under a name.

    Distinct from `User.settings`, which is one person's preferences. This is
    the instance's own — what it charges, how long a trial runs — and it lives
    in the database rather than in the environment so the operator can change
    it from the console without a deploy.
    """
    __tablename__ = "instance_settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                  onupdate=utcnow)


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
    # Which newsroom's copy this masthead runs, when it is not its own.
    #
    # Publishing groups syndicate one newsroom across many local titles: seven
    # Vocento papers in Spain, four Glacier Media titles in British Columbia,
    # four Mediahuis mastheads in the Netherlands. Each has its own domain and
    # its own URL, so URL dedup does not see them, and each one counted as an
    # independent outlet corroborating the story — which made one newsroom look
    # like a consensus. Sources sharing a group corroborate once between them.
    #
    # Empty means "its own newsroom", which is the honest default: a group is
    # only recorded once the same copy has been seen under both mastheads
    # several times. Learned by syndication.detect, not configured.
    #
    # Deliberately not indexed. The additive migration that adds this column to
    # an existing database uses ALTER TABLE, which cannot also create an index,
    # so `index=True` would be true of a fresh database and quietly false of the
    # one actually running — a declaration describing only some of its
    # deployments. It buys nothing here either: the one query over it reads
    # every source row, and there are thousands of those, not millions.
    syndicate: Mapped[str] = mapped_column(String(40), default="")

    articles = relationship("Article", back_populates="source", cascade="all, delete-orphan")


class DiscoveredDomain(Base):
    """Outcome of probing a publisher domain seen in Google News coverage,
    so auto-discovery never hammers the same outlet twice."""
    __tablename__ = "discovered_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="no-feed")  # added | no-feed | seen
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # How many separate cycles this domain has turned up as a publisher.
    # Adoption waits for a second sighting: a real outlet keeps appearing in
    # coverage, while a ticketing calendar or a funeral home surfaces once and
    # never again. Counting is what tells them apart without a list of every
    # kind of website that is not a newspaper.
    sightings: Mapped[int] = mapped_column(Integer, default=0)


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


class Hazard(Base):
    """One live hazard — a wildfire, a reading of the air — on the Typhon layer.

    Deliberately not an Article, and the reason is mutability rather than
    taste. The federal wildfire feed re-reports the same incident every few
    minutes with new acreage and new containment; an air-quality reading is a
    number that changes all day. Delphi's ingest is append-only, dedupes on
    `Article.url`, and evaluates alerts only over rows inserted in that tick —
    so an *updated* article can never fire an alert, and "the fire near your
    town grew from 200 to 40,000 acres" is exactly the event this exists to
    deliver. A row that is meant to be rewritten needs a table that expects it.

    Keeping them out of the article corpus also keeps them out of the things
    tuned for prose: full-text search, importance scoring, clustering into
    events, translation, and Home's curated columns. A hazard has no headline
    and no body. That boundary is the design; if it erodes, Delphi gets worse
    at the thing it is good at.

    The upside of a table of its own: these carry real coordinates from the
    provider, so they are never subject to the gazetteer's limit of matching
    place names it happens to know. A hazard geofences perfectly, always.
    """
    __tablename__ = "hazards"
    __table_args__ = (
        # What "the same hazard as last poll" means. Every provider hands back
        # a stable id of its own — an IRWIN id for a fire, a reporting area for
        # air — and the upsert turns on finding this row again.
        UniqueConstraint("provider", "external_id", name="uq_hazard_external"),
        # The map asks "what is inside this rectangle" and nothing else.
        Index("ix_hazards_lat_lon", "lat", "lon"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What sort of thing this is; drives which map layer draws it and how
    # `severity` is bucketed for alerting.
    kind: Mapped[str] = mapped_column(String(24), index=True)
    # Who said so. A column rather than an assumption, so a second wildfire
    # provider for another continent slots in behind the same shape.
    provider: Mapped[str] = mapped_column(String(24), index=True)
    external_id: Mapped[str] = mapped_column(String(120))

    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    country: Mapped[str] = mapped_column(String(2), default="")

    # 0-100, normalised across kinds so one scale colours the map. What counts
    # as a step *for alerting* is bucketed per kind instead (see hazards.py):
    # air quality has the EPA's six categories, and a fire has size bands.
    # Alerting on the raw number would notify on 51 -> 52.
    severity: Mapped[int] = mapped_column(Integer, default=0)
    # The provider's own fields, kept whole. The popup reads acreage,
    # containment and cause straight out of here, so a provider offering one
    # more useful number is not a migration.
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                        default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Stamped on every row the provider still lists. Retention is "the provider
    # stopped mentioning it a week ago", which is how a contained fire leaves
    # without anybody having to be told it ended.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                   index=True)


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


class Device(Base):
    """One browser profile on one machine, and when it was last used.

    Sessions here are stateless — a signed token carrying an account id, a
    version and an expiry — which is cheap and revocable but records nothing
    about where it is being used. There was no way to answer "how many places
    is this account open in right now", because nothing was ever written down.

    `device_key` is minted by the browser and kept in its local storage, so it
    survives tabs, reloads and restarts and distinguishes a laptop from a phone
    — which counting tokens or connections cannot do, since one laptop with
    four tabs is still one device. It identifies, it does not authenticate:
    the token does that, and this row is only ever read for an account the
    token already proved. Clearing site data or opening a private window looks
    like a new device, which is the honest limit of identifying a browser
    without fingerprinting one.
    """
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_key: Mapped[str] = mapped_column(String(64), index=True)
    # Parsed from the user agent once, on first sight: "desktop" | "mobile" |
    # "tablet" | "unknown", plus the platform and browser behind it.
    kind: Mapped[str] = mapped_column(String(16), default="unknown")
    platform: Mapped[str] = mapped_column(String(32), default="")
    browser: Mapped[str] = mapped_column(String(32), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # The whole point of the table. "Active" is this within a recent window,
    # not "holds a valid token" — a token lives for thirty days.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    __table_args__ = (UniqueConstraint("user_id", "device_key", name="uq_device_user_key"),)
