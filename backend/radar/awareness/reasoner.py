"""Awareness engine — Layer 3 (LLM rerank + reasoning).

BUILD_PLAN §9.1 step 3, §9.3 (recommended actions), §9.4 (citations).

For each batch of candidates, calls Claude Opus 4.7 with a forced tool
(`emit_awareness_items`) to emit a list of AwarenessItem records. The
profile + reasoning rubric live in a cached system prompt — across batches
in the same `rerank()` call, only the first call pays the full prefix
cost; calls 2..N read it back from the cache.

Output discipline:
- relevance_score in [0, 10]
- reasoning is 2–3 sentences and references SPECIFIC enrichment fields
  (stance, materiality.scope, materiality.bindingness, topics, mentioned
  entities, source entity name)
- recommended_actions: 0–3 entries, each one concrete (tied to the
  activity, names a docket / bill / agency where one is in the input),
  with a 1-line rationale appended
- citations: list of field paths the reasoning drew from
  (e.g. "enrichment.stance", "enrichment.materiality.scope", "activity.title")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from radar.awareness.retrieval import CandidateActivity
from radar.profiles.builder import StructuredProfile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class AwarenessItem:
    activity_id: str
    relevance_score: float  # 0.0 – 10.0
    reasoning: str
    recommended_actions: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool schema — single tool, emits a list (one Opus call per batch)
# ---------------------------------------------------------------------------


_EMIT_AWARENESS_TOOL: dict[str, Any] = {
    "name": "emit_awareness_items",
    "description": (
        "Emit one awareness item per input candidate. Call this tool exactly "
        "once with the full list — do not call it multiple times."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "activity_id": {
                            "type": "string",
                            "description": "Must match one of the activity_ids in the input list.",
                        },
                        "relevance_score": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 10.0,
                            "description": (
                                "0–10. 3=tangential, 6=should review, 8=directly "
                                "actionable, 10=must brief immediately."
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "description": (
                                "2–3 sentences. MUST reference at least one specific "
                                "enrichment field (stance, materiality.scope/bindingness/novelty, "
                                "a matched topic, a mentioned entity, source entity name) "
                                "drawn from the candidate JSON. Do not invent facts."
                            ),
                        },
                        "recommended_actions": {
                            "type": "array",
                            "maxItems": 3,
                            "items": {"type": "string"},
                            "description": (
                                "0–3 concrete actions. Each item is one line: "
                                "'<action> — <one-line rationale>'. Empty list is OK "
                                "for purely informational items."
                            ),
                        },
                        "citations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Field paths drawn from for the reasoning. Examples: "
                                "'enrichment.summary', 'enrichment.stance', "
                                "'enrichment.materiality.scope', 'activity.title', "
                                "'activity.payload.docket_id'. Only list fields actually "
                                "present in the input."
                            ),
                        },
                    },
                    "required": [
                        "activity_id",
                        "relevance_score",
                        "reasoning",
                        "recommended_actions",
                        "citations",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_RUBRIC = """\
You score and explain why each candidate AI-policy activity matters to a specific user.

Scoring rubric (0–10):
  0–2  : Not relevant. Tangentially mentions a topic the user cares about but no actionable substance.
  3    : Tangentially related — context only.
  4–5  : Adjacent. The user might read it on a slow day.
  6    : Should review — touches a topic OR entity the user explicitly tracks.
  7    : Worth flagging — material development on a tracked topic or watched entity, or both.
  8    : Directly actionable — the user should consider taking a specific action (file comment, brief stakeholder, etc.).
  9    : High-priority brief — material policy change with clear stakes for this user's work.
  10   : Must brief immediately — binding rule, statute, or enforcement action that directly hits the user's equities.

Reasoning rules (HARD):
  1. 2–3 sentences. No more.
  2. The reasoning MUST reference at least one specific enrichment field drawn from the candidate JSON. Acceptable references:
     - `stance` (and `stance_quote` if present)
     - `materiality.scope`, `materiality.bindingness`, `materiality.novelty`, `materiality.confidence`
     - one of the candidate's `topics` (matched to the user's interests)
     - one of the candidate's `mentioned_entities`
     - the source `entity_name`
     - the `title` or a payload field (e.g. `payload.docket_id`)
  3. Do NOT invent dates, docket numbers, bill numbers, or quotes that aren't in the input. If a field isn't there, don't reference it.
  4. Do NOT speculate beyond the input.

