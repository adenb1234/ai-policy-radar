"""Eval judge (BUILD_PLAN §12.3).

LLM-as-judge using Claude Opus 4.7. Per case, given:
  - the case profile (NL + structured)
  - the ground-truth `expected_*` arrays
  - the actual dashboard items (with reasoning + actions + citations)

…the judge emits three 0–10 axes plus a one-line rationale per axis:
  - relevance_recall:  did we surface the expected items?
  - reasoning_quality: were the reasonings accurate, grounded, actionable?
  - extras_quality:    were the items we surfaced beyond `expected` reasonable?

If `expected_*` arrays are empty, the corresponding axis is set to null and
the rationale captures "ungraded — Aden has not yet provided ground truth".

The system prompt (rubric) is cached: profile + dashboard change per case but
the rubric is stable across the run, so cache hits reduce cost.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make `radar.*` importable when called from the runner that already set up
# sys.path, but also when judge.py is imported standalone.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import anthropic  # noqa: E402

log = logging.getLogger("evals.judge")

JUDGE_MODEL = "claude-opus-4-7"
REPORT_PATH = REPO_ROOT / "evals" / "report.md"


# ---------------------------------------------------------------------------
# Rubric (cached system prompt — deliberately stable byte-for-byte)
# ---------------------------------------------------------------------------


_RUBRIC = """\
You are the evaluation judge for an AI policy awareness dashboard.

You will be given, for ONE eval case:
  - The user profile (NL description + structured fields).
  - Ground-truth fields ("expected_*"), written by a human reviewer.
  - The system's actual top-k dashboard output (activities + reasonings + actions).

Your job is to score three axes (0–10 each) and emit one short rationale per
axis. Call the `emit_judgment` tool exactly once.

Axes:

1. relevance_recall (0–10)
   - Computes how well the system surfaced the items in
     `expected_top_activities`. Strict intersection: count distinct
     expected activity_ids that appear anywhere in the actual items list,
     divided by len(expected). Multiply by 10 and round to nearest 0.5.
   - 10 = every expected activity_id is in the actual top-k.
   - 0  = none of the expected activity_ids surfaced.
   - Also factor `expected_topics_surfaced`: if a substantial expected
     topic is missing across all surfaced items, dock 1 point per missing
     topic (cap at 3 deductions).

