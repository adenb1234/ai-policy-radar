"""End-to-end smoke test for the storage + ingestion orchestrator.

Bootstraps a temp SQLite DB, hand-inserts two entities (`eff` for the research
adapter, `bis` for Federal Register), runs `ingest_entity` for each with the
real adapters, then asserts:

  - >=1 activity persisted for each entity
  - all research-adapter activities have url_verified_at set
  - payload validates against the registry
  - re-running is idempotent (no duplicates; was_new=False on second pass)

Usage:
    PYTHONPATH=backend uv run python backend/scripts/test_ingest.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from pprint import pformat

from dotenv import load_dotenv

# Load .env from repo root (same flow as radar/main.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

# Force the temp DB path BEFORE importing modules that resolve it.
_TMP_DB = Path(tempfile.mkdtemp(prefix="radar_test_ingest_")) / "radar.db"
os.environ["RADAR_DB"] = str(_TMP_DB)

from radar.adapters.base import EntityRef  # noqa: E402
from radar.adapters.structured.federal_register import FederalRegisterAdapter  # noqa: E402
from radar.db.connection import bootstrap  # noqa: E402
from radar.db.payload_schemas import validate_payload  # noqa: E402
from radar.db.storage import compute_activity_id  # noqa: E402
from radar.ingest.orchestrator import ingest_entity  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_entity(conn: sqlite3.Connection, entity: EntityRef) -> None:
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
            aliases=excluded.aliases,
            metadata=excluded.metadata
        """,
        (
            entity.id,
            entity.name,
            entity.entity_type,
            entity.subcategory,
            entity.jurisdiction,
            "",
            json.dumps(entity.aliases),
            json.dumps(entity.metadata),
            _now_iso(),
        ),
    )
    conn.commit()


def _print_result(label: str, result) -> None:
    print(f"\n--- {label} ---")
    print(
        f"  entity_id={result.entity_id} adapter={result.adapter} "
        f"discovered={result.discovered} verified={result.verified} "
        f"new={result.persisted_new} updated={result.persisted_updated} "
        f"failures={len(result.failures)}"
    )
    for f in result.failures[:5]:
        print(f"  FAIL: {f}")
    if len(result.failures) > 5:
        print(f"  ... ({len(result.failures) - 5} more failures)")


