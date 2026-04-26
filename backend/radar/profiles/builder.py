"""Profile builder (BUILD_PLAN §11).

Converts a user's natural-language description of their organization +
interests into a structured `user_profile.structured` JSON, persisted to
SQLite. Driven by Claude Opus 4.7 with structured tool-calling — the model
emits a single `emit_profile` tool call whose arguments populate
`StructuredProfile`.

Persistence layer talks to the `user_profile` table (see schema.sql §6.6).

The system prompt inlines:
  - the controlled topic vocabulary (id + name only — synonyms are dropped to
    keep the cached prefix small)
  - the entity directory (id, name, entity_type, jurisdiction)

Both are loaded at adapter init time so updates to the YAMLs flow through on
the next instantiation. If either YAML is missing we emit a logging warning
and run with empty vocab/directory — the LLM will then output unconstrained
keys and validation will drop them all.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import anthropic
import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — valid sets used during validation
# ---------------------------------------------------------------------------

# Per schema.sql §6.1 — the 9 valid entity_types.
VALID_ENTITY_TYPES: list[str] = [
    "company",
    "legislator",
    "legislative_body",
    "judiciary",
    "executive_agency",
    "state_local",
    "civil_society",
    "international",
    "party_faction",
]

# Curated jurisdiction set per task spec. We're permissive (log unknowns
# rather than dropping silently) because the LLM may emit reasonable values
# we haven't enumerated yet (e.g. "US-WA").
KNOWN_JURISDICTIONS: set[str] = {
    "US-Federal",
    "US-CA",
    "US-NY",
    "US-TX",
    "EU",
    "UK",
    "CN",
    "JP",
    "global",
    "sector",
}

VALID_RECENCY_DAYS: tuple[int, ...] = (7, 30, 90, 180)
VALID_RISK_TOLERANCE: tuple[str, ...] = ("informational", "actionable_only")

DEFAULT_TOPICS_PATH = Path(__file__).resolve().parents[2] / "data" / "topics.yaml"
DEFAULT_ENTITY_PATH = Path(__file__).resolve().parents[2] / "data" / "entity_seed.yaml"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class StructuredProfile:
    """Structured representation of a user's interests (BUILD_PLAN §11)."""

    topics_weighted: dict[str, float] = field(default_factory=dict)
    watch_entities: list[str] = field(default_factory=list)
    jurisdictions: list[str] = field(default_factory=list)
    entity_types: list[str] = field(default_factory=list)
    activity_type_filters: list[str] | None = None
    recency_days: int = 30
    risk_tolerance: Literal["informational", "actionable_only"] = "actionable_only"
    notes: str | None = None


# ---------------------------------------------------------------------------
# YAML loaders
# ---------------------------------------------------------------------------


def _load_topics_vocab(path: Path) -> list[dict[str, str]]:
    """Load the topics YAML into [{id, name}, ...]. Empty list if missing."""
    if not path.exists():
        log.warning(
            "topics.yaml not found at %s — profile builder will run with an empty topic vocabulary",
            path,
        )
        return []
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("failed to parse topics.yaml at %s: %s", path, e)
        return []
    if not isinstance(raw, list):
        log.warning("topics.yaml at %s did not produce a list (got %s)", path, type(raw).__name__)
        return []
    out: list[dict[str, str]] = []
    for elem in raw:
        if not isinstance(elem, dict):
            continue
        tid = elem.get("id")
        name = elem.get("name") or tid
        if isinstance(tid, str):
            out.append({"id": tid, "name": str(name)})
    return out


