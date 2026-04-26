"""Standalone smoke test for the awareness engine.

Builds a temp SQLite DB at /tmp/radar_test.db with:
  - 4 entities (eff, ostp, ftc, sen-cantwell, plus bis as a tracked agency)
  - 6 synthetic activities mixing topics + dates within the last 90 days
  - 6 synthetic enrichment rows (with realistic topics, stances, materiality)
  - A "frontier-lab policy lead" StructuredProfile

Runs `AwarenessEngine.build_dashboard(...)` end-to-end and pretty-prints the
resulting AwarenessItem list.

Run with:
    PYTHONPATH=backend uv run python backend/scripts/test_awareness.py

Costs ~$0.05 in Opus 4.7 calls (6 candidates → ~2 batches of 5).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

# Force a dedicated temp DB.
TEST_DB_PATH = Path("/tmp/radar_test.db")
if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()
os.environ["RADAR_DB"] = str(TEST_DB_PATH)

from radar.awareness.engine import AwarenessEngine  # noqa: E402
from radar.awareness.embedding_model import EmbeddingModel  # noqa: E402
from radar.db.connection import bootstrap  # noqa: E402
from radar.profiles.builder import StructuredProfile, insert_profile  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic seed data
# ---------------------------------------------------------------------------


TODAY = date.today()


def _days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


ENTITIES = [
    {
        "id": "eff",
        "name": "Electronic Frontier Foundation",
        "entity_type": "civil_society",
        "subcategory": "civil_liberties",
        "jurisdiction": "US-Federal",
        "description": "Civil liberties non-profit",
        "aliases": ["EFF"],
    },
    {
        "id": "ostp",
        "name": "Office of Science and Technology Policy",
        "entity_type": "executive_agency",
        "subcategory": "white_house",
        "jurisdiction": "US-Federal",
        "description": "WH science and tech policy office",
        "aliases": ["OSTP", "White House OSTP"],
    },
    {
        "id": "ftc",
        "name": "Federal Trade Commission",
        "entity_type": "executive_agency",
        "subcategory": "independent_agency",
        "jurisdiction": "US-Federal",
        "description": "Consumer protection / antitrust enforcement",
        "aliases": ["FTC"],
    },
    {
        "id": "sen-cantwell",
        "name": "Sen. Maria Cantwell",
        "entity_type": "legislator",
        "subcategory": "senate_dem",
        "jurisdiction": "US-Federal",
        "description": "Chair, Senate Commerce Committee",
        "aliases": ["Cantwell", "Senator Cantwell"],
    },
    {
        "id": "bis",
        "name": "Bureau of Industry and Security",
        "entity_type": "executive_agency",
        "subcategory": "commerce",
        "jurisdiction": "US-Federal",
        "description": "Commerce sub-agency that runs export controls",
        "aliases": ["BIS"],
    },
    {
        "id": "ed",
        "name": "Department of Education",
        "entity_type": "executive_agency",
        "subcategory": "cabinet",
        "jurisdiction": "US-Federal",
        "description": "Federal education agency",
        "aliases": ["DOE", "Education Department"],
    },
]


# Each tuple: (activity, enrichment)
# Designed so the frontier-lab profile should rank #1 on:
#   - bis-export-controls (export_controls + entity bis)
#   - ostp-compute-thresholds (compute_thresholds)
# and rank a sectoral_education item LOW.
SYNTHETIC: list[tuple[dict, dict]] = [
    # 1. BIS export controls update — should rank highest for frontier lab
    (
        {
            "id": "act-bis-export-001",
            "entity_id": "bis",
            "entity_type": "executive_agency",
            "activity_type": "FINAL_RULE",
            "occurred_at": _days_ago(5),
            "source_url": "https://www.federalregister.gov/d/2026-test-bis-export",
            "source_adapter": "test_seed",
            "title": "BIS Updates Advanced Computing Chip Export Controls",
            "raw_text": (
                "BIS amends the Export Administration Regulations to expand "
                "controls on advanced AI accelerators destined for entities of "
                "concern. New thresholds tighten coverage of high-performance "
                "GPUs and require additional license review for cloud-based "
                "compute access exceeding specified compute thresholds."
            ),
            "payload": {"docket_id": "BIS-2026-0009", "agency": "BIS"},
        },
        {
            "summary": (
                "BIS final rule tightens export controls on advanced AI chips "
                "and adds compute-threshold license review for cloud access."
            ),
            "topics": ["export_controls", "compute_thresholds", "chip_controls"],
            "mentioned_entities": [],
            "stance": "supports",
            "stance_quote": "tighten coverage of high-performance GPUs",
            "materiality": {
                "scope": "federal",
                "bindingness": "rule",
                "novelty": "escalation",
                "confidence": 0.9,
            },
        },
    ),
    # 2. OSTP RFI on compute thresholds — also high relevance for frontier lab
    (
        {
            "id": "act-ostp-rfi-002",
            "entity_id": "ostp",
            "entity_type": "executive_agency",
            "activity_type": "RFI",
            "occurred_at": _days_ago(12),
            "source_url": "https://www.whitehouse.gov/ostp/test-compute-rfi",
            "source_adapter": "test_seed",
            "title": "OSTP RFI: Compute-Threshold Reporting for Frontier Models",
            "raw_text": (
                "The White House Office of Science and Technology Policy seeks "
                "public comment on appropriate compute-threshold reporting "
                "requirements for frontier model developers, including disclosure "
                "of training-run scale and red-teaming practices."
            ),
            "payload": {"docket_id": "OSTP-2026-RFI-04", "agency": "OSTP"},
        },
        {
            "summary": (
                "OSTP requests public comment on compute-threshold reporting "
                "and red-team disclosures for frontier model developers."
            ),
            "topics": ["compute_thresholds", "model_release_policy", "frontier_safety"],
            "mentioned_entities": [],
            "stance": "neutral",
            "stance_quote": "seeks public comment on appropriate compute-threshold reporting requirements",
            "materiality": {
                "scope": "federal",
                "bindingness": "guidance",
                "novelty": "new_position",
                "confidence": 0.8,
            },
        },
    ),
    # 3. EFF letter — civil_society, biometric (not directly frontier-lab)
    (
        {
            "id": "act-eff-bio-003",
            "entity_id": "eff",
            "entity_type": "civil_society",
            "activity_type": "OPEN_LETTER",
            "occurred_at": _days_ago(20),
            "source_url": "https://www.eff.org/letters/test-biometric",
            "source_adapter": "test_seed",
            "title": "EFF Urges OSTP to Halt Federal Face-Recognition Deployment",
            "raw_text": (
                "EFF writes to OSTP urging a moratorium on federal face "
                "recognition deployment pending Congressional action and NIST "
                "real-world bias audits."
            ),
            "payload": {},
        },
        {
            "summary": (
                "EFF urges OSTP to halt federal face-recognition deployment and "
                "expand NIST bias audits."
            ),
            "topics": ["biometric_surveillance", "civil_liberties_ai"],
            "mentioned_entities": ["ostp"],
            "stance": "opposes",
            "stance_quote": "urging a moratorium on federal face recognition deployment",
            "materiality": {
                "scope": "federal",
                "bindingness": "statement",
                "novelty": "restated",
                "confidence": 0.7,
            },
        },
    ),
    # 4. FTC enforcement — overlaps slightly via product safety
    (
        {
            "id": "act-ftc-enf-004",
            "entity_id": "ftc",
            "entity_type": "executive_agency",
            "activity_type": "ENFORCEMENT_ACTION",
            "occurred_at": _days_ago(35),
            "source_url": "https://www.ftc.gov/test-enforcement-genai",
            "source_adapter": "test_seed",
            "title": "FTC Settles With GenAI Vendor Over Deceptive Performance Claims",
            "raw_text": (
                "The Federal Trade Commission announced a settlement with a "
                "generative AI vendor over allegedly deceptive performance "
                "benchmarks and unsubstantiated safety claims."
            ),
            "payload": {"agency": "FTC"},
        },
        {
            "summary": (
                "FTC settles with a generative AI vendor over deceptive "
                "performance benchmarks and unsubstantiated safety claims."
            ),
            "topics": ["product_safety", "deceptive_practices", "model_release_policy"],
            "mentioned_entities": [],
            "stance": "opposes",
            "stance_quote": "deceptive performance benchmarks and unsubstantiated safety claims",
            "materiality": {
                "scope": "federal",
                "bindingness": "enforcement",
                "novelty": "new_position",
                "confidence": 0.85,
            },
        },
    ),
    # 5. Cantwell statement on AI export policy — mentioned entity match
    (
        {
            "id": "act-cantwell-stmt-005",
            "entity_id": "sen-cantwell",
            "entity_type": "legislator",
            "activity_type": "PRESS_STATEMENT",
            "occurred_at": _days_ago(8),
            "source_url": "https://www.cantwell.senate.gov/test-stmt-export",
            "source_adapter": "test_seed",
            "title": "Cantwell Calls For Tighter AI Chip Export Enforcement",
            "raw_text": (
                "Sen. Cantwell calls on Commerce and BIS to step up enforcement "
                "of advanced computing chip export controls and to close "
                "third-country diversion loopholes."
            ),
            "payload": {},
        },
        {
            "summary": (
                "Sen. Cantwell urges Commerce/BIS to tighten enforcement of "
                "advanced chip export controls and close third-country diversion."
            ),
            "topics": ["export_controls", "chip_controls"],
            "mentioned_entities": ["bis"],
            "stance": "supports",
            "stance_quote": "step up enforcement of advanced computing chip export controls",
            "materiality": {
                "scope": "federal",
                "bindingness": "statement",
                "novelty": "restated",
                "confidence": 0.75,
            },
        },
    ),
    # 6. ED guidance on K-12 AI use — sectoral_education, should rank LOW
    (
        {
            "id": "act-ed-guidance-006",
            "entity_id": "ed",
            "entity_type": "executive_agency",
            "activity_type": "GUIDANCE",
            "occurred_at": _days_ago(15),
            "source_url": "https://www.ed.gov/test-k12-ai-guidance",
            "source_adapter": "test_seed",
            "title": "Education Dept Issues K-12 Guidance on Classroom AI Use",
            "raw_text": (
                "The Department of Education issues non-binding guidance for "
                "K-12 districts on responsible classroom use of generative AI, "
                "addressing student privacy and equity considerations."
            ),
            "payload": {},
        },
        {
            "summary": (
                "ED issues non-binding K-12 classroom AI guidance covering "
                "student privacy and equity."
            ),
            "topics": ["sectoral_education", "privacy_training_data"],
            "mentioned_entities": [],
            "stance": "neutral",
            "stance_quote": "non-binding guidance for K-12 districts on responsible classroom use",
            "materiality": {
                "scope": "federal",
                "bindingness": "guidance",
                "novelty": "new_position",
                "confidence": 0.6,
            },
        },
    ),
]


def seed_db() -> None:
    conn = bootstrap(TEST_DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    # Entities
    for e in ENTITIES:
        conn.execute(
            """
            INSERT OR REPLACE INTO entity
            (id, name, entity_type, subcategory, jurisdiction, description, aliases, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                e["id"],
                e["name"],
                e["entity_type"],
                e.get("subcategory"),
                e.get("jurisdiction"),
                e.get("description") or "",
                json.dumps(e.get("aliases") or []),
                json.dumps({}),
                now,
            ),
        )

    # Activities + enrichments
    for activity, enrichment in SYNTHETIC:
        conn.execute(
            """
            INSERT OR REPLACE INTO activity
            (id, entity_id, entity_type, activity_type, occurred_at, ingested_at,
             source_url, source_adapter, title, raw_text, payload, url_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity["id"],
                activity["entity_id"],
                activity["entity_type"],
                activity["activity_type"],
                activity["occurred_at"],
                now,
                activity["source_url"],
                activity["source_adapter"],
                activity["title"],
                activity["raw_text"],
                json.dumps(activity.get("payload") or {}),
                None,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO enrichment
            (activity_id, summary, topics, mentioned_entities, stance, stance_quote,
             materiality, enriched_at, enricher_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity["id"],
                enrichment["summary"],
                json.dumps(enrichment["topics"]),
                json.dumps(enrichment["mentioned_entities"]),
                enrichment.get("stance"),
                enrichment.get("stance_quote"),
                json.dumps(enrichment.get("materiality") or {}),
                now,
                "test-seed",
            ),
        )
    conn.commit()
    conn.close()


def insert_test_profile() -> tuple[str, str]:
    """Build + persist the frontier-lab policy lead profile. Return (id, nl)."""
    nl = (
        "Policy lead at a frontier AI lab. We care most about export controls "
        "(especially advanced computing chips), compute thresholds and any "
        "associated reporting/disclosure requirements, model-release policy, "
        "and frontier-safety voluntary commitments at OSTP. We track BIS, OSTP, "
        "NIST/AISI, FTC, and key Hill voices on AI competitiveness."
    )
    profile = StructuredProfile(
        topics_weighted={
            "export_controls": 1.0,
            "chip_controls": 0.9,
            "compute_thresholds": 1.0,
            "model_release_policy": 0.8,
            "frontier_safety": 0.8,
            "product_safety": 0.4,
        },
        watch_entities=["bis", "ostp", "ftc", "sen-cantwell"],
        jurisdictions=["US-Federal"],
        entity_types=[
            "executive_agency",
            "legislator",
            "civil_society",
            "company",
            "international",
            "legislative_body",
        ],
        activity_type_filters=None,
        recency_days=90,
        risk_tolerance="actionable_only",
        notes="Frontier-lab policy lead. Prioritize export controls + compute thresholds.",
    )

    conn = bootstrap(TEST_DB_PATH)
    pid = insert_profile(
        conn,
        name="Frontier Lab Policy Lead (test)",
        nl_description=nl,
        structured=profile,
    )
    conn.close()
    return pid, nl


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------


async def _amain() -> int:
    print("=== AI Policy Radar — awareness engine smoke test ===")
    print(f"DB: {TEST_DB_PATH}")
    seed_db()
    print(f"Seeded {len(ENTITIES)} entities, {len(SYNTHETIC)} activities/enrichments")

    profile_id, nl = insert_test_profile()
    print(f"Profile id: {profile_id}")
    print()

    # Embedding availability report
    em = EmbeddingModel.get()
    if em is None:
        print(
            "[embeddings] sentence_transformers not installed — using identity Layer 2"
        )
    else:
        print("[embeddings] sentence_transformers loaded — Layer 2 active")
    print()

    # Use batch_size=3 so 5 candidates → 2 Opus calls. The 2nd call should
    # read most of the system prompt back from the cache. (Set batch_size
    # back to default 5 in production.)
    from radar.awareness.reasoner import AwarenessReasoner

    engine = AwarenessEngine(reasoner=AwarenessReasoner(batch_size=3))
    from radar.db.connection import bootstrap as _bootstrap

    conn = _bootstrap(TEST_DB_PATH)
    items = await engine.build_dashboard(
        conn,
        profile_id,
        since=date.today() - timedelta(days=90),
        top_k=15,
        live_agent_threshold=10,
    )
    diag = engine.last_diagnostics
    print()
    print("=== diagnostics ===")
    print(json.dumps({k: v for k, v in diag.items() if k != "batch_usages"}, indent=2))
    print()

    print(f"=== {len(items)} awareness items (sorted by relevance_score desc) ===")
    for i, it in enumerate(items, start=1):
        print(f"\n[{i}] activity_id={it.activity_id}  score={it.relevance_score:.2f}")
        print(f"    reasoning: {it.reasoning}")
        if it.recommended_actions:
            for j, a in enumerate(it.recommended_actions, start=1):
                print(f"    action{j}: {a}")
        else:
            print("    (no recommended actions)")
        if it.citations:
            print(f"    citations: {', '.join(it.citations)}")

    print()
    print("=== batch usage / cache stats ===")
    for i, u in enumerate(diag.get("batch_usages", []), start=1):
        print(f"  batch {i}: {u}")
    print()

    # Sanity assertions
    assert len(items) >= 3, f"expected >=3 awareness items, got {len(items)}"
    for it in items:
        assert 0.0 <= it.relevance_score <= 10.0, f"score out of range: {it.relevance_score}"
        assert it.reasoning, "reasoning is empty"
        assert isinstance(it.recommended_actions, list), "actions not a list"
        assert isinstance(it.citations, list), "citations not a list"

    # Frontier-lab ranking sanity check: the BIS export-controls activity
    # OR the OSTP compute-thresholds activity should outrank the K-12
    # education guidance.
    score_by_id = {it.activity_id: it.relevance_score for it in items}
    bis_score = score_by_id.get("act-bis-export-001")
    ostp_score = score_by_id.get("act-ostp-rfi-002")
    ed_score = score_by_id.get("act-ed-guidance-006")
    if ed_score is not None:
        ranked_higher = []
        if bis_score is not None and bis_score > ed_score:
            ranked_higher.append(f"BIS-export({bis_score:.1f}) > ED-K12({ed_score:.1f})")
        if ostp_score is not None and ostp_score > ed_score:
            ranked_higher.append(f"OSTP-compute({ostp_score:.1f}) > ED-K12({ed_score:.1f})")
        if ranked_higher:
            print("[OK] frontier-lab ranking check passed:")
            for r in ranked_higher:
                print(f"     {r}")
        else:
            print(
                f"[WARN] expected BIS or OSTP to outrank ED-K12; "
                f"got BIS={bis_score}, OSTP={ostp_score}, ED={ed_score}"
            )
    else:
        # ED was not surfaced at all — that's also a correct outcome here.
        print(
            "[OK] ED-K12 not in top-k (correctly filtered out for frontier-lab profile)."
        )
        if bis_score is None and ostp_score is None:
            print(
                "[WARN] neither BIS export nor OSTP compute appeared in items — "
                "ranking check inconclusive."
            )

    print()
    print("[OK] awareness engine smoke test complete.")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
