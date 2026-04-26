"""Ingestion orchestrator (BUILD_PLAN §7).

Routes each entity to the appropriate adapter(s), runs URL verification on
research-adapter outputs (§7.4), and persists activities through the storage
helpers. One entity can be covered by multiple adapters; the storage layer's
stable id (`hash(source_url|entity_id)`) means a duplicate row from a second
adapter naturally collapses into an UPDATE.

Per-activity errors NEVER abort the batch — every Activity is processed inside
its own try/except, with the failure reason recorded on the IngestResult.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from radar.adapters.base import (
    Activity,
    ActivityStub,
    EntityRef,
    ResearchAdapter,
    StructuredAdapter,
)
from radar.adapters.research.url_verify import verify_activity
from radar.db.storage import list_entities_with_aliases, upsert_activity

log = logging.getLogger(__name__)


# Defensive cap: no single entity should ever produce more than this many rows
# in a single ingest run. If we approach it, something is wrong upstream.
_MAX_ACTIVITIES_PER_ENTITY = 100


@dataclass
class IngestResult:
    entity_id: str
    adapter: str  # comma-joined adapter names if multiple ran for this entity
    discovered: int = 0
    verified: int = 0  # URL-verified (==discovered for structured)
    persisted_new: int = 0
    persisted_updated: int = 0
    failures: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_excerpt(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def ingest_entity(
    conn: sqlite3.Connection,
    entity: EntityRef,
    *,
    since: date,
    research_adapter: ResearchAdapter | None = None,
    structured_adapters: list[StructuredAdapter] | None = None,
    dry_run: bool = False,
) -> IngestResult:
    """Run all matching adapters for `entity` and persist results.

    Routing:
      - For each StructuredAdapter whose `handles_entity_types` includes
        `entity.entity_type`, call `discover()` then `fetch()` per stub. No URL
        verification (structured sources are trusted).
      - If `research_adapter` is supplied and its `handles_entity_types`
        includes `entity.entity_type`, call `discover_and_fetch()` and run
        `verify_activity()` on each result. Only verified items are persisted
        (and stamped with `url_verified_at`).
    """
    adapters_run: list[str] = []
    discovered = 0
    verified = 0
    persisted_new = 0
    persisted_updated = 0
    failures: list[str] = []

    persisted_total = 0

    # ---- Structured adapters ------------------------------------------------
    for adapter in structured_adapters or []:
        if entity.entity_type not in getattr(adapter, "handles_entity_types", []):
            continue
        adapters_run.append(adapter.name)

        try:
            stubs: list[ActivityStub] = adapter.discover(entity, since)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"{adapter.name}.discover failed: {type(exc).__name__}: {_safe_excerpt(str(exc))}"
            )
            log.warning(
                "[ingest] %s: %s.discover failed: %s",
                entity.id,
                adapter.name,
                exc,
            )
            continue

        discovered += len(stubs)
        # Structured sources don't need URL verification; count discovered as verified.
        verified += len(stubs)

        for stub in stubs:
            if persisted_total >= _MAX_ACTIVITIES_PER_ENTITY:
                failures.append(
                    f"{adapter.name}: hit per-entity cap ({_MAX_ACTIVITIES_PER_ENTITY}); remaining stubs dropped"
                )
                break
            try:
                act = adapter.fetch(entity, stub)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"{adapter.name}.fetch failed for {stub.source_url!r}: "
                    f"{type(exc).__name__}: {_safe_excerpt(str(exc))}"
                )
                continue
            if act is None:
                # Adapter chose to drop the stub (e.g. payload validation failure
                # already logged). Not counted as a hard failure here.
                continue
            ok, reason = _persist(conn, act, url_verified_at=None, dry_run=dry_run)
            if ok is None:
                failures.append(f"{adapter.name}: {reason}")
            elif ok:
                persisted_new += 1
                persisted_total += 1
            else:
                persisted_updated += 1
                persisted_total += 1

    # ---- Research adapter ---------------------------------------------------
    if research_adapter is not None and entity.entity_type in getattr(
        research_adapter, "handles_entity_types", []
    ):
        adapters_run.append(research_adapter.name)
        try:
            activities: list[Activity] = research_adapter.discover_and_fetch(entity, since)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"{research_adapter.name}.discover_and_fetch failed: "
                f"{type(exc).__name__}: {_safe_excerpt(str(exc))}"
            )
            activities = []

        discovered += len(activities)

        for act in activities:
            if persisted_total >= _MAX_ACTIVITIES_PER_ENTITY:
                failures.append(
                    f"{research_adapter.name}: hit per-entity cap "
                    f"({_MAX_ACTIVITIES_PER_ENTITY}); remaining activities dropped"
                )
                break

            try:
                passed, reason = verify_activity(act)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"{research_adapter.name}: url_verify raised "
                    f"{type(exc).__name__}: {_safe_excerpt(str(exc))} for {act.source_url!r}"
                )
                continue

            if not passed:
                failures.append(
                    f"{research_adapter.name}: url_verify={reason} for {act.source_url!r}"
                )
                continue

            verified += 1
            verified_at = _now()
            ok, persist_reason = _persist(
                conn, act, url_verified_at=verified_at, dry_run=dry_run
            )
            if ok is None:
                failures.append(f"{research_adapter.name}: {persist_reason}")
            elif ok:
                persisted_new += 1
                persisted_total += 1
            else:
                persisted_updated += 1
                persisted_total += 1

    return IngestResult(
        entity_id=entity.id,
        adapter=",".join(adapters_run) if adapters_run else "(none)",
        discovered=discovered,
        verified=verified,
        persisted_new=persisted_new,
        persisted_updated=persisted_updated,
        failures=failures,
    )


def _persist(
    conn: sqlite3.Connection,
    act: Activity,
    *,
    url_verified_at: datetime | None,
    dry_run: bool,
) -> tuple[bool | None, str]:
    """Single-activity persistence wrapper.

    Returns:
        (True, "ok")          on insert
        (False, "ok")         on update
        (None, <reason>)      on validation/persistence failure
    """
    if dry_run:
        # Still validate, just don't write. Treat "would-insert" as new.
        try:
            from radar.db.payload_schemas import validate_payload

            validate_payload(act.activity_type, act.payload or {})
        except Exception as exc:  # noqa: BLE001
            return None, f"validate_payload: {_safe_excerpt(str(exc))}"
        return True, "ok"

    try:
        _, was_new = upsert_activity(conn, act, url_verified_at=url_verified_at)
    except ValueError as exc:
        return None, f"upsert_activity: {_safe_excerpt(str(exc))}"
    except Exception as exc:  # noqa: BLE001
        return None, f"upsert_activity raised {type(exc).__name__}: {_safe_excerpt(str(exc))}"
    return (True if was_new else False), "ok"


def ingest_all(
    conn: sqlite3.Connection,
    *,
    since: date,
    research_adapter: ResearchAdapter | None = None,
    structured_adapters: list[StructuredAdapter] | None = None,
    only_entity_types: set[str] | None = None,
    only_entity_ids: set[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[IngestResult]:
    """Run `ingest_entity` for every entity in the DB matching the filters.

    `only_entity_ids` takes precedence over `only_entity_types` — if a caller
    passes an explicit id set, the type filter is ignored for those rows.
    """
    entities = list_entities_with_aliases(conn)

    if only_entity_ids:
        entities = [e for e in entities if e.id in only_entity_ids]
    elif only_entity_types:
        entities = [e for e in entities if e.entity_type in only_entity_types]

    if limit is not None:
        entities = entities[:limit]

    results: list[IngestResult] = []
    for entity in entities:
        log.info("[ingest] entity=%s type=%s", entity.id, entity.entity_type)
        result = ingest_entity(
            conn,
            entity,
            since=since,
            research_adapter=research_adapter,
            structured_adapters=structured_adapters,
            dry_run=dry_run,
        )
        results.append(result)
        if not dry_run:
            conn.commit()
    return results


__all__ = [
    "IngestResult",
    "ingest_entity",
    "ingest_all",
]
