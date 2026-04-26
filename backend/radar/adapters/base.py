"""Adapter protocol definitions.

Two adapter shapes per BUILD_PLAN §7:
  - StructuredAdapter: hits a clean API (congress.gov, federalregister.gov, etc.)
  - ResearchAdapter: wraps Claude Opus 4.7 + web search for entities without APIs

Both produce normalized Activity dicts ready for storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable


@dataclass
class EntityRef:
    """A lightweight reference to a row in the `entity` table.

    Adapters get this rather than a full ORM object to keep them stateless.
    """
    id: str
    name: str
    entity_type: str
    subcategory: str | None = None
    jurisdiction: str | None = None
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActivityStub:
    """Lightweight handle returned by `discover()`. Cheap to enumerate.

    `fetch()` turns this into a full Activity. Splitting discovery from fetch
    lets us decide what to ingest before paying the cost (HTTP, LLM tokens).
    """
    source_url: str
    title: str
    occurred_at: date | str
    activity_type: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Activity:
    """Normalized activity record ready for storage in the `activity` table.

    `id` is computed by the storage layer (hash of source_url + entity_id), so
    adapters do not set it. `ingested_at` and `url_verified_at` are also stamped
    by the storage layer.
    """
    entity_id: str
    entity_type: str
    activity_type: str
    occurred_at: date | str
    source_url: str
    source_adapter: str
    title: str
    raw_text: str
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class StructuredAdapter(Protocol):
    """Adapter against a known API. See §7.1."""

    name: str
    handles_entity_types: list[str]

    def discover(self, entity: EntityRef, since: date) -> list[ActivityStub]: ...

    def fetch(self, entity: EntityRef, stub: ActivityStub) -> Activity: ...


@runtime_checkable
class ResearchAdapter(Protocol):
    """Adapter using Claude + web search. See §7.2.

    Single combined call rather than discover/fetch — the LLM does both passes
    in one tool-using turn. Output goes through URL verification (§7.4) before
    storage; that step lives in the ingestion pipeline, not the adapter.
    """

    name: str
    handles_entity_types: list[str]

    def discover_and_fetch(self, entity: EntityRef, since: date) -> list[Activity]: ...
