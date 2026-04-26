"""End-to-end smoke test for the Federal Register structured adapter (TODO 2.5).

Hits the keyless public Federal Register API. Targets `nist-aisi`
(executive_agency) for the last 180 days, fetches up to 5 documents in detail,
and pretty-prints one full Activity dict.

Usage:
    PYTHONPATH=backend uv run python backend/scripts/test_federal_register.py
"""
from __future__ import annotations

import dataclasses
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pprint import pformat

from radar.adapters.base import EntityRef
from radar.adapters.structured.federal_register import FederalRegisterAdapter
from radar.db.payload_schemas import validate_payload


def _activity_to_dict(act) -> dict:
    d = dataclasses.asdict(act)
    if hasattr(act.occurred_at, "isoformat"):
        d["occurred_at"] = act.occurred_at.isoformat()
    return d


def _run_for(adapter: FederalRegisterAdapter, entity: EntityRef, since: date, fetch_cap: int = 5):
    print("=" * 72)
    print(f"Federal Register adapter smoke test — entity={entity.id} ({entity.name})")
    print(f"Window: {since.isoformat()} to {date.today().isoformat()}")
    print("=" * 72)

    stubs = adapter.discover(entity, since)
    print(f"\nDiscovered: {len(stubs)} stubs")
    for i, s in enumerate(stubs[:10], 1):
        occ = s.occurred_at.isoformat() if hasattr(s.occurred_at, "isoformat") else str(s.occurred_at)
        print(f"  {i}. [{occ}] [{s.activity_type}] {(s.title or '')[:80]}")

    fetched = []
    for s in stubs[:fetch_cap]:
        act = adapter.fetch(entity, s)
        if act is not None:
            fetched.append(act)

    print(f"\nFetched (cap={fetch_cap}): {len(fetched)} activities")
    for i, a in enumerate(fetched, 1):
        occ = a.occurred_at.isoformat() if hasattr(a.occurred_at, "isoformat") else str(a.occurred_at)
        print(f"  {i}. [{occ}] [{a.activity_type}] {(a.title or '')[:80]}")

    by_type = Counter(a.activity_type for a in fetched)
    print(f"\nDistribution by activity_type: {dict(by_type)}")

    return stubs, fetched


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    nist = EntityRef(
        id="nist-aisi",
        name="NIST US AI Safety Institute",
        entity_type="executive_agency",
        subcategory="standards_body",
        jurisdiction="US-Federal",
        aliases=[
            "NIST",
            "National Institute of Standards and Technology",
            "AISI",
            "US AISI",
            "NIST AI Safety Institute",
        ],
        metadata={"parent_dept": "Department of Commerce"},
    )

    bis = EntityRef(
        id="bis",
        name="Bureau of Industry and Security",
        entity_type="executive_agency",
        subcategory="regulator",
        jurisdiction="US-Federal",
        aliases=["BIS", "Commerce BIS", "Department of Commerce BIS", "Industry and Security Bureau"],
        metadata={"parent_dept": "Department of Commerce"},
    )

    today = date.today()
    since = today - timedelta(days=180)

    adapter = FederalRegisterAdapter(http_timeout=20.0)
    try:
        nist_stubs, nist_fetched = _run_for(adapter, nist, since, fetch_cap=5)

        # Per instructions: try BIS too. NIST/AISI publishes few FR docs
        # under its own slug because most NIST FR notices file under the
        # parent "national-institute-of-standards-and-technology" or
        # "commerce-department" agencies; BIS rounds out the smoke test.
        bis_stubs, bis_fetched = _run_for(adapter, bis, since, fetch_cap=5)

        # Combine for aggregate reporting.
        stubs = nist_stubs + bis_stubs
        fetched = nist_fetched + bis_fetched

        if not fetched:
            print("\nNo fetched activities for either entity in the window.")
            return 1

        # Re-validate one payload through validate_payload as a hand-check.
        sample = None
        # Prefer the most recent NPRM or FINAL_RULE for the showcase.
        for a in sorted(fetched, key=lambda x: str(x.occurred_at), reverse=True):
            if a.activity_type in ("NPRM", "FINAL_RULE"):
                sample = a
                break
        if sample is None:
            sample = sorted(fetched, key=lambda x: str(x.occurred_at), reverse=True)[0]

        print("\n--- Re-validating sample payload through validate_payload() ---")
        try:
            validated = validate_payload(sample.activity_type, sample.payload)
            print(f"  OK: {sample.activity_type} payload validates "
                  f"(model={type(validated).__name__})")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {exc}")
            return 2

        print("\n--- One full Activity (pretty-printed) ---")
        d = _activity_to_dict(sample)
        print(pformat(d, width=110, sort_dicts=False))

        # Aggregate summary
        all_types = Counter(a.activity_type for a in fetched)
        print("\n--- Aggregate ---")
        print(f"  total stubs discovered: {len(stubs)}")
        print(f"  total fetched: {len(fetched)}")
        print(f"  by activity_type: {dict(all_types)}")
        print(f"  example URL: {sample.source_url}")

        if len(fetched) < 3:
            print("\nWARN: fewer than 3 fetched activities for both entities.")
            return 3
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    sys.exit(main())
