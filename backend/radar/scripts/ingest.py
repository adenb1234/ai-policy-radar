"""`make ingest` entrypoint — wires adapters and the orchestrator.

Usage examples:
    PYTHONPATH=backend uv run python -m radar.scripts.ingest
    PYTHONPATH=backend uv run python -m radar.scripts.ingest --since 90 --entity-type civil_society
    PYTHONPATH=backend uv run python -m radar.scripts.ingest --entity-id eff --dry-run

Loads ANTHROPIC_API_KEY from the repo-root .env (same flow as radar/main.py)
so the research adapter can run without explicit env wiring.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Match radar/main.py — .env at the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

from radar.adapters.structured.federal_register import FederalRegisterAdapter  # noqa: E402
from radar.db.connection import bootstrap  # noqa: E402
from radar.ingest.orchestrator import IngestResult, ingest_all  # noqa: E402

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="radar.scripts.ingest",
        description="Run source adapters and ingest activities into SQLite.",
    )
    p.add_argument(
        "--since",
        type=int,
        default=180,
        help="Days back from today to ingest (default: 180).",
    )
    p.add_argument(
        "--entity-type",
        action="append",
        default=[],
        help="Restrict to one or more entity_types (repeatable).",
    )
    p.add_argument(
        "--entity-id",
        action="append",
        default=[],
        help="Restrict to specific entity ids (repeatable). Overrides --entity-type.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discover/fetch/verify but do not write to the DB.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N entities total (after filtering).",
    )
    p.add_argument(
        "--no-research",
        action="store_true",
        help="Skip the research adapter even when ANTHROPIC_API_KEY is set.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return p.parse_args(argv)


def _build_research_adapter(disable: bool):
    if disable:
        log.info("[ingest] research adapter disabled by --no-research")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "[ingest] ANTHROPIC_API_KEY not set; research adapter disabled. "
            "Structured adapters will still run."
        )
        return None
    # Imported lazily so a missing key / import-time failure doesn't kill
    # structured-only runs.
    from radar.adapters.research.per_entity import PerEntityResearchAdapter

    return PerEntityResearchAdapter()


def _print_summary(results: list[IngestResult]) -> None:
    if not results:
        print("[ingest] no entities matched filters; nothing ingested.")
        return

    headers = ("entity", "adapter", "disc", "verif", "new", "upd", "fail")
    rows = [
        (
            r.entity_id,
            r.adapter,
            str(r.discovered),
            str(r.verified),
            str(r.persisted_new),
            str(r.persisted_updated),
            str(len(r.failures)),
        )
        for r in results
    ]
    widths = [
        max(len(h), max((len(row[i]) for row in rows), default=0))
        for i, h in enumerate(headers)
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))

    print()
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))

    totals = {
        "discovered": sum(r.discovered for r in results),
        "verified": sum(r.verified for r in results),
        "new": sum(r.persisted_new for r in results),
        "updated": sum(r.persisted_updated for r in results),
        "failures": sum(len(r.failures) for r in results),
    }
    print()
    print(
        f"[ingest] totals: discovered={totals['discovered']} "
        f"verified={totals['verified']} new={totals['new']} "
        f"updated={totals['updated']} failures={totals['failures']}"
    )

    # Echo failure reasons compactly so demo runs surface bad URLs / drops.
    any_fail = [r for r in results if r.failures]
    if any_fail:
        print()
        print("[ingest] failures by entity:")
        for r in any_fail:
            for reason in r.failures[:5]:
                print(f"  {r.entity_id}: {reason}")
            if len(r.failures) > 5:
                print(f"  {r.entity_id}: ... ({len(r.failures) - 5} more)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    since = date.today() - timedelta(days=args.since)
    only_types = set(args.entity_type) or None
    only_ids = set(args.entity_id) or None

    conn = bootstrap()

    research = _build_research_adapter(disable=args.no_research)
    structured = [FederalRegisterAdapter()]

    print(
        f"[ingest] since={since.isoformat()} "
        f"types={sorted(only_types) if only_types else 'ALL'} "
        f"ids={sorted(only_ids) if only_ids else 'ALL'} "
        f"dry_run={args.dry_run} research={'on' if research else 'off'}"
    )

    try:
        results = ingest_all(
            conn,
            since=since,
            research_adapter=research,
            structured_adapters=structured,
            only_entity_types=only_types,
            only_entity_ids=only_ids,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        for adapter in structured:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    _print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