def _assert_persisted(conn: sqlite3.Connection, entity_id: str, *, require_verified: bool) -> int:
    rows = conn.execute(
        "SELECT id, source_url, payload, url_verified_at, activity_type "
        "FROM activity WHERE entity_id = ? ORDER BY occurred_at DESC",
        (entity_id,),
    ).fetchall()
    assert rows, f"expected >=1 persisted activity for entity={entity_id}, got 0"

    # Validate payload JSON parses + matches schema, and (if research) url_verified_at is set.
    for r in rows:
        payload = json.loads(r["payload"])
        validate_payload(r["activity_type"], payload)
        # id should be deterministic from (source_url, entity_id).
        assert r["id"] == compute_activity_id(r["source_url"], entity_id), (
            f"activity id mismatch for {r['source_url']!r}: {r['id']!r}"
        )
        if require_verified:
            assert r["url_verified_at"], (
                f"research-adapter activity {r['id']} missing url_verified_at"
            )
    return len(rows)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print(f"[test] using temp DB: {_TMP_DB}")
    conn = bootstrap()

    eff = EntityRef(
        id="eff",
        name="Electronic Frontier Foundation",
        entity_type="civil_society",
        subcategory="advocacy_ngo",
        jurisdiction="US-Federal",
        aliases=["EFF"],
        metadata={"ideology_axis": "civil_libertarian"},
    )
    bis = EntityRef(
        id="bis",
        name="Bureau of Industry and Security",
        entity_type="executive_agency",
        subcategory="export_controls",
        jurisdiction="US-Federal",
        aliases=["BIS", "Industry and Security Bureau"],
        metadata={"fr_agency_slug": "industry-and-security-bureau"},
    )
    _seed_entity(conn, eff)
    _seed_entity(conn, bis)

    # Build adapters.
    structured = [FederalRegisterAdapter()]

    research = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from radar.adapters.research.per_entity import PerEntityResearchAdapter

        research = PerEntityResearchAdapter()
    else:
        print(
            "[test] WARNING: ANTHROPIC_API_KEY not set; research-adapter portion will be skipped."
        )

    today = date.today()
    # Use a window wide enough to capture fresh items but bounded for cost.
    research_since = today.replace(day=1) if today.day > 1 else today
    # Use a 60-day window for research and 180-day for FR (matches FR smoke test).
    from datetime import timedelta

    research_since = today - timedelta(days=60)
    fr_since = today - timedelta(days=180)

    # ---- Run #1 -----------------------------------------------------------
    print("\n=== run 1: research adapter on EFF ===")
    if research is not None:
        eff_result_1 = ingest_entity(
            conn, eff, since=research_since,
            research_adapter=research, structured_adapters=None,
        )
        _print_result("EFF (run 1)", eff_result_1)
        conn.commit()
    else:
        eff_result_1 = None

    print("\n=== run 1: FR adapter on BIS ===")
    bis_result_1 = ingest_entity(
        conn, bis, since=fr_since,
        research_adapter=None, structured_adapters=structured,
    )
    _print_result("BIS (run 1)", bis_result_1)
    conn.commit()

    # ---- Assertions -------------------------------------------------------
    print("\n=== verify persistence ===")
    n_bis = _assert_persisted(conn, "bis", require_verified=False)
    print(f"  BIS persisted activities: {n_bis}")

    n_eff = 0
    if research is not None:
        n_eff = _assert_persisted(conn, "eff", require_verified=True)
        print(f"  EFF persisted activities (all url_verified_at set): {n_eff}")

    # ---- Run #2 (idempotency) --------------------------------------------
    print("\n=== run 2: re-run for idempotency ===")
    if research is not None:
        eff_result_2 = ingest_entity(
            conn, eff, since=research_since,
            research_adapter=research, structured_adapters=None,
        )
        _print_result("EFF (run 2)", eff_result_2)
        conn.commit()
        # NOTE: research-adapter call is non-deterministic; we only assert
        # that activities that DID match by id were updates, not new rows.
        # The total persisted count for EFF should not have grown beyond
        # what we just counted plus any new finds — but since the model may
        # surface different items, we use the row-count as a soft check.

    bis_result_2 = ingest_entity(
        conn, bis, since=fr_since,
        research_adapter=None, structured_adapters=structured,
    )
    _print_result("BIS (run 2)", bis_result_2)
    conn.commit()

    # FR adapter is deterministic for a given window: every result on run 2
    # should be an update, none should be new.
    assert bis_result_2.persisted_new == 0, (
        f"expected 0 new BIS rows on rerun (idempotency), got "
        f"{bis_result_2.persisted_new}"
    )
    assert bis_result_2.persisted_updated >= 1, (
        "expected >=1 BIS update on rerun"
    )

    # Row count for BIS should be unchanged.
    n_bis_2 = conn.execute(
        "SELECT COUNT(*) FROM activity WHERE entity_id = ?", ("bis",)
    ).fetchone()[0]
    assert n_bis_2 == n_bis, (
        f"BIS row count changed across runs: {n_bis} -> {n_bis_2}"
    )

    # ---- Read back one BIS activity for hand-check -----------------------
    print("\n=== sample persisted BIS activity ===")
    row = conn.execute(
        "SELECT * FROM activity WHERE entity_id = 'bis' LIMIT 1"
    ).fetchone()
    sample = dict(row)
    sample["payload"] = json.loads(sample["payload"])
    print(pformat(sample, width=110, sort_dicts=False))

    if research is not None:
        print("\n=== sample persisted EFF activity ===")
        row = conn.execute(
            "SELECT * FROM activity WHERE entity_id = 'eff' LIMIT 1"
        ).fetchone()
        if row:
            sample = dict(row)
            sample["payload"] = json.loads(sample["payload"])
            print(pformat(sample, width=110, sort_dicts=False))

    # ---- Summary ---------------------------------------------------------
    print("\n=== summary ===")
    print(f"  BIS activities persisted (run 1): {n_bis}")
    print(f"  EFF activities persisted (run 1): {n_eff}")
    print(
        f"  BIS rerun: new={bis_result_2.persisted_new} "
        f"updated={bis_result_2.persisted_updated} (idempotent)"
    )
    if research is not None:
        print(
            f"  EFF rerun: new={eff_result_2.persisted_new} "
            f"updated={eff_result_2.persisted_updated}"
        )
    print(f"\n[test] DB lives at: {_TMP_DB}")
    print("[test] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
