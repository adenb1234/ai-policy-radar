"""Storage helpers for the `activity` table (BUILD_PLAN §6.2).

Bridges adapter outputs (Activity dataclass) and the SQLite `activity` table.
Activity.id is computed here (not by adapters) per §6.2: a stable hash of
`source_url|entity_id` so re-ingesting the same source URL for the same entity
yields the same id and the upsert becomes idempotent.

Payload validation is enforced at the storage boundary — every row is run
through `validate_payload(activity_type, payload)` before it touches the DB.
On failure we raise ValueError; the orchestrator catches per-activity and
records the reason without aborting the batch.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError

from radar.adapters.base import Activity, EntityRef
from radar.db.payload_schemas import validate_payload


# ---------------------------------------------------------------------------
# id computation
# ---------------------------------------------------------------------------


def compute_activity_id(source_url: str, entity_id: str) -> str:
    """SHA-256 hex digest of f'{source_url}|{entity_id}', truncated to 16 chars.

    Stable across runs — re-ingesting the same activity yields the same id, so
    `INSERT ... ON CONFLICT(id) DO UPDATE` becomes the idempotency primitive.
    """
    payload = f"{source_url}|{entity_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_occurred_at(value: date | str | Any) -> str:
    """Normalize occurred_at to an ISO date string ('YYYY-MM-DD').

    Accepts `date`, `datetime`, or string. Strings are returned as-is if they
    already look like ISO dates; otherwise we attempt a best-effort parse and
    fall back to the raw string (the schema enforces NOT NULL TEXT, not a
    format).
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    # Cheap pass-through if it already parses as an ISO date prefix.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        # Try YYYY-MM-DD strict.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return s


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload or {}, default=_json_default, sort_keys=True)


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def upsert_activity(
    conn: sqlite3.Connection,
    activity: Activity,
    *,
    url_verified_at: datetime | str | None = None,
) -> tuple[str, bool]:
    """Validate the activity's payload, then upsert into `activity`.

    Returns (activity_id, was_new). `was_new` is True on the INSERT path,
    False if the row already existed (UPDATE).

    Raises:
        ValueError: payload fails `validate_payload(activity_type, payload)`,
            or activity_type is unknown.
    """
    if not activity.entity_id:
        raise ValueError("activity.entity_id is required")
    if not activity.source_url:
        raise ValueError("activity.source_url is required")
    if not activity.activity_type:
        raise ValueError("activity.activity_type is required")

    # Run payload validation against the registry. Surface a single-line
    # ValueError so callers (orchestrator) can record a concise failure reason.
    try:
        validate_payload(activity.activity_type, activity.payload or {})
    except ValidationError as exc:
        try:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            summary = ",".join(fields) or "<unknown>"
        except Exception:  # noqa: BLE001
            summary = "<errors>"
        raise ValueError(
            f"payload validation failed for activity_type={activity.activity_type}: {summary}"
        ) from exc

    activity_id = compute_activity_id(activity.source_url, activity.entity_id)
    occurred_at = _normalize_occurred_at(activity.occurred_at)
    ingested_at = _now_iso()
    payload_json = _payload_to_json(activity.payload or {})

    if isinstance(url_verified_at, datetime):
        url_verified_iso: str | None = url_verified_at.astimezone(timezone.utc).isoformat()
    elif isinstance(url_verified_at, str):
        url_verified_iso = url_verified_at
    else:
        url_verified_iso = None

    # Detect insert vs update by checking existence first; upsert in one pass.
    existing = conn.execute(
        "SELECT 1 FROM activity WHERE id = ?", (activity_id,)
    ).fetchone()
    was_new = existing is None

    conn.execute(
        """
        INSERT INTO activity (
            id, entity_id, entity_type, activity_type, occurred_at, ingested_at,
            source_url, source_adapter, title, raw_text, payload, url_verified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entity_id      = excluded.entity_id,
            entity_type    = excluded.entity_type,
            activity_type  = excluded.activity_type,
            occurred_at    = excluded.occurred_at,
            ingested_at    = excluded.ingested_at,
            source_url     = excluded.source_url,
            source_adapter = excluded.source_adapter,
            title          = excluded.title,
            raw_text       = excluded.raw_text,
            payload        = excluded.payload,
            url_verified_at = COALESCE(excluded.url_verified_at, activity.url_verified_at)
        """,
        (
            activity_id,
            activity.entity_id,
            activity.entity_type,
            activity.activity_type,
            occurred_at,
            ingested_at,
            activity.source_url,
            activity.source_adapter,
            activity.title or "",
            activity.raw_text or "",
            payload_json,
            url_verified_iso,
        ),
    )

    return activity_id, was_new


# ---------------------------------------------------------------------------
# entity reads
# ---------------------------------------------------------------------------


def list_entities_with_aliases(conn: sqlite3.Connection) -> list[EntityRef]:
    """Return all rows from `entity` as EntityRef instances.

    `aliases` and `metadata` are JSON-decoded; if either is malformed we fall
    back to empty list / dict and continue (don't break ingestion on bad seed
    data).
    """
    rows = conn.execute(
        """
        SELECT id, name, entity_type, subcategory, jurisdiction, aliases, metadata
        FROM entity
        ORDER BY id
        """
    ).fetchall()

    out: list[EntityRef] = []
    for r in rows:
        try:
            aliases = json.loads(r["aliases"]) if r["aliases"] else []
            if not isinstance(aliases, list):
                aliases = []
        except (TypeError, ValueError):
            aliases = []
        try:
            metadata = json.loads(r["metadata"]) if r["metadata"] else {}
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError):
            metadata = {}
        out.append(
            EntityRef(
                id=r["id"],
                name=r["name"],
                entity_type=r["entity_type"],
                subcategory=r["subcategory"],
                jurisdiction=r["jurisdiction"],
                aliases=[str(a) for a in aliases],
                metadata=metadata,
            )
        )
    return out


__all__ = [
    "compute_activity_id",
    "upsert_activity",
    "list_entities_with_aliases",
]