def _load_entity_directory(path: Path) -> list[dict[str, str]]:
    """Load entity_seed YAML into [{id, name, entity_type, jurisdiction}, ...]."""
    if not path.exists():
        log.warning(
            "entity_seed.yaml not found at %s — profile builder will run with an empty entity directory",
            path,
        )
        return []
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("failed to parse entity_seed.yaml at %s: %s", path, e)
        return []
    if not isinstance(raw, list):
        log.warning(
            "entity_seed.yaml at %s did not produce a list (got %s)",
            path,
            type(raw).__name__,
        )
        return []
    out: list[dict[str, str]] = []
    for elem in raw:
        if not isinstance(elem, dict):
            continue
        eid = elem.get("id")
        if not isinstance(eid, str):
            continue
        out.append(
            {
                "id": eid,
                "name": str(elem.get("name") or eid),
                "entity_type": str(elem.get("entity_type") or ""),
                "jurisdiction": str(elem.get("jurisdiction") or ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Prompt + tool schema
# ---------------------------------------------------------------------------


_EMIT_PROFILE_TOOL_DESC = (
    "Emit the structured policy-tracking profile extracted from the user's "
    "natural-language description. Call this tool exactly once."
)


def _build_emit_profile_tool(
    valid_topic_ids: list[str],
    valid_entity_ids: list[str],
) -> dict[str, Any]:
    """Build the `emit_profile` tool schema. The schema is deterministic given
    the vocab + directory, which keeps the cached prompt prefix stable."""
    return {
        "name": "emit_profile",
        "description": _EMIT_PROFILE_TOOL_DESC,
        "input_schema": {
            "type": "object",
            "properties": {
                "topics_weighted": {
                    "type": "object",
                    "description": (
                        "Map of topic_id -> weight in (0, 1]. Use 3-10 topics. "
                        "Only use topic_ids from the vocabulary inlined in the system prompt."
                    ),
                    "additionalProperties": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "watch_entities": {
                    "type": "array",
                    "description": (
                        "Entity ids the user is *especially* interested in (not every "
                        "entity in their domain). Only use ids from the entity directory. "
                        "Empty list is fine."
                    ),
                    "items": {"type": "string"},
                },
                "jurisdictions": {
                    "type": "array",
                    "description": (
                        "Jurisdictions of interest, e.g. ['US-Federal'], ['US-CA','US-Federal']. "
                        "Default to ['US-Federal'] if unclear."
                    ),
                    "items": {"type": "string"},
                },
                "entity_types": {
                    "type": "array",
                    "description": (
                        "Which entity_types to surface in the dashboard. Valid values: "
                        + ", ".join(VALID_ENTITY_TYPES)
                        + ". Default to all 9 if not specified."
                    ),
                    "items": {"type": "string", "enum": list(VALID_ENTITY_TYPES)},
                },
                "activity_type_filters": {
                    "type": ["array", "null"],
                    "description": (
                        "Optional further narrowing by activity_type (e.g. ['NPRM', 'FINAL_RULE']). "
                        "Use null to skip filtering."
                    ),
                    "items": {"type": "string"},
                },
                "recency_days": {
                    "type": "integer",
                    "description": "How recent activities must be. One of: 7, 30, 90, 180. Prefer 30 unless the description suggests otherwise.",
                    "enum": list(VALID_RECENCY_DAYS),
                },
                "risk_tolerance": {
                    "type": "string",
                    "description": (
                        "'actionable_only' for users who want only items they should act on; "
                        "'informational' for research-oriented users who want broader awareness. "
                        "Default to 'actionable_only' unless the description sounds informational/research."
                    ),
                    "enum": list(VALID_RISK_TOLERANCE),
                },
                "notes": {
                    "type": ["string", "null"],
                    "description": "Any other free-form constraint pulled from the NL that doesn't fit other fields.",
                },
            },
            "required": [
                "topics_weighted",
                "watch_entities",
                "jurisdictions",
                "entity_types",
                "recency_days",
                "risk_tolerance",
            ],
            "additionalProperties": False,
        },
    }


def _build_system_prompt(
    topics_vocab: list[dict[str, str]],
    entity_directory: list[dict[str, str]],
) -> str:
    """Assemble the cacheable system prompt.

    Stable content (instructions + vocab + directory) sits before any volatile
    content. The user turn carries only the NL description.
    """
    topic_lines = "\n".join(f"  - {t['id']}: {t['name']}" for t in topics_vocab) or "  (vocabulary unavailable)"
    entity_lines = (
        "\n".join(
            f"  - {e['id']}: {e['name']} [{e['entity_type']}, {e['jurisdiction']}]"
            for e in entity_directory
        )
        or "  (directory unavailable)"
    )

    return (
        "You convert a user's natural-language description into a structured "
        "policy-tracking profile for the AI Policy Radar dashboard.\n"
        "\n"
        "You will receive a single user turn: a free-form description of the "
        "user's organization and what they care about. Your job is to call the "
        "`emit_profile` tool exactly once with the structured representation.\n"
        "\n"
        "Strict rules:\n"
        "1. `topics_weighted` — infer 3-10 topics from the NL, weighted in [0.3, 1.0]. "
        "   Use only topic_ids from the vocabulary below. NEVER invent new topic ids.\n"
        "2. `watch_entities` — list entity ids the user is *especially* interested in, "
        "   not every entity in their domain. Only use ids from the directory below. "
        "   An empty list is fine.\n"
        "3. `jurisdictions` — infer from the description (US-Federal, US-CA, US-NY, "
        "   US-TX, EU, UK, CN, JP, global, sector). Default to ['US-Federal'] if unclear.\n"
        "4. `entity_types` — pick from the 9 valid values. Default to all 9 if not "
        "   constrained by the description.\n"
        "5. `recency_days` — one of {7, 30, 90, 180}. Prefer 30 unless the NL implies otherwise.\n"
        "6. `risk_tolerance` — 'actionable_only' by default. Use 'informational' only "
        "   when the description sounds research-oriented (e.g. academic, journalist, think-tank).\n"
        "7. `activity_type_filters` — usually null. Set only if the NL clearly constrains.\n"
        "8. `notes` — a one-line capture of free-form constraints that don't fit elsewhere.\n"
        "\n"
        "Topic vocabulary (id: name) — these are the ONLY valid topic_ids:\n"
        f"{topic_lines}\n"
        "\n"
        "Entity directory (id: name [entity_type, jurisdiction]) — these are the ONLY "
        "valid entity ids for `watch_entities`:\n"
        f"{entity_lines}\n"
        "\n"
        "Call `emit_profile` exactly once. Do not emit any other tool call or any "
        "free-form text alongside the tool call."
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class ProfileBuilder:
    """Convert NL → StructuredProfile via Claude Opus 4.7 + tool calling."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        topics_path: Path | None = None,
        entity_directory: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in the environment; the profile "
                "builder cannot run. Load it from .env (see radar/main.py)."
            )

        # Let SDK pull ANTHROPIC_API_KEY from env so we never log it.
        self._client = anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens

        self._topics_path = topics_path or DEFAULT_TOPICS_PATH
        self._topics_vocab = _load_topics_vocab(self._topics_path)
        self._valid_topic_ids: set[str] = {t["id"] for t in self._topics_vocab}

        if entity_directory is not None:
            self._entity_directory = entity_directory
        else:
            self._entity_directory = _load_entity_directory(DEFAULT_ENTITY_PATH)
        self._valid_entity_ids: set[str] = {e["id"] for e in self._entity_directory}

        self._tool = _build_emit_profile_tool(
            sorted(self._valid_topic_ids),
            sorted(self._valid_entity_ids),
        )
        self._system_prompt = _build_system_prompt(
            self._topics_vocab, self._entity_directory
        )

        self.last_usage: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def build(
        self,
        nl_description: str,
        *,
        structured_overrides: dict | None = None,
    ) -> StructuredProfile:
        """Extract a StructuredProfile from `nl_description`.

        `structured_overrides` (from the form on /profile/new) merges in
        AFTER the LLM extraction — the form wins on direct conflicts. This
        lets the user tweak details without re-prompting.
        """
        nl_description = (nl_description or "").strip()
        if not nl_description:
            raise ValueError("nl_description must not be empty")

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[self._tool],
            tool_choice={"type": "tool", "name": "emit_profile"},
            messages=[{"role": "user", "content": nl_description}],
        )

        # Capture usage. Defensive — fields differ across SDK versions.
        try:
            usage = response.usage
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            }
        except Exception:  # noqa: BLE001
            self.last_usage = None

        tool_args = self._extract_tool_args(response)
        if tool_args is None:
            raise RuntimeError(
                "model did not return an `emit_profile` tool call "
                f"(stop_reason={getattr(response, 'stop_reason', '?')})"
            )

        profile = self._validate_args(tool_args)

        if structured_overrides:
            profile = self._apply_overrides(profile, structured_overrides)

        return profile

    def to_db_json(self, profile: StructuredProfile) -> str:
        """Serialize StructuredProfile to a stable JSON string (sort_keys=True)."""
        return json.dumps(asdict(profile), sort_keys=True)

    def from_db_json(self, blob: str) -> StructuredProfile:
        """Deserialize a JSON blob back into a StructuredProfile."""
        data = json.loads(blob)
        if not isinstance(data, dict):
            raise ValueError("structured profile JSON must decode to an object")

        # Coerce / default any missing fields so older serialized blobs
        # still round-trip.
        return StructuredProfile(
            topics_weighted={
                str(k): float(v) for k, v in (data.get("topics_weighted") or {}).items()
            },
            watch_entities=[str(x) for x in (data.get("watch_entities") or [])],
            jurisdictions=[str(x) for x in (data.get("jurisdictions") or [])],
            entity_types=[str(x) for x in (data.get("entity_types") or [])],
            activity_type_filters=(
                [str(x) for x in data["activity_type_filters"]]
                if isinstance(data.get("activity_type_filters"), list)
                else None
            ),
            recency_days=int(data.get("recency_days") or 30),
            risk_tolerance=(
                data.get("risk_tolerance")
                if data.get("risk_tolerance") in VALID_RISK_TOLERANCE
                else "actionable_only"
            ),
            notes=(data.get("notes") if isinstance(data.get("notes"), str) else None),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_args(response: anthropic.types.Message) -> dict | None:
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_profile":
                inp = getattr(block, "input", None)
                if isinstance(inp, dict):
                    return inp
        return None

    def _validate_args(self, args: dict) -> StructuredProfile:
        # topics_weighted ----------------------------------------------------
        raw_topics = args.get("topics_weighted") or {}
        topics_weighted: dict[str, float] = {}
        if isinstance(raw_topics, dict):
            for tid, weight in raw_topics.items():
                if not isinstance(tid, str):
                    continue
                if self._valid_topic_ids and tid not in self._valid_topic_ids:
                    log.warning("dropping unknown topic_id from LLM output: %r", tid)
                    continue
                try:
                    w = float(weight)
                except (TypeError, ValueError):
                    log.warning("dropping non-numeric weight for topic %r: %r", tid, weight)
                    continue
                # Clamp to (0, 1].
                if w <= 0:
                    log.warning("dropping non-positive weight for topic %r: %r", tid, w)
                    continue
                if w > 1.0:
                    w = 1.0
                topics_weighted[tid] = w

        # watch_entities -----------------------------------------------------
        raw_watch = args.get("watch_entities") or []
        watch_entities: list[str] = []
        if isinstance(raw_watch, list):
            for eid in raw_watch:
                if not isinstance(eid, str):
                    continue
                if self._valid_entity_ids and eid not in self._valid_entity_ids:
                    log.warning("dropping unknown entity_id from LLM output: %r", eid)
                    continue
                if eid not in watch_entities:
                    watch_entities.append(eid)

        # jurisdictions ------------------------------------------------------
        raw_juris = args.get("jurisdictions") or []
        jurisdictions: list[str] = []
        if isinstance(raw_juris, list):
            for j in raw_juris:
                if not isinstance(j, str):
                    continue
                if j not in KNOWN_JURISDICTIONS:
                    log.warning("unknown jurisdiction in LLM output (kept): %r", j)
                if j not in jurisdictions:
                    jurisdictions.append(j)
        if not jurisdictions:
            jurisdictions = ["US-Federal"]

        # entity_types -------------------------------------------------------
        raw_etypes = args.get("entity_types") or []
        valid_etypes_set = set(VALID_ENTITY_TYPES)
        entity_types: list[str] = []
        if isinstance(raw_etypes, list):
            for et in raw_etypes:
                if isinstance(et, str) and et in valid_etypes_set and et not in entity_types:
                    entity_types.append(et)
        if not entity_types:
            entity_types = list(VALID_ENTITY_TYPES)

        # activity_type_filters ----------------------------------------------
        raw_atypes = args.get("activity_type_filters")
        if isinstance(raw_atypes, list):
            activity_type_filters: list[str] | None = [str(a) for a in raw_atypes if isinstance(a, str)]
            if not activity_type_filters:
                activity_type_filters = None
        else:
            activity_type_filters = None

        # recency_days -------------------------------------------------------
        raw_recency = args.get("recency_days")
        try:
            recency = int(raw_recency)
        except (TypeError, ValueError):
            recency = 30
        if recency not in VALID_RECENCY_DAYS:
            # Snap to nearest legal value.
            recency = min(VALID_RECENCY_DAYS, key=lambda v: abs(v - recency))

        # risk_tolerance -----------------------------------------------------
        raw_risk = args.get("risk_tolerance")
        risk: Literal["informational", "actionable_only"]
        if raw_risk in VALID_RISK_TOLERANCE:
            risk = raw_risk  # type: ignore[assignment]
        else:
            risk = "actionable_only"

        # notes --------------------------------------------------------------
        raw_notes = args.get("notes")
        notes = raw_notes.strip() if isinstance(raw_notes, str) and raw_notes.strip() else None

        return StructuredProfile(
            topics_weighted=topics_weighted,
            watch_entities=watch_entities,
            jurisdictions=jurisdictions,
            entity_types=entity_types,
            activity_type_filters=activity_type_filters,
            recency_days=recency,
            risk_tolerance=risk,
            notes=notes,
        )

    @staticmethod
    def _apply_overrides(profile: StructuredProfile, overrides: dict) -> StructuredProfile:
        """Form-wins-on-direct-conflicts merge.

        For each key present in `overrides`, replace the value on the profile.
        Unknown keys are ignored with a log warning.
        """
        valid_keys = {f for f in profile.__dataclass_fields__}
        for key, value in overrides.items():
            if key not in valid_keys:
                log.warning("ignoring unknown override key: %r", key)
                continue
            setattr(profile, key, value)
        return profile


# ---------------------------------------------------------------------------
# Persistence (user_profile table)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_profile_id() -> str:
    return uuid.uuid4().hex[:12]


def _profile_to_blob(profile: StructuredProfile) -> str:
    return json.dumps(asdict(profile), sort_keys=True)


def insert_profile(
    conn: sqlite3.Connection,
    *,
    name: str,
    nl_description: str,
    structured: StructuredProfile,
) -> str:
    """INSERT INTO user_profile, return new id (uuid4 hex prefix).

    On primary-key conflict (uuid collision is implausible but we cover it),
    bumps `updated_at` and rewrites `structured` + `nl_description`.
    """
    if not name or not name.strip():
        raise ValueError("profile name must not be empty")
    if not nl_description or not nl_description.strip():
        raise ValueError("profile nl_description must not be empty")

    profile_id = _new_profile_id()
    blob = _profile_to_blob(structured)
    now = _now_iso()

    conn.execute(
        """
        INSERT INTO user_profile (id, name, nl_description, structured, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name           = excluded.name,
            nl_description = excluded.nl_description,
            structured     = excluded.structured,
            updated_at     = excluded.updated_at
        """,
        (profile_id, name.strip(), nl_description, blob, now, now),
    )
    conn.commit()
    return profile_id


def get_profile(
    conn: sqlite3.Connection, profile_id: str
) -> tuple[str, str, StructuredProfile] | None:
    """Return (name, nl_description, StructuredProfile) or None."""
    row = conn.execute(
        "SELECT name, nl_description, structured FROM user_profile WHERE id = ?",
        (profile_id,),
    ).fetchone()
    if row is None:
        return None
    name = row["name"] if hasattr(row, "keys") else row[0]
    nl = row["nl_description"] if hasattr(row, "keys") else row[1]
    blob = row["structured"] if hasattr(row, "keys") else row[2]

    # Reuse the deserializer for backward compatibility.
    builder = _make_loader_builder()
    profile = builder.from_db_json(blob)
    return (name, nl, profile)


def list_profiles(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return [(profile_id, name), ...] for the landing page."""
    rows = conn.execute(
        "SELECT id, name FROM user_profile ORDER BY updated_at DESC"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# Lazy reusable builder used purely as a deserializer (no API key needed for
# from_db_json — but the constructor checks the env var, so we provide a
# fallback path that bypasses init when only deserialization is wanted).
_LOADER_BUILDER: ProfileBuilder | None = None


def _make_loader_builder() -> ProfileBuilder:
    """Construct a no-op-style builder for `from_db_json`.

    `from_db_json` doesn't touch the API client, but `ProfileBuilder.__init__`
    requires `ANTHROPIC_API_KEY`. Fall back to a thin shim object exposing the
    same `from_db_json` if the key isn't set, so reads work even when the env
    is read-only.
    """
    global _LOADER_BUILDER
    if _LOADER_BUILDER is not None:
        return _LOADER_BUILDER
    if os.environ.get("ANTHROPIC_API_KEY"):
        _LOADER_BUILDER = ProfileBuilder.__new__(ProfileBuilder)
        # We don't actually need any of the runtime state — from_db_json is
        # functionally static. Bind nothing.
        return _LOADER_BUILDER  # type: ignore[return-value]

    # Construct a bare shim that exposes only `from_db_json` semantics.
    shim = ProfileBuilder.__new__(ProfileBuilder)
    _LOADER_BUILDER = shim
    return shim


# ---------------------------------------------------------------------------
# Belt-and-suspenders: redact any sk-... that might show up in str(exc) etc.
# ---------------------------------------------------------------------------


def _redact_key(s: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-REDACTED", s)


__all__ = [
    "StructuredProfile",
    "ProfileBuilder",
    "VALID_ENTITY_TYPES",
    "VALID_RECENCY_DAYS",
    "VALID_RISK_TOLERANCE",
    "KNOWN_JURISDICTIONS",
    "insert_profile",
    "get_profile",
    "list_profiles",
]
