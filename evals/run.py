"""Eval runner (BUILD_PLAN §12.2).

For each case in `evals/cases.yaml`:
  - Build a `StructuredProfile` from `profile.nl` (LLM extraction via
    `ProfileBuilder`) and merge `profile.structured_overrides`.
  - Persist the profile to a TEMPORARY row in `user_profile`.
  - Call `AwarenessEngine.build_dashboard(profile_id, since=today-180d, top_k)`.
  - Capture the full hydrated dashboard (activity + enrichment + entity +
    awareness block per item).
  - Delete the temp profile row + its `awareness_item` rows.

Outputs:
  - `evals/runs/<UTC-timestamp>/<case_id>.json`
  - then invokes `evals.judge.judge_runs(<run_dir>)` which writes
    `evals/report.md`.

CLI:
  PYTHONPATH=backend uv run python -m evals.run
  PYTHONPATH=backend uv run python -m evals.run --case-id case_frontier_lab_policy
  PYTHONPATH=backend uv run python -m evals.run --top-k 10 --limit 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Make the backend `radar.*` package importable when run as `python -m evals.run`
# from the repo root. We don't rely on PYTHONPATH being set externally — the
# Makefile sets it but direct invocation should also work.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Load .env so ANTHROPIC_API_KEY is available before any module reads it.
load_dotenv(REPO_ROOT / ".env")

from radar.awareness.engine import AwarenessEngine  # noqa: E402
from radar.db.connection import bootstrap  # noqa: E402
from radar.profiles.builder import (  # noqa: E402
    ProfileBuilder,
    StructuredProfile,
    insert_profile,
)

log = logging.getLogger("evals.run")

CASES_PATH = REPO_ROOT / "evals" / "cases.yaml"
RUNS_DIR = REPO_ROOT / "evals" / "runs"


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"cases file not found: {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a YAML list of cases")
    cases: list[dict] = []
    for elem in raw:
        if not isinstance(elem, dict):
            continue
        if "id" not in elem or "profile" not in elem:
            continue
        cases.append(elem)
    return cases


def filter_cases(
    cases: list[dict],
    *,
    case_ids: list[str] | None,
    limit: int | None,
) -> list[dict]:
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.get("id") in wanted]
    if limit is not None and limit >= 0:
        cases = cases[:limit]
    return cases


# ---------------------------------------------------------------------------
# Activity / enrichment serialization (mirrors the API hydration shape)
# ---------------------------------------------------------------------------


def _safe_json_loads(blob: Any, default: Any) -> Any:
    if blob is None:
        return default
    if isinstance(blob, (dict, list)):
        return blob
    if not isinstance(blob, str):
        return default
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return default


def _hydrate_items_for_run(
    db: sqlite3.Connection,
    items: list,
) -> list[dict]:
    """Hydrate awareness items into plain dicts matching the API response.

    Mirrors `radar.api.routes._hydrate_awareness_items` but emits dicts (not
    pydantic models) so the JSON we write to disk has stable field names
    without any pydantic-version coupling.
    """
    if not items:
        return []
    ids = [it.activity_id for it in items]
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT a.id, a.entity_id, a.entity_type, a.activity_type, a.occurred_at,
               a.ingested_at, a.source_url, a.source_adapter, a.title, a.raw_text,
               a.payload, a.url_verified_at,
               e.id AS e_id, e.name AS e_name, e.entity_type AS e_entity_type,
               e.subcategory AS e_subcategory, e.jurisdiction AS e_jurisdiction,
               e.description AS e_description, e.aliases AS e_aliases,
               en.activity_id AS en_activity_id,
               en.summary, en.topics, en.mentioned_entities,
               en.stance, en.stance_quote, en.materiality,
               en.enriched_at, en.enricher_model
        FROM activity a
        JOIN entity e ON e.id = a.entity_id
        LEFT JOIN enrichment en ON en.activity_id = a.id
        WHERE a.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}

    out: list[dict] = []
    for it in items:
        r = by_id.get(it.activity_id)
        if r is None:
            activity = {
                "id": it.activity_id,
                "entity_id": "",
                "entity_type": "",
                "activity_type": "",
                "occurred_at": "",
                "ingested_at": "",
                "source_url": "",
                "source_adapter": "",
                "title": "(activity not found)",
                "raw_text": None,
                "payload": {},
                "url_verified_at": None,
            }
            enrichment: dict | None = None
            source_entity: dict | None = None
        else:
            activity = {
                "id": r["id"],
                "entity_id": r["entity_id"],
                "entity_type": r["entity_type"],
                "activity_type": r["activity_type"],
                "occurred_at": r["occurred_at"],
                "ingested_at": r["ingested_at"],
                "source_url": r["source_url"],
                "source_adapter": r["source_adapter"],
                "title": r["title"],
                "raw_text": r["raw_text"],
                "payload": _safe_json_loads(r["payload"], {}),
                "url_verified_at": r["url_verified_at"],
            }
            enrichment = None
            if r["en_activity_id"]:
                enrichment = {
                    "activity_id": r["en_activity_id"],
                    "summary": r["summary"],
                    "topics": _safe_json_loads(r["topics"], []),
                    "mentioned_entities": _safe_json_loads(
                        r["mentioned_entities"], []
                    ),
                    "stance": r["stance"],
                    "stance_quote": r["stance_quote"],
                    "materiality": _safe_json_loads(r["materiality"], {}),
                    "enriched_at": r["enriched_at"],
                    "enricher_model": r["enricher_model"],
                }
            ent_aliases = _safe_json_loads(r["e_aliases"], [])
            if not isinstance(ent_aliases, list):
                ent_aliases = []
            source_entity = {
                "id": r["e_id"],
                "name": r["e_name"],
                "entity_type": r["e_entity_type"],
                "subcategory": r["e_subcategory"],
                "jurisdiction": r["e_jurisdiction"],
                "description": r["e_description"],
                "aliases": [str(a) for a in ent_aliases],
            }
        out.append(
            {
                "activity": activity,
                "enrichment": enrichment,
                "source_entity": source_entity,
                "awareness": {
                    "relevance_score": float(it.relevance_score),
                    "reasoning": it.reasoning,
                    "recommended_actions": list(it.recommended_actions),
                    "citations": list(it.citations),
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# Per-case runner
# ---------------------------------------------------------------------------


async def run_case(
    case: dict,
    conn: sqlite3.Connection,
    *,
    top_k: int = 15,
    builder: ProfileBuilder | None = None,
    engine: AwarenessEngine | None = None,
) -> dict:
    """Build profile from case.profile.nl + overrides, persist temp profile,
    call AwarenessEngine.build_dashboard, capture the full dashboard, then
    clean up the temp profile.

    Returns:
      {
        "case_id": str,
        "generated_at": ISO8601 UTC,
        "profile_structured": dict,   # asdict(StructuredProfile)
        "profile_nl": str,
        "dashboard": {
          "items": [<hydrated AwarenessItem>],
          "diagnostics": {layer1_count, layer2_count, layer3_count, since},
        },
      }
    """
    case_id = str(case["id"])
    profile_block = case.get("profile") or {}
    nl = (profile_block.get("nl") or "").strip()
    if not nl:
        raise ValueError(f"case {case_id!r} has no profile.nl")
    overrides = profile_block.get("structured_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    builder = builder or ProfileBuilder()
    engine = engine or AwarenessEngine()

    # 1. Extract structured profile from NL + apply form overrides.
    structured: StructuredProfile = await builder.build(
        nl, structured_overrides=overrides
    )

    # 2. Persist as a temp row so AwarenessEngine.build_dashboard can load it.
    profile_id = insert_profile(
        conn,
        name=f"[eval] {case_id}",
        nl_description=nl,
        structured=structured,
    )
    log.info("[eval.run] case=%s temp profile_id=%s", case_id, profile_id)

    try:
        # 3. Run the dashboard. Use a 180-day window so eval coverage is
        #    independent of the structured `recency_days` field (which steers
        #    structured retrieval but shouldn't gate the eval window).
        since_date: date = date.today() - timedelta(days=180)
        items = await engine.build_dashboard(
            conn, profile_id, since=since_date, top_k=top_k
        )
        diagnostics = dict(engine.last_diagnostics or {})

        hydrated = _hydrate_items_for_run(conn, items)
    finally:
        # 4. Cleanup. Drop awareness_item rows first (FK), then user_profile.
        try:
            conn.execute(
                "DELETE FROM awareness_item WHERE user_id = ?", (profile_id,)
            )
            conn.execute(
                "DELETE FROM user_profile WHERE id = ?", (profile_id,)
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning(
                "[eval.run] cleanup failed for profile %s: %s", profile_id, exc
            )

    return {
        "case_id": case_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_k": top_k,
        "profile_nl": nl,
        "profile_structured": asdict(structured),
        "expected_top_activities": list(case.get("expected_top_activities") or []),
        "expected_topics_surfaced": list(case.get("expected_topics_surfaced") or []),
        "expected_action_themes": list(case.get("expected_action_themes") or []),
        "dashboard": {
            "items": hydrated,
            "diagnostics": diagnostics,
        },
    }


# ---------------------------------------------------------------------------
# Per-case summary printer
# ---------------------------------------------------------------------------


def _print_case_summary(result: dict) -> None:
    items = result.get("dashboard", {}).get("items", []) or []
    diag = result.get("dashboard", {}).get("diagnostics", {}) or {}
    case_id = result.get("case_id", "?")
    print(
        f"  [{case_id}] items={len(items)} "
        f"L1={diag.get('layer1_count','?')} "
        f"L2={diag.get('layer2_count','?')} "
        f"L3={diag.get('layer3_count','?')}"
    )
    for it in items[:3]:
        a = it.get("activity") or {}
        aw = it.get("awareness") or {}
        title = (a.get("title") or "")[:80]
        print(
            f"      - {a.get('id','?')[:12]} "
            f"score={aw.get('relevance_score',0):.1f}  {title}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _utc_run_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / ts
    out.mkdir(parents=True, exist_ok=True)
    return out


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Policy Radar eval runner")
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Run only this case_id. Repeatable.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=15,
        help="Number of awareness items per dashboard (default 15).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of cases run (after --case-id filter).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the judge step (just produce run JSON).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cases = load_cases()
    cases = filter_cases(cases, case_ids=args.case_id, limit=args.limit)
    if not cases:
        print("no cases matched the filters; nothing to do.")
        return 1

    run_dir = _utc_run_dir()
    print(f"eval run dir: {run_dir.relative_to(REPO_ROOT)}")
    print(f"cases: {len(cases)}  top_k: {args.top_k}")

    # Bootstrap DB + shared LLM clients.
    conn = bootstrap()
    builder = ProfileBuilder()
    engine = AwarenessEngine()

    try:
        for case in cases:
            cid = case.get("id", "?")
            print(f"\n→ running {cid}")
            try:
                result = await run_case(
                    case, conn, top_k=args.top_k, builder=builder, engine=engine
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("case %s failed: %s", cid, exc)
                error_path = run_dir / f"{cid}.error.json"
                error_path.write_text(
                    json.dumps(
                        {"case_id": cid, "error": str(exc)}, indent=2
                    ),
                    encoding="utf-8",
                )
                continue

            out_path = run_dir / f"{cid}.json"
            out_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _print_case_summary(result)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if args.no_judge:
        print("\n--no-judge set; skipping judge step.")
        return 0

    # Hand off to the judge.
    from evals.judge import judge_runs  # local import to avoid hard dep at parse

    print("\nrunning judge…")
    await judge_runs(run_dir)
    print(f"report written to evals/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
