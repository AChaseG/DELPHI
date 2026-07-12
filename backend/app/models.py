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

    articles = relationship("Article", back_populates="source", cascade="all, delete-orphan")


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
    image_url: Mapped[str] = mapped_column(String(1000), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
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


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
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
    name: Mapped[str] = mapped_column(String(200))
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)  # same shape as Feed.criteria
    active: Mapped[bool] = mapped_column(Boolean, default=True)
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