Recommended actions (0–3):
  - Each action is one line: "<concrete action> — <one-line rationale>".
  - Concrete = tied to THIS activity. Bad: "monitor AI policy". Good: "File a public comment on the FTC face-recognition NPRM by the comment deadline — directly addresses the user's enforcement risk on facial-recognition products."
  - If the activity is purely informational and there's no realistic action, return an empty list.
  - DO NOT invent dates, dockets, or names that are not in the input.

Citations:
  - List the field paths (e.g. "enrichment.summary", "enrichment.stance",
    "enrichment.materiality.scope", "activity.title", "activity.payload.docket_id")
    you actually drew from. These render as clickable chips in the UI — accuracy matters.
"""


def _format_profile_block(
    profile: StructuredProfile, profile_nl_description: str
) -> str:
    """Render the user profile in a deterministic, cache-stable way."""
    # sort_keys=True → byte-stable; the same profile across batches produces
    # the same prefix and cache hits.
    structured_obj = {
        "topics_weighted": dict(
            sorted((profile.topics_weighted or {}).items())
        ),
        "watch_entities": sorted(profile.watch_entities or []),
        "jurisdictions": sorted(profile.jurisdictions or []),
        "entity_types": sorted(profile.entity_types or []),
        "activity_type_filters": (
            sorted(profile.activity_type_filters)
            if profile.activity_type_filters
            else None
        ),
        "recency_days": profile.recency_days,
        "risk_tolerance": profile.risk_tolerance,
        "notes": profile.notes,
    }
    structured_json = json.dumps(structured_obj, indent=2, sort_keys=True)
    return (
        "## User profile (NL description)\n"
        f"{(profile_nl_description or '').strip() or '(no description)'}\n\n"
        "## User profile (structured)\n"
        f"```json\n{structured_json}\n```"
    )


def _build_system_prompt(
    profile: StructuredProfile, profile_nl_description: str
) -> str:
    """Stable, cacheable system prompt: rubric + profile."""
    return (
        "You are the AI Policy Radar awareness reasoner. Your job is to assess, "
        "for a SPECIFIC user profile, how relevant each input policy activity is, "
        "explain why in 2–3 sentences grounded in the structured enrichment, and "
        "propose 0–3 concrete actions.\n\n"
        + _RUBRIC
        + "\n\n"
        + _format_profile_block(profile, profile_nl_description)
        + "\n\n"
        "When the user turn arrives with a JSON list of candidates, call the "
        "`emit_awareness_items` tool exactly once with one item per candidate. "
        "Preserve `activity_id` exactly. Do not emit any other tool call or any "
        "free-form text."
    )


def _candidate_to_compact_dict(c: CandidateActivity) -> dict:
    """Compact projection of a candidate row for the user turn.

    Keep this tight — never include `raw_text`. The model must reason from
    the enrichment fields and metadata, which is the whole point of the
    enrichment pipeline.
    """
    row = c.activity_row
    payload_obj: Any = row.get("payload")
    if isinstance(payload_obj, str):
        try:
            payload_obj = json.loads(payload_obj)
        except (TypeError, ValueError):
            payload_obj = {}
    if not isinstance(payload_obj, dict):
        payload_obj = {}
    # Drop internal-only payload keys (start with `_`).
    payload_clean = {
        k: v for k, v in payload_obj.items() if isinstance(k, str) and not k.startswith("_")
    }

    return {
        "activity_id": c.activity_id,
        "title": row.get("title"),
        "occurred_at": row.get("occurred_at"),
        "source_entity_id": row.get("entity_id"),
        "source_entity_name": row.get("entity_name"),
        "entity_type": row.get("entity_type"),
        "activity_type": row.get("activity_type"),
        "summary": row.get("summary"),
        "topics": row.get("topics_list", []),
        "mentioned_entities": row.get("mentioned_entities_list", []),
        "stance": row.get("stance"),
        "stance_quote": row.get("stance_quote"),
        "materiality": row.get("materiality_obj", {}),
        "payload": payload_clean,
        "structured_score": round(c.structured_score, 4),
        "embedding_score": (
            None if c.embedding_score is None else round(c.embedding_score, 4)
        ),
    }


# ---------------------------------------------------------------------------
# Reasoner
# ---------------------------------------------------------------------------


class AwarenessReasoner:
    """Layer 3 — Opus 4.7 batched rerank with cached system prompt."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        batch_size: int = 5,
        max_tokens: int = 4096,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; the awareness reasoner cannot run."
            )
        # SDK reads ANTHROPIC_API_KEY from env — never pass explicitly.
        self._client = anthropic.AsyncAnthropic()
        self._model = model
        self._batch_size = max(1, batch_size)
        self._max_tokens = max_tokens
        # Stash usage stats per call for visibility / cache verification.
        self.batch_usages: list[dict[str, int]] = []

    async def rerank(
        self,
        profile: StructuredProfile,
        profile_nl_description: str,
        candidates: list[CandidateActivity],
        *,
        top_k: int = 15,
    ) -> list[AwarenessItem]:
        """Rerank candidates via Opus 4.7. Returns top_k by relevance_score."""
        if not candidates:
            return []

        system_prompt = _build_system_prompt(profile, profile_nl_description)

        # Slice into batches; each batch is a separate API call. Same system
        # prompt across calls → cache hits on calls 2..N.
        batches: list[list[CandidateActivity]] = []
        for i in range(0, len(candidates), self._batch_size):
            batches.append(candidates[i : i + self._batch_size])

        self.batch_usages = []

        # Run batches sequentially so the cache write from batch 1 is visible
        # to batch 2..N. Cache becomes readable once the first response begins
        # streaming; sequential is the simple, correct ordering here.
        all_items: list[AwarenessItem] = []
        for idx, batch in enumerate(batches):
            try:
                items = await self._reason_one_batch(
                    system_prompt=system_prompt, batch=batch
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[awareness.reasoner] batch %d/%d failed: %s — skipping",
                    idx + 1,
                    len(batches),
                    e,
                )
                continue
            all_items.extend(items)

        # Drop any items whose activity_id we didn't ask about (defensive).
        valid_ids = {c.activity_id for c in candidates}
        all_items = [a for a in all_items if a.activity_id in valid_ids]

        all_items.sort(key=lambda a: a.relevance_score, reverse=True)
        return all_items[:top_k]

    # ------------------------------------------------------------------

    async def _reason_one_batch(
        self,
        *,
        system_prompt: str,
        batch: list[CandidateActivity],
    ) -> list[AwarenessItem]:
        candidates_json = json.dumps(
            [_candidate_to_compact_dict(c) for c in batch], indent=2
        )
        user_text = (
            "Here are the candidate activities for this user. Emit one "
            "awareness item per candidate by calling `emit_awareness_items` "
            "exactly once.\n\n"
            f"```json\n{candidates_json}\n```"
        )

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_EMIT_AWARENESS_TOOL],
            tool_choice={"type": "tool", "name": "emit_awareness_items"},
            messages=[{"role": "user", "content": user_text}],
        )

        # Capture usage.
        try:
            u = response.usage
            self.batch_usages.append(
                {
                    "input_tokens": getattr(u, "input_tokens", 0) or 0,
                    "output_tokens": getattr(u, "output_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(
                        u, "cache_creation_input_tokens", 0
                    )
                    or 0,
                    "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0)
                    or 0,
                }
            )
        except Exception:  # noqa: BLE001
            pass

        # Find the tool_use block.
        tool_input: Optional[dict] = None
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "emit_awareness_items"
            ):
                inp = getattr(block, "input", None)
                if isinstance(inp, dict):
                    tool_input = inp
                    break

        if tool_input is None:
            log.warning(
                "[awareness.reasoner] model did not call emit_awareness_items "
                "(stop_reason=%s)",
                getattr(response, "stop_reason", "?"),
            )
            return []

        items_in = tool_input.get("items") or []
        if not isinstance(items_in, list):
            log.warning(
                "[awareness.reasoner] emit_awareness_items.items not a list (got %s)",
                type(items_in).__name__,
            )
            return []

        out: list[AwarenessItem] = []
        for raw in items_in:
            if not isinstance(raw, dict):
                continue
            try:
                aid = str(raw.get("activity_id") or "")
                if not aid:
                    continue
                score = float(raw.get("relevance_score", 0.0))
                score = max(0.0, min(10.0, score))
                reasoning = str(raw.get("reasoning") or "").strip()
                actions_raw = raw.get("recommended_actions") or []
                actions: list[str] = []
                if isinstance(actions_raw, list):
                    for a in actions_raw:
                        if isinstance(a, str) and a.strip():
                            actions.append(a.strip())
                actions = actions[:3]
                citations_raw = raw.get("citations") or []
                citations: list[str] = []
                if isinstance(citations_raw, list):
                    for cstr in citations_raw:
                        if isinstance(cstr, str) and cstr.strip():
                            citations.append(cstr.strip())
                out.append(
                    AwarenessItem(
                        activity_id=aid,
                        relevance_score=score,
                        reasoning=reasoning,
                        recommended_actions=actions,
                        citations=citations,
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[awareness.reasoner] failed to parse item %r: %s", raw, e
                )
                continue
        return out


__all__ = [
    "AwarenessItem",
    "AwarenessReasoner",
]
