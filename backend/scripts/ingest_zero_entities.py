"""One-off ingestion script for entities currently at 0 activities.

The dev-tier 30K-input-tokens/min Anthropic cap caused the main ingestion run
to give up early on a number of entities. This script:

1. Queries the DB for every entity that currently has 0 rows in `activity`.
2. Iterates them sequentially, with a deliberate `--sleep` second pause
   BETWEEN each Anthropic call (default 40s — slightly above 30K/(8K max
   tokens per call) ≈ 16s, but with headroom for cache_creation tokens and
   web_search response tokens which also count against the per-minute cap).
3. Runs `PerEntityResearchAdapter.discover_and_fetch(entity, since=today-90d)`
   via the existing `ingest_entity` orchestrator helper — that gives us URL
   verification (§7.4) and persistence for free, and ensures behavior matches
   the production path exactly.
4. Logs per-entity counts (discovered / verified / new / failures).

Usage:
    PYTHONPATH=backend uv run python backend/scripts/ingest_zero_entities.py
    PYTHONPATH=backend uv run python backend/scripts/ingest_zero_entities.py --sleep 60
    PYTHONPATH=backend uv run python backend/scripts/ingest_zero_entities.py --since 90 --only sen-schumer,openai
    PYTHONPATH=backend uv run python backend/scripts/ingest_zero_entities.py --skip heritage,sen-cruz

Loads ANTHROPIC_API_KEY from the repo-root .env, never echoes it.

This script does NOT modify the orchestrator's main flow. It re-uses
`ingest_entity` and the production `PerEntityResearchAdapter` — the only
difference from a normal `make ingest` run is the upfront sleep between
entities.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (same flow as radar/main.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

# Make `backend/` importable so `radar.*` resolves when this script is run by
# path rather than via -m. (Tests in this directory do the same.)
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from radar.db.connection import bootstrap, connect  # noqa: E402
from radar.db.storage import list_entities_with_aliases  # noqa: E402
from radar.ingest.orchestrator import ingest_entity  # noqa: E402

log = logging.getLogger("ingest_zero_entities")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ingest_zero_entities",
        description="Re-ingest only entities currently at 0 activities, "
        "with deliberate pacing for the dev-tier rate limit.",
    )
    p.add_argument(
        "--since",
        type=int,
        default=90,
        help="Days back from today (default: 90).",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=40.0,
        help="Seconds to sleep BEFORE each Anthropic call after the first "
        "(default: 40 — tuned for 30K input-tokens/min dev-tier cap).",
    )
    p.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated entity_ids to restrict to (still must be at 0).",
    )
    p.add_argument(
        "--skip",
        type=str,
        default=None,
        help="Comma-separated entity_ids to skip.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discover/fetch/verify but do not write to the DB.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
    )
    return p.parse_args(argv)


def _load_zero_entity_ids(conn) -> list[str]:
    cur = conn.execute(
        """
        SELECT e.id
          FROM entity e
          LEFT JOIN activity a ON a.entity_id = e.id
         WHERE a.id IS NULL
         ORDER BY e.id
        """
    )
    return [row[0] for row in cur.fetchall()]


# Priority order matches the brief — frontier-lab profile demo first, then
# named research/grantmakers, then international, then catch-all / lowest-yield.
_PRIORITY: list[str] = [
    # HIGHEST
    "openai",
    "microsoft",
    "nvidia",
    "google-deepmind",
    "apple",
    "xai",
    # HIGH
    "mitre",
    "open-philanthropy",
    "coefficient-giving",
    "cais",
    # MEDIUM
    "france-macron",
    "japan-meti",
    "oecd-ai",
    "uk-aisi",
    # LOW
    "heritage",
    "sen-schumer",
    "sen-young",
    "sen-cantwell",
    "sen-cruz",
    "sen-lujan",
    "rep-khanna",
    "senate-ai-caucus",
    "house-energy-commerce",
    "us-congress",
    "scotus",
    "ndca",
    "cafc",
    "ostp",
    "ca-ag",
    "ca-legislature",
    "ny-ag",
    "tx-ag",
    "nyc",
    # factions are member-rollups; per_entity adapter still attempts them
    "faction-ai-safety-dem",
    "faction-libertarian-chamber-r",
    "faction-maga-tech-skeptic",
    "faction-populist-dem",
    "tsmc",
]


def _ordered(entity_ids: list[str]) -> list[str]:
    """Sort by priority list; unknown ids go to the end (alpha)."""
    rank = {eid: i for i, eid in enumerate(_PRIORITY)}
    in_priority = sorted(
        [e for e in entity_ids if e in rank], key=lambda e: rank[e]
    )
    rest = sorted([e for e in entity_ids if e not in rank])
    return in_priority + rest


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error(
            "ANTHROPIC_API_KEY is not set; this script cannot run. "
            "Populate .env at the repo root."
        )
        return 1

    # Lazy import so a missing key surfaces above before we touch anthropic.
    from radar.adapters.research.per_entity import PerEntityResearchAdapter

    # Schema is already bootstrapped by the running backend / earlier ingest;
    # use connect() to avoid taking the write lock from init_schema while a
    # uvicorn process or another tool may be touching the DB.
    conn = connect()

    zero_ids = _load_zero_entity_ids(conn)
    if not zero_ids:
        print("[zero-ingest] no entities at 0 activities — nothing to do.")
        return 0

    only = (
        {x.strip() for x in args.only.split(",") if x.strip()}
        if args.only
        else None
    )
    skip = (
        {x.strip() for x in args.skip.split(",") if x.strip()}
        if args.skip
        else set()
    )

    target_ids = [eid for eid in zero_ids if eid not in skip]
    if only is not None:
        target_ids = [eid for eid in target_ids if eid in only]
    target_ids = _ordered(target_ids)

    if not target_ids:
        print("[zero-ingest] no entities matched filters; nothing to do.")
        return 0

    # Resolve EntityRef rows for the targets.
    by_id = {e.id: e for e in list_entities_with_aliases(conn)}
    entities = [by_id[eid] for eid in target_ids if eid in by_id]

    since = date.today() - timedelta(days=args.since)
    print(
        f"[zero-ingest] {len(entities)} entities at 0 activities | "
        f"since={since.isoformat()} sleep={args.sleep}s dry_run={args.dry_run}"
    )
    print("[zero-ingest] order: " + ", ".join(e.id for e in entities))

    research = PerEntityResearchAdapter()

    summary_rows: list[tuple[str, str, int, int, int, int]] = []  # (id, type, disc, ver, new, fail)
    total_new = 0

    for idx, entity in enumerate(entities):
        if idx > 0:
            log.info(
                "[zero-ingest] sleeping %.1fs before next call (rate-limit pacing)…",
                args.sleep,
            )
            time.sleep(args.sleep)

        log.info(
            "[zero-ingest] %d/%d entity=%s type=%s",
            idx + 1,
            len(entities),
            entity.id,
            entity.entity_type,
        )

        try:
            result = ingest_entity(
                conn,
                entity,
                since=since,
                research_adapter=research,
                structured_adapters=None,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "[zero-ingest] %s: orchestrator raised %s — continuing",
                entity.id,
                type(exc).__name__,
            )
            summary_rows.append((entity.id, entity.entity_type, 0, 0, 0, 1))
            continue

        if not args.dry_run:
            conn.commit()

        summary_rows.append(
            (
                entity.id,
                entity.entity_type,
                result.discovered,
                result.verified,
                result.persisted_new,
                len(result.failures),
            )
        )
        total_new += result.persisted_new

        log.info(
            "[zero-ingest] %s: discovered=%d verified=%d new=%d failures=%d",
            entity.id,
            result.discovered,
            result.verified,
            result.persisted_new,
            len(result.failures),
        )
        if result.failures:
            for f in result.failures[:5]:
                log.info("[zero-ingest] %s   reason: %s", entity.id, f)
            if len(result.failures) > 5:
                log.info(
                    "[zero-ingest] %s   reason: ... %d more",
                    entity.id,
                    len(result.failures) - 5,
                )

    # ---- summary table -----------------------------------------------------
    print()
    print("[zero-ingest] per-entity result table")
    headers = ("entity", "type", "disc", "verif", "new", "fail")
    rows = [(eid, et, str(d), str(v), str(n), str(f)) for eid, et, d, v, n, f in summary_rows]
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]

    def fmt(row):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))

    nonzero = sum(1 for r in summary_rows if r[4] > 0)
    print()
    print(
        f"[zero-ingest] {nonzero}/{len(summary_rows)} entities now have >=1 activity. "
        f"total persisted_new={total_new}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
