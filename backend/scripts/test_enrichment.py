"""Standalone smoke test for the enrichment pipeline.

Builds a synthetic Activity dict (a hand-written EFF-style open letter on
biometric surveillance) plus a small entity directory, runs
`pipeline.enrich_activity`, and pretty-prints the result. No DB required.

Run with:
    PYTHONPATH=backend uv run python backend/scripts/test_enrichment.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

from radar.enrich.pipeline import EnrichmentError, EnrichmentPipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixture
# ---------------------------------------------------------------------------

# Realistic-ish EFF open letter on biometric surveillance (~350 words).
# Mentions OSTP, NIST, FTC by name (so the model has real entities to surface
# in mentioned_entities) and takes a clear stance ("opposes" the deployment of
# face recognition on federal property).
SYNTHETIC_RAW_TEXT = """\
The Electronic Frontier Foundation (EFF) writes today to urge the White House
Office of Science and Technology Policy (OSTP) to halt the planned deployment
of real-time face recognition systems at federal buildings, courthouses, and
ports of entry. As detailed in our March policy paper, "Faces in the Crowd,"
mass biometric surveillance is incompatible with the basic civil liberties
guarantees of the First and Fourth Amendments.

Face recognition is not a neutral identification technology. It is a tool of
mass surveillance that — once deployed at scale — chills protest, dissent, and
ordinary public assembly. Independent audits by the National Institute of
Standards and Technology (NIST) have repeatedly demonstrated that
commercially-available systems exhibit substantially higher false-positive
rates on Black women and on Asian American faces than on white men. These are
not abstract statistical defects. They translate directly into wrongful
arrests, mistaken denials of entry, and a chilling of constitutionally
protected activity.

We further note that the Federal Trade Commission (FTC) has separately raised
concerns about misleading vendor claims of accuracy in this market, and that
the Commission's recent enforcement actions against face-recognition firms
demonstrate the inadequacy of vendor self-regulation.

EFF therefore calls on OSTP to:

  1. Issue a binding government-wide moratorium on the use of one-to-many face
     recognition by federal agencies pending Congressional action;
  2. Direct NIST to expand its Face Recognition Vendor Test program to
     include real-world bias audits in deployment contexts; and
  3. Require any federal agency using one-to-one biometric verification to
     publish a public impact assessment, including disparate-impact analysis,
     before deployment.

The status quo — quiet, agency-by-agency adoption with minimal public
oversight — is unacceptable. Mass face surveillance must not become the
default, and the federal government must not endorse, by deployment or by
inaction, a technology that demonstrably encodes and amplifies racial bias.

We urge OSTP to act, and we stand ready to provide further technical and
legal analysis on request.

— Cindy Cohn, Executive Director, Electronic Frontier Foundation
"""


SYNTHETIC_ACTIVITY = {
    "id": "test-eff-biometric-001",
    "entity_id": "eff",
    "entity_type": "civil_society",
    "activity_type": "OPEN_LETTER",
    "occurred_at": "2026-04-15",
    "source_url": "https://www.eff.org/letters/2026-04-15-eff-letter-ostp-biometric-moratorium",
    "title": "EFF Urges OSTP to Halt Federal Face-Recognition Deployment",
    "raw_text": SYNTHETIC_RAW_TEXT,
}


# Hand-built directory — id, name, aliases. Includes EFF (source — should NOT
# appear in mentioned_entities) and the four agencies the letter references.
SYNTHETIC_ENTITY_DIRECTORY = [
    {
        "id": "eff",
        "name": "Electronic Frontier Foundation",
        "aliases": ["EFF"],
    },
    {
        "id": "ostp",
        "name": "Office of Science and Technology Policy",
        "aliases": ["OSTP", "White House OSTP"],
    },
    {
        "id": "nist",
        "name": "National Institute of Standards and Technology",
        "aliases": ["NIST"],
    },
    {
        "id": "nist_aisi",
        "name": "U.S. AI Safety Institute",
        "aliases": ["AISI", "AI Safety Institute"],
    },
    {
        "id": "ftc",
        "name": "Federal Trade Commission",
        "aliases": ["FTC", "the Commission"],
    },
]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def _amain() -> int:
    print("=== AI Policy Radar — enrichment smoke test ===")
    print(f"Activity: {SYNTHETIC_ACTIVITY['title']!r}")
    print(f"Entity directory size: {len(SYNTHETIC_ENTITY_DIRECTORY)}")
    print()

    pipeline = EnrichmentPipeline()
    print(f"Model: {pipeline._model}")
    print(f"Topic vocab loaded: {len(pipeline._topics)} topics")
    print()

    try:
        result = await pipeline.enrich_activity(
            SYNTHETIC_ACTIVITY, entity_directory=SYNTHETIC_ENTITY_DIRECTORY
        )
    except EnrichmentError as e:
        print(f"[FAIL] EnrichmentError: {e}")
        return 1

    print("=== enrichment result ===")
    print(json.dumps(result, indent=2, default=str))
    print()

    # Cache stats
    print("=== cache / usage stats (last call) ===")
    if pipeline.last_usage:
        print(json.dumps(pipeline.last_usage, indent=2))
        cw = pipeline.last_usage.get("cache_creation_input_tokens", 0)
        cr = pipeline.last_usage.get("cache_read_input_tokens", 0)
        if cw and not cr:
            print("(first call — cache was just created; rerun to see cache hits)")
        elif cr:
            print(f"(cache hit — {cr} tokens served from cache)")
    else:
        print("(no usage data captured)")
    print()

    # Light sanity assertions to make this self-checking
    assert result["summary"], "summary is empty"
    assert isinstance(result["topics"], list), "topics is not a list"
    assert len(result["topics"]) <= 5, "topics > 5"
    valid_topic_ids = pipeline._topic_ids
    for t in result["topics"]:
        assert t in valid_topic_ids, f"topic {t!r} not in vocab"
    valid_eids = {e["id"] for e in SYNTHETIC_ENTITY_DIRECTORY}
    for eid in result["mentioned_entities"]:
        assert eid in valid_eids, f"mentioned_entity {eid!r} not in directory"
        assert eid != SYNTHETIC_ACTIVITY["entity_id"], "source entity in mentioned_entities"
    if result["stance"] is not None:
        assert result["stance"] in {"supports", "opposes", "neutral", "mixed"}, (
            f"invalid stance {result['stance']!r}"
        )
        assert result["stance_quote"], "stance_quote required when stance non-null"
        # Substring check
        import re

        norm_q = re.sub(r"\s+", " ", result["stance_quote"]).strip().lower()
        norm_t = re.sub(r"\s+", " ", SYNTHETIC_RAW_TEXT).strip().lower()
        assert norm_q in norm_t, (
            f"stance_quote {result['stance_quote']!r} not a substring of raw_text"
        )

    mat = result["materiality"]
    assert mat["scope"] in {"federal", "state", "local", "international", "sector"}
    assert mat["bindingness"] in {"rule", "guidance", "enforcement", "statement", "proposal"}
    assert mat["novelty"] in {"new_position", "restated", "escalation", "reversal"}
    assert 0.0 <= mat["confidence"] <= 1.0

    print("[OK] all sanity checks passed.")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
