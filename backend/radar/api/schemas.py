"""Pydantic v2 response/request models for the FastAPI layer.

All datetime/date fields are kept as `str` to mirror what the SQLite layer
stores (ISO strings) and to keep JSON simple for the Next.js frontend.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class EntitySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    entity_type: str
    subcategory: str | None = None
    jurisdiction: str | None = None
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)


class TopTopicStat(BaseModel):
    topic_id: str
    count: int
    dominant_stance: str | None = None


class EntityStats(BaseModel):
    activity_count: int
    recent_activities: list["ActivityWithEnrichment"] = Field(default_factory=list)
    top_topics: list[TopTopicStat] = Field(default_factory=list)


class EntityOut(BaseModel):
    entity: EntitySummary
    stats: EntityStats


# ---------------------------------------------------------------------------
# Activity / enrichment
# ---------------------------------------------------------------------------


class ActivityOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    entity_id: str
    entity_type: str
    activity_type: str
    occurred_at: str
    ingested_at: str
    source_url: str
    source_adapter: str
    title: str
    raw_text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    url_verified_at: str | None = None


class EnrichmentOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    activity_id: str
    summary: str
    topics: list[str] = Field(default_factory=list)
    mentioned_entities: list[str] = Field(default_factory=list)
    stance: str | None = None
    stance_quote: str | None = None
    materiality: dict[str, Any] = Field(default_factory=dict)
    enriched_at: str
    enricher_model: str


class ActivityWithEnrichment(BaseModel):
    activity: ActivityOut
    enrichment: EnrichmentOut | None = None
    source_entity: EntitySummary | None = None


# Resolve forward ref on EntityStats
EntityStats.model_rebuild()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class StructuredProfileOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topics_weighted: dict[str, float] = Field(default_factory=dict)
    watch_entities: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    activity_type_filters: list[str] | None = None
    recency_days: int = 30
    risk_tolerance: str = "actionable_only"
    notes: str | None = None


class ProfileIn(BaseModel):
    name: str = Field(min_length=1)
    nl_description: str = Field(min_length=1)
    structured_overrides: dict[str, Any] | None = None


class ProfileSummary(BaseModel):
    id: str
    name: str
    created_at: str | None = None


class ProfileOut(BaseModel):
    profile_id: str
    name: str
    nl_description: str
    structured: StructuredProfileOut


# ---------------------------------------------------------------------------
# Awareness / dashboard
# ---------------------------------------------------------------------------


class AwarenessBlock(BaseModel):
    relevance_score: float
    reasoning: str
    recommended_actions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class AwarenessItemOut(BaseModel):
    activity: ActivityOut
    enrichment: EnrichmentOut | None = None
    source_entity: EntitySummary | None = None
    awareness: AwarenessBlock


class DashboardOut(BaseModel):
    profile_id: str
    generated_at: str
    items: list[AwarenessItemOut] = Field(default_factory=list)


__all__ = [
    "EntitySummary",
    "EntityStats",
    "EntityOut",
    "TopTopicStat",
    "ActivityOut",
    "EnrichmentOut",
    "ActivityWithEnrichment",
    "StructuredProfileOut",
    "ProfileIn",
    "ProfileSummary",
    "ProfileOut",
    "AwarenessBlock",
    "AwarenessItemOut",
    "DashboardOut",
]