2. reasoning_quality (0–10)
   - For each actual item, judge whether the `reasoning` is:
     (a) accurate (consistent with the activity title/summary/enrichment),
     (b) grounded (references specific enrichment fields per the rubric —
         stance, materiality, topics, mentioned entities, source entity),
     (c) actionable (clearly tied to this user's profile equities).
   - Average across actual items, scaled to 0–10. If reasoning hallucinates
     dates/dockets/quotes not in the input, dock 2 points and flag in the
     rationale.

3. extras_quality (0–10)
   - For each actual item NOT in `expected_top_activities`, decide whether
     it was a reasonable surface for this profile. 10 = every extra is
     defensibly relevant. 0 = extras are off-topic noise.
   - Also weigh `expected_action_themes`: if the actual items' recommended
     actions cover at least 50% of the expected_action_themes substrings,
     keep score; if <25% match, dock up to 3 points.

Ungraded handling (HARD):
  - If `expected_top_activities` is empty, set `relevance_recall` to null and
    add "ungraded — Aden has not yet provided ground truth" to its rationale.
  - If `expected_top_activities` is empty, also set `extras_quality` to null
    with the same rationale (extras are defined relative to expected).
  - `reasoning_quality` can still be scored without ground truth — judge it
    against the activity content directly.

Rationale format:
  - One sentence per axis. Reference specific items by activity_id when useful.
  - If an axis is null, the rationale must explicitly say
    "ungraded — Aden has not yet provided ground truth".

Be conservative. The point of these scores is to detect regressions across
runs. Use the full 0–10 range; do not bunch everything at 7–8.
"""


_JUDGE_TOOL: dict[str, Any] = {
    "name": "emit_judgment",
    "description": (
        "Emit the per-axis judgment for this single eval case. Call exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "relevance_recall": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 10.0,
                "description": (
                    "0–10. Null if `expected_top_activities` is empty."
                ),
            },
            "reasoning_quality": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 10.0,
                "description": "0–10. Always graded if there is at least one actual item.",
            },
            "extras_quality": {
                "type": ["number", "null"],
                "minimum": 0.0,
                "maximum": 10.0,
                "description": (
                    "0–10. Null if `expected_top_activities` is empty."
                ),
            },
            "rationales": {
                "type": "object",
                "properties": {
                    "relevance_recall": {"type": "string"},
                    "reasoning_quality": {"type": "string"},
                    "extras_quality": {"type": "string"},
                },
                "required": [
                    "relevance_recall",
                    "reasoning_quality",
                    "extras_quality",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "relevance_recall",
            "reasoning_quality",
            "extras_quality",
            "rationales",
        ],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Per-case judging
# ---------------------------------------------------------------------------


def _compact_item_for_judge(item: dict) -> dict:
    """Compact view of a hydrated awareness item for the judge prompt.

    Drops `raw_text` (always huge) and `payload` internals; keeps the
    enrichment + reasoning + actions, which are what we're judging.
    """
    activity = item.get("activity") or {}
    enrichment = item.get("enrichment") or {}
    awareness = item.get("awareness") or {}
    source = item.get("source_entity") or {}
    return {
        "activity_id": activity.get("id"),
        "title": activity.get("title"),
        "occurred_at": activity.get("occurred_at"),
        "activity_type": activity.get("activity_type"),
        "source_entity_name": source.get("name"),
        "source_entity_type": source.get("entity_type"),
        "summary": enrichment.get("summary"),
        "topics": enrichment.get("topics") or [],
        "mentioned_entities": enrichment.get("mentioned_entities") or [],
        "stance": enrichment.get("stance"),
        "stance_quote": enrichment.get("stance_quote"),
        "materiality": enrichment.get("materiality") or {},
        "relevance_score": awareness.get("relevance_score"),
        "reasoning": awareness.get("reasoning"),
        "recommended_actions": awareness.get("recommended_actions") or [],
        "citations": awareness.get("citations") or [],
    }


def _build_judge_user_payload(case: dict, run: dict) -> str:
    """Compose the per-case JSON the judge sees in the user turn."""
    items = (run.get("dashboard") or {}).get("items") or []
    payload = {
        "case_id": run.get("case_id") or case.get("id"),
        "profile_nl": run.get("profile_nl") or (case.get("profile") or {}).get("nl"),
        "profile_structured": run.get("profile_structured")
        or (case.get("profile") or {}).get("structured_overrides")
        or {},
        "expected_top_activities": run.get("expected_top_activities")
        or case.get("expected_top_activities")
        or [],
        "expected_topics_surfaced": run.get("expected_topics_surfaced")
        or case.get("expected_topics_surfaced")
        or [],
        "expected_action_themes": run.get("expected_action_themes")
        or case.get("expected_action_themes")
        or [],
        "actual_items": [_compact_item_for_judge(it) for it in items],
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        "Here is the eval case + actual dashboard. Score it per the rubric "
        "and call `emit_awareness_items`-style tool `emit_judgment` exactly once.\n\n"
        f"```json\n{body}\n```"
    )


def _ungraded_marker() -> str:
    return "ungraded — Aden has not yet provided ground truth"


async def judge_case(
    case: dict,
    run: dict,
    *,
    client: anthropic.AsyncAnthropic | None = None,
) -> dict:
    """Judge one (case, run) pair via Opus 4.7. Returns a dict:

    {
      "case_id": str,
      "scores": {"relevance_recall": float|None, "reasoning_quality": float|None,
                  "extras_quality": float|None},
      "rationales": {<axis>: str, ...},
      "usage": {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens},
    }
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; the judge cannot run."
        )
    client = client or anthropic.AsyncAnthropic()

    case_id = run.get("case_id") or case.get("id") or "?"

    # If no actual items, every axis is ungraded — short-circuit, save an API call.
    items = (run.get("dashboard") or {}).get("items") or []
    expected = run.get("expected_top_activities") or case.get("expected_top_activities") or []

    if not items:
        marker = "no actual items returned by the dashboard"
        return {
            "case_id": case_id,
            "scores": {
                "relevance_recall": None,
                "reasoning_quality": None,
                "extras_quality": None,
            },
            "rationales": {
                "relevance_recall": (
                    _ungraded_marker() if not expected else f"{marker}; cannot compute recall"
                ),
                "reasoning_quality": marker,
                "extras_quality": (
                    _ungraded_marker() if not expected else marker
                ),
            },
            "usage": {},
        }

    user_text = _build_judge_user_payload(case, run)

    response = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": _RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "emit_judgment"},
        messages=[{"role": "user", "content": user_text}],
    )

    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(
            usage_obj, "cache_creation_input_tokens", 0
        )
        or 0,
        "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0)
        or 0,
    }

    tool_input: dict | None = None
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "emit_judgment"
        ):
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                tool_input = inp
                break

    if tool_input is None:
        log.warning(
            "[evals.judge] no emit_judgment tool call for %s (stop_reason=%s)",
            case_id,
            getattr(response, "stop_reason", "?"),
        )
        return {
            "case_id": case_id,
            "scores": {
                "relevance_recall": None,
                "reasoning_quality": None,
                "extras_quality": None,
            },
            "rationales": {
                "relevance_recall": "judge produced no tool call",
                "reasoning_quality": "judge produced no tool call",
                "extras_quality": "judge produced no tool call",
            },
            "usage": usage,
        }

    rationales_in = tool_input.get("rationales") or {}
    if not isinstance(rationales_in, dict):
        rationales_in = {}

    def _coerce_score(val: Any) -> float | None:
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(10.0, f))

    return {
        "case_id": case_id,
        "scores": {
            "relevance_recall": _coerce_score(tool_input.get("relevance_recall")),
            "reasoning_quality": _coerce_score(tool_input.get("reasoning_quality")),
            "extras_quality": _coerce_score(tool_input.get("extras_quality")),
        },
        "rationales": {
            "relevance_recall": str(rationales_in.get("relevance_recall") or "").strip(),
            "reasoning_quality": str(rationales_in.get("reasoning_quality") or "").strip(),
            "extras_quality": str(rationales_in.get("extras_quality") or "").strip(),
        },
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Run-directory judging + report assembly
# ---------------------------------------------------------------------------


def _load_cases_index() -> dict[str, dict]:
    """Map case_id -> case dict, loading directly from cases.yaml.

    The runner's JSON has the expected_* fields baked in (copied from
    cases.yaml at run time), so the judge could rely on them — but loading
    cases.yaml too gives us a fallback if the run JSON is older.
    """
    from evals.run import load_cases  # late import to avoid cycles

    try:
        cases = load_cases()
    except FileNotFoundError:
        return {}
    return {str(c.get("id")): c for c in cases if c.get("id")}


def _avg_or_none(vals: list[float | None]) -> float | None:
    nums = [v for v in vals if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _list_run_dirs() -> list[Path]:
    runs_dir = REPO_ROOT / "evals" / "runs"
    if not runs_dir.exists():
        return []
    return sorted(
        [p for p in runs_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )


def _read_prior_aggregate() -> dict[str, float | None] | None:
    """Read the previous report.md's aggregate line so we can compute deltas.

    We parse a tiny, well-known anchor block written below. If the file
    doesn't have it (e.g. first run), return None.
    """
    if not REPORT_PATH.exists():
        return None
    text = REPORT_PATH.read_text(encoding="utf-8", errors="replace")
    marker = "<!-- AGGREGATE-LINE: "
    end = " -->"
    i = text.find(marker)
    if i == -1:
        return None
    j = text.find(end, i)
    if j == -1:
        return None
    blob = text[i + len(marker) : j].strip()
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _delta(curr: float | None, prior: float | None) -> str:
    if curr is None or prior is None:
        return "—"
    diff = curr - prior
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.2f}"


async def judge_runs(run_dir: Path) -> Path:
    """Async-fan-out judge_case across all <case_id>.json files in run_dir.

    Writes (overwrites) `evals/report.md` with the aggregate + per-case
    table + detailed rationales.
    """
    run_dir = Path(run_dir).resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    json_files = sorted(
        [p for p in run_dir.iterdir() if p.suffix == ".json" and not p.name.endswith(".error.json")]
    )
    if not json_files:
        log.warning("[evals.judge] no run JSON files in %s", run_dir)

    cases_index = _load_cases_index()

    # Load all run JSONs first (cheap).
    runs: list[dict] = []
    for p in json_files:
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("[evals.judge] failed to load %s: %s", p, exc)

    if not runs:
        log.warning("[evals.judge] no runs to judge")

    # Single shared client; sequential awaits would also work, but limited
    # concurrency keeps the whole pass fast without hammering the API.
    client = anthropic.AsyncAnthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    sem = asyncio.Semaphore(4)

    async def _one(run: dict) -> dict:
        cid = run.get("case_id") or "?"
        case = cases_index.get(cid, {"id": cid})
        async with sem:
            try:
                return await judge_case(case, run, client=client)
            except Exception as exc:  # noqa: BLE001
                log.exception("[evals.judge] case %s failed: %s", cid, exc)
                return {
                    "case_id": cid,
                    "scores": {
                        "relevance_recall": None,
                        "reasoning_quality": None,
                        "extras_quality": None,
                    },
                    "rationales": {
                        "relevance_recall": f"judge crashed: {exc}",
                        "reasoning_quality": f"judge crashed: {exc}",
                        "extras_quality": f"judge crashed: {exc}",
                    },
                    "usage": {},
                }

    judgments = await asyncio.gather(*[_one(r) for r in runs])

    # Persist judgments alongside run JSON for traceability.
    judgments_path = run_dir / "_judgments.json"
    judgments_path.write_text(
        json.dumps(judgments, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Aggregate.
    avg_recall = _avg_or_none([j["scores"]["relevance_recall"] for j in judgments])
    avg_reason = _avg_or_none(
        [j["scores"]["reasoning_quality"] for j in judgments]
    )
    avg_extras = _avg_or_none([j["scores"]["extras_quality"] for j in judgments])

    prior = _read_prior_aggregate()
    d_recall = _delta(avg_recall, (prior or {}).get("relevance_recall"))
    d_reason = _delta(avg_reason, (prior or {}).get("reasoning_quality"))
    d_extras = _delta(avg_extras, (prior or {}).get("extras_quality"))

    # Compose markdown.
    lines: list[str] = []
    ts = run_dir.name
    lines.append(f"## Run {ts}")
    lines.append("")
    lines.append(
        f"## Aggregate: avg recall {_fmt(avg_recall)}, "
        f"avg reasoning {_fmt(avg_reason)}, "
        f"avg extras {_fmt(avg_extras)}"
    )
    lines.append(
        f"## Delta vs prior run: {d_recall} recall, {d_reason} reasoning, {d_extras} extras"
    )
    lines.append("")
    # Hidden anchor for next run's delta computation.
    lines.append(
        "<!-- AGGREGATE-LINE: "
        + json.dumps(
            {
                "relevance_recall": avg_recall,
                "reasoning_quality": avg_reason,
                "extras_quality": avg_extras,
                "run": ts,
            },
            sort_keys=True,
        )
        + " -->"
    )
    lines.append("")

    # Per-case table.
    lines.append("## Per-case table")
    lines.append("")
    lines.append("| case_id | recall | reasoning | extras | notes |")
    lines.append("| --- | --- | --- | --- | --- |")
    for j in judgments:
        cid = j.get("case_id", "?")
        s = j.get("scores", {})
        rats = j.get("rationales", {}) or {}
        # Pull the most informative note: prefer recall rationale (it carries
        # the "ungraded" marker when applicable), then reasoning.
        note = (
            rats.get("relevance_recall")
            or rats.get("reasoning_quality")
            or rats.get("extras_quality")
            or ""
        )
        # Truncate for table cell.
        note = note.replace("|", "\\|").replace("\n", " ")
        if len(note) > 140:
            note = note[:137] + "…"
        lines.append(
            f"| {cid} "
            f"| {_fmt(s.get('relevance_recall'))} "
            f"| {_fmt(s.get('reasoning_quality'))} "
            f"| {_fmt(s.get('extras_quality'))} "
            f"| {note} |"
        )
    lines.append("")

    # Detailed rationales.
    lines.append("## Detailed rationales")
    lines.append("")
    for j in judgments:
        cid = j.get("case_id", "?")
        s = j.get("scores", {})
        rats = j.get("rationales", {}) or {}
        lines.append(f"### {cid}")
        lines.append("")
        lines.append(
            f"- **relevance_recall ({_fmt(s.get('relevance_recall'))})** — "
            f"{rats.get('relevance_recall', '').strip() or '—'}"
        )
        lines.append(
            f"- **reasoning_quality ({_fmt(s.get('reasoning_quality'))})** — "
            f"{rats.get('reasoning_quality', '').strip() or '—'}"
        )
        lines.append(
            f"- **extras_quality ({_fmt(s.get('extras_quality'))})** — "
            f"{rats.get('extras_quality', '').strip() or '—'}"
        )
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"📊 judge: avg recall {_fmt(avg_recall)} · "
        f"avg reasoning {_fmt(avg_reason)} · avg extras {_fmt(avg_extras)} "
        f"({len(judgments)} cases)"
    )
    return REPORT_PATH


__all__ = ["judge_case", "judge_runs"]


if __name__ == "__main__":
    # Simple CLI: judge_runs against the most recent run dir.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Run directory under evals/runs/ (default: most recent).",
    )
    args = parser.parse_args()

    if args.run_dir:
        target = Path(args.run_dir).resolve()
    else:
        run_dirs = _list_run_dirs()
        if not run_dirs:
            print("no run directories under evals/runs/")
            sys.exit(1)
        target = run_dirs[-1]

    asyncio.run(judge_runs(target))
