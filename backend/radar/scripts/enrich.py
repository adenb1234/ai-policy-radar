"""`make enrich` entrypoint — runs the EnrichmentPipeline over activities.

Usage:
    PYTHONPATH=backend uv run python -m radar.scripts.enrich
    PYTHONPATH=backend uv run python -m radar.scripts.enrich --limit 50
    PYTHONPATH=backend uv run python -m radar.scripts.enrich --activity-id abc123 --activity-id def456
    PYTHONPATH=backend uv run python -m radar.scripts.enrich --reenrich --limit 10
    PYTHONPATH=backend uv run python -m radar.scripts.enrich --dry-run

Loads ANTHROPIC_API_KEY from the repo-root .env (same flow as the ingest
script and radar/main.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

from radar.db.connection import bootstrap  # noqa: E402
from radar.enrich.pipeline import EnrichmentError, EnrichmentPipeline  # noqa: E402

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="radar.scripts.enrich",
        description="Run the enrichment pipeline over activities in the DB.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Process at most N activities (default: 100).",
    )
    p.add_argument(
        "--activity-id",
        action="append",
        default=[],
        help="Restrict to specific activity ids (repeatable).",
    )
    p.add_argument(
        "--reenrich",
        action="store_true",
        help="Reprocess activities that already have an enrichment row.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run enrichment but do not write to the DB.",
    )
    p.add_argument(
        "--max-concurrent",
        type=int,
        default=6,
        help="Max concurrent in-flight enrichments (default: 6).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return p.parse_args(argv)


def _select_activities(conn, *, ids: list[str], reenrich: bool, limit: int) -> list[dict]:
    """Pull activity rows from SQLite. Returns list of dicts."""
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql = f"SELECT * FROM activity WHERE id IN ({placeholders}) ORDER BY occurred_at DESC LIMIT ?"
        rows = conn.execute(sql, (*ids, limit)).fetchall()
    elif reenrich:
        rows = conn.execute(
            "SELECT * FROM activity ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.* FROM activity a
            LEFT JOIN enrichment e ON e.activity_id = a.id
            WHERE e.activity_id IS NULL
            ORDER BY a.occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _select_entity_directory(conn) -> list[dict]:
    """Build entity_directory: id, name, aliases."""
    rows = conn.execute(
        "SELECT id, name, aliases FROM entity ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            aliases = json.loads(r["aliases"]) if r["aliases"] else []
            if not isinstance(aliases, list):
                aliases = []
        except (TypeError, ValueError):
            aliases = []
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "aliases": [str(a) for a in aliases],
            }
        )
    return out


def _insert_enrichment(conn, activity_id: str, result: dict) -> None:
    conn.execute(
        """
        INSERT INTO enrichment (
            activity_id, summary, topics, mentioned_entities,
            stance, stance_quote, materiality, enriched_at, enricher_model
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(activity_id) DO UPDATE SET
            summary            = excluded.summary,
            topics             = excluded.topics,
            mentioned_entities = excluded.mentioned_entities,
            stance             = excluded.stance,
            stance_quote       = excluded.stance_quote,
            materiality        = excluded.materiality,
            enriched_at        = excluded.enriched_at,
            enricher_model     = excluded.enricher_model
        """,
        (
            activity_id,
            result["summary"],
            json.dumps(result.get("topics") or []),
            json.dumps(result.get("mentioned_entities") or []),
            result.get("stance"),
            result.get("stance_quote"),
            json.dumps(result.get("materiality") or {}),
            result["enriched_at"],
            result["enricher_model"],
        ),
    )


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = bootstrap()
    try:
        activities = _select_activities(
            conn, ids=args.activity_id, reenrich=args.reenrich, limit=args.limit
        )
        entity_directory = _select_entity_directory(conn)

        if not activities:
            print("[enrich] no activities to process (use --reenrich to reprocess existing).")
            return 0

        print(
            f"[enrich] activities={len(activities)} "
            f"entities={len(entity_directory)} "
            f"dry_run={args.dry_run} reenrich={args.reenrich}"
        )

        pipeline = EnrichmentPipeline(max_concurrent=args.max_concurrent)
        results = await pipeline.enrich_batch(
            activities, entity_directory=entity_directory
        )

        ok = 0
        errors = 0
        sample_ok: dict | None = None
        stance_counts: Counter[str] = Counter()
        for activity_id, res in results:
            if isinstance(res, EnrichmentError):
                errors += 1
                log.warning("[enrich] %s: %s", activity_id, res)
                continue
            ok += 1
            stance_counts[str(res.get("stance"))] += 1
            if sample_ok is None:
                sample_ok = {"activity_id": activity_id, **res}
            if not args.dry_run:
                _insert_enrichment(conn, activity_id, res)
        if not args.dry_run:
            conn.commit()

        print()
        print("=== summary ===")
        print(f"  ok:     {ok}")
        print(f"  errors: {errors}")
        if stance_counts:
            print("  stance distribution:")
            for s, c in stance_counts.most_common():
                print(f"    {s}: {c}")
        if sample_ok is not None:
            print()
            print("=== sample successful enrichment ===")
            print(json.dumps(sample_ok, indent=2, default=str))

        # Print pipeline cache stats from the most recent call (best-effort).
        if pipeline.last_usage:
            print()
            print("=== last-call usage ===")
            print(json.dumps(pipeline.last_usage, indent=2))

        return 0 if errors == 0 else 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
