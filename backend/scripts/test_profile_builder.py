"""Smoke test for the profile builder (TODO 4.5).

Runs against the real Anthropic API. Builds three structured profiles from
distinct natural-language descriptions and pretty-prints each one. Useful
hand-check signals are noted below the script's output.

Usage:
    PYTHONPATH=backend uv run python backend/scripts/test_profile_builder.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from pprint import pformat

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

from radar.profiles.builder import ProfileBuilder  # noqa: E402


CASES: list[tuple[str, str]] = [
    (
        "frontier-lab",
        "I'm the policy lead at a frontier AI lab. I care about export controls, "
        "compute thresholds, model release policy, EU AI Act implementing acts, "
        "and US federal preemption of state AI laws.",
    ),
    (
        "state-ag",
        "California Attorney General's office. We need to track AI consumer protection "
        "cases, AI bias audit enforcement, deepfake election laws, and major federal "
        "preemption fights since CA SB1047.",
    ),
    (
        "healthcare-startup",
        "I run a healthcare AI startup. Tell me about FDA AI/ML regulations, clinical "
        "decision support guidance, and reimbursement / Medicare AI policy.",
    ),
]


async def _build_one(builder: ProfileBuilder, label: str, nl: str) -> None:
    print("=" * 72)
    print(f"CASE: {label}")
    print("-" * 72)
    print(f"NL: {nl}")
    print("-" * 72)

    profile = await builder.build(nl)

    d = asdict(profile)
    print(pformat(d, width=110, sort_dicts=False))

    if builder.last_usage:
        u = builder.last_usage
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

    # Round-trip sanity (also exercised by from_db_json on its own).
    blob = builder.to_db_json(profile)
    rt = builder.from_db_json(blob)
    assert rt == profile, "to_db_json / from_db_json round-trip failed"
    print("Round-trip: OK")


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    builder = ProfileBuilder()
    print(f"Loaded vocab: {len(builder._valid_topic_ids)} topics")  # type: ignore[attr-defined]
    print(f"Loaded entity directory: {len(builder._valid_entity_ids)} entities")  # type: ignore[attr-defined]

    for label, nl in CASES:
        try:
            await _build_one(builder, label, nl)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL [{label}]: {type(e).__name__}: {e}")
            return 1
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
