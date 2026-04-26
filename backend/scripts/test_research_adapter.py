"""End-to-end smoke test for the per-entity research adapter (TODO 2.8).

Runs against the real Anthropic API. Targets EFF (entity_type=civil_society)
for the last 60 days, then runs URL verification on the discovered set.

Prints:
  - count discovered
  - count after URL verification (passed / failed)
  - per-failure reason summary
  - one full Activity dict pretty-printed

Usage:
    PYTHONPATH=backend uv run python backend/scripts/test_research_adapter.py
"""
from __future__ import annotations

import dataclasses
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from pprint import pformat

from dotenv import load_dotenv

# Load .env from repo root (same pattern as radar/main.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

from radar.adapters.base import EntityRef  # noqa: E402
from radar.adapters.research.per_entity import PerEntityResearchAdapter  # noqa: E402
from radar.adapters.research.url_verify import verify_batch  # noqa: E402


def _activity_to_dict(act) -> dict:
    d = dataclasses.asdict(act)
    # Render dates as ISO strings for readability.
    if hasattr(act.occurred_at, "isoformat"):
        d["occurred_at"] = act.occurred_at.isoformat()
    return d


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    eff = EntityRef(
        id="eff",
        name="Electronic Frontier Foundation",
        entity_type="civil_society",
        subcategory="advocacy_ngo",
        jurisdiction="US-Federal",
        aliases=["EFF"],
        metadata={"ideology_axis": "civil_libertarian"},
    )

    today = date.today()
    since = today - timedelta(days=60)

    print("=" * 72)
    print(f"Research adapter smoke test — entity={eff.id} ({eff.name})")
    print(f"Window: {since.isoformat()} to {today.isoformat()}")
    print("=" * 72)

    adapter = PerEntityResearchAdapter(max_results=8)
    activities = adapter.discover_and_fetch(eff, since)

    print(f"\nDiscovered: {len(activities)} activities")
    if adapter.last_usage:
        u = adapter.last_usage
        total = (
            u["input_tokens"]
            + u["output_tokens"]
            + u["cache_creation_input_tokens"]
            + u["cache_read_input_tokens"]
        )
        print(
            f"Tokens — input={u['input_tokens']} output={u['output_tokens']} "
            f"cache_create={u['cache_creation_input_tokens']} "
            f"cache_read={u['cache_read_input_tokens']} total={total}"
        )

    if not activities:
        print("\nNo activities discovered. Nothing to verify.")
        return 1

    print("\nDiscovered titles + URLs:")
    for i, act in enumerate(activities, 1):
        occ = (
            act.occurred_at.isoformat()
            if hasattr(act.occurred_at, "isoformat")
            else str(act.occurred_at)
        )
        print(f"  {i}. [{occ}] [{act.activity_type}] {act.title}")
        print(f"       {act.source_url}")

    print("\nRunning URL verification...")
    passed, failed = verify_batch(activities, timeout=20.0)
    print(f"  passed: {len(passed)}")
    print(f"  failed: {len(failed)}")

    if failed:
        reason_counts = Counter(reason for _, reason in failed)
        print("\nFailure reasons:")
        for reason, n in reason_counts.most_common():
            print(f"  {reason}: {n}")
        print("\nFailed items:")
        for act, reason in failed:
            print(f"  [{reason}] {act.title}")
            print(f"    {act.source_url}")

    if passed:
        print("\n--- One full verified Activity (pretty-printed) ---")
        sample = passed[0]
        d = _activity_to_dict(sample)
        print(pformat(d, width=110, sort_dicts=False))

        # Sanity check: does the recorded raw_text contain the verify_phrase?
        verify_phrases = sample.payload.get("_verify_phrases") or []
        if verify_phrases:
            phrase = verify_phrases[0]
            in_excerpt = phrase.lower() in (sample.raw_text or "").lower()
            print(
                f"\nSanity: verify_phrase {'found in' if in_excerpt else 'NOT in'} "
                f"raw_text excerpt (it must appear on the source page; the excerpt "
                f"is a separate copy and may or may not contain the phrase)."
            )

    if not passed:
        print("\nNo activities passed URL verification.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
