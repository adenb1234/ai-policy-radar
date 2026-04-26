"""Seed entity, topic, and membership tables from YAML fixtures.

Idempotent: re-running upserts existing rows by id rather than failing.

Usage:
    PYTHONPATH=backend uv run python -m radar.scripts.seed_entities
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from radar.db.connection import bootstrap

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "backend" / "data"
ENTITY_YAML = DATA_DIR / "entity_seed.yaml"
TOPICS_YAML = DATA_DIR / "topics.yaml"

VALID_ENTITY_TYPES = {
    "company", "legislator", "legislative_body", "judiciary",
    "executive_agency", "state_local", "civil_society",
    "international", "party_faction",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_yaml(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[seed] missing: {path}", file=sys.stderr)
        return []
    with path.open() as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a YAML list at top level")
    return data


def seed_entities(conn, records: list[dict]) -> tuple[int, int]:
    """Returns (entities_upserted, memberships_upserted)."""
    entity_ids: set[str] = set()
    factions: list[tuple[str, list[dict | str]]] = []
    now = _now()

    for r in records:
        if "id" not in r or "name" not in r or "entity_type" not in r:
            raise ValueError(f"entity record missing required field: {r}")
        if r["entity_type"] not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"entity {r['id']!r}: invalid entity_type {r['entity_type']!r}"
            )
        entity_ids.add(r["id"])

    for r in records:
        conn.execute(
            """
            INSERT INTO entity (
                id, name, entity_type, subcategory, jurisdiction,
                description, aliases, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                entity_type=excluded.entity_type,
                subcategory=excluded.subcategory,
                jurisdiction=excluded.jurisdiction,
                description=excluded.description,
                aliases=excluded.aliases,
                metadata=excluded.metadata
            """,
            (
                r["id"],
                r["name"],
                r["entity_type"],
                r.get("subcategory"),
                r.get("jurisdiction"),
                (r.get("description") or "").strip(),
                json.dumps(r.get("aliases", []) or []),
                json.dumps(r.get("metadata", {}) or {}),
                now,
            ),
        )
        if r["entity_type"] == "party_faction" and r.get("members"):
            factions.append((r["id"], r["members"]))

    membership_count = 0
    for faction_id, members in factions:
        for m in members:
            if isinstance(m, str):
                member_id, role = m, None
            elif isinstance(m, dict):
                member_id = m.get("id")
                role = m.get("role")
            else:
                raise ValueError(f"faction {faction_id!r}: bad member entry {m!r}")
            if member_id not in entity_ids:
                raise ValueError(
                    f"faction {faction_id!r}: member {member_id!r} not in entity universe"
                )
            conn.execute(
                """
                INSERT INTO membership (group_id, member_id, role)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id, member_id) DO UPDATE SET role=excluded.role
                """,
                (faction_id, member_id, role),
            )
            membership_count += 1

    return len(records), membership_count


def seed_topics(conn, records: list[dict]) -> int:
    ids = {r["id"] for r in records}
    for r in records:
        parent = r.get("parent_id")
        if parent and parent not in ids:
            raise ValueError(f"topic {r['id']!r}: parent_id {parent!r} not in topic set")
        conn.execute(
            """
            INSERT INTO topic (id, name, parent_id, synonyms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                parent_id=excluded.parent_id,
                synonyms=excluded.synonyms
            """,
            (
                r["id"],
                r["name"],
                parent,
                json.dumps(r.get("synonyms", []) or []),
            ),
        )
    return len(records)


def main() -> int:
    conn = bootstrap()
    entities = load_yaml(ENTITY_YAML)
    topics = load_yaml(TOPICS_YAML)
    if not entities and not topics:
        print("[seed] no fixture data found; nothing to seed.", file=sys.stderr)
        return 1

    n_ent, n_mem = (0, 0)
    n_top = 0
    if entities:
        n_ent, n_mem = seed_entities(conn, entities)
    if topics:
        n_top = seed_topics(conn, topics)
    conn.commit()

    by_type = conn.execute(
        "SELECT entity_type, COUNT(*) FROM entity GROUP BY entity_type ORDER BY 1"
    ).fetchall()
    print(f"[seed] entities upserted: {n_ent}, memberships: {n_mem}, topics: {n_top}")
    print("[seed] entity counts by type:")
    for t, c in by_type:
        print(f"  {t:20s} {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
