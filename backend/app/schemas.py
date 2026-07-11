"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Criteria(BaseModel):
    countries: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)  # news/reddit/mastodon/bluesky/youtube
    categories: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    query: str = ""                                # legacy single boolean string
    queries: list[str] = Field(default_factory=list)  # boolean strings, OR'd together
    min_importance: int = 0
    hours: float | None = None
    date_from: str = ""   # ISO date (YYYY-MM-DD), inclusive
    date_to: str = ""     # ISO date (YYYY-MM-DD), inclusive
    # Hide events that haven't been updated within the user's staleness
    # threshold (set in Settings). Display-only: clustering still attaches
    # new similar articles to hidden events, which then resurface.
    hide_stale: bool = False
    geo: dict | None = None
    # When true, saving the feed/alert also ensures a Google News tracker
    # source exists for the query/keywords, so worldwide press coverage of
    # the topic is ingested rather than only what the catalog publishes.
    auto_coverage: bool = False


class FeedIn(BaseModel):
    name: str
    criteria: Criteria = Field(default_factory=Criteria)
    sort: str = "newest"
    position: int = 0
    width: int = 1
    group_events: bool = False


class FeedOut(FeedIn):
    id: int
    user_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class AlertIn(BaseModel):
    name: str
    criteria: Criteria = Field(default_factory=Criteria)
    active: bool = True


class AlertOut(AlertIn):
    id: int
    user_id: str
    created_at: datetime
    last_triggered_at: datetime | None = None

    class Config:
        from_attributes = True


class SourceIn(BaseModel):
    name: str
    rss_url: str
    homepage: str = ""
    country: str = ""
    region: str = ""
    language: str = "en"
    scope: str = "national"
    categories: list[str] = Field(default_factory=list)
    platform: str = "news"
    tier: int = 2
    enabled: bool = True


class SocialTrackerIn(BaseModel):
    """Creates social-platform search/tag feeds for a topic."""
    query: str


class TopicTrackerIn(BaseModel):
    """Creates a virtual source from a Google News search query."""
    query: str
    language: str = "en"
    country: str = "US"


class QueryValidateIn(BaseModel):
    query: str
