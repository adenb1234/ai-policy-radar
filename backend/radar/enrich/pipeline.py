"""Enrichment pipeline (BUILD_PLAN §8).

Consumes a freshly-ingested Activity row and produces an `enrichment` row:
summary, topics, mentioned_entities, stance + quote, materiality. All via
Claude Sonnet 4.6 with prompt caching on the system block (topic vocab +
entity directory + per-type guidance — together typically 30KB+, the whole
point of caching).

Design notes:
- Tool-calling is forced via `tool_choice={"type": "tool", "name": "emit_enrichment"}`.
  This sidesteps JSON-extraction failures — the SDK validates the tool input
  against the declared schema before we touch it.
- The system prompt assembly is deterministic (sorted keys on the JSON
  blocks) so the prefix is byte-stable across calls — that's what makes the
  cache hit. See SDK docs §Prompt Caching for the prefix-match invariant.
- Validation is post-hoc (topics, entities, stance_quote) — we trust the
  schema for shape, then sanity-check ids and citation discipline ourselves
  before returning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import yaml
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
PROMPTS_DIR = _THIS.with_name("prompts")
# topics.yaml lives at backend/data/topics.yaml; this file is at
# backend/radar/enrich/pipeline.py — go up 2 (radar/enrich → backend), then data/.
DEFAULT_TOPIC_VOCAB_PATH = _THIS.parents[2] / "data" / "topics.yaml"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EnrichmentError(Exception):
    """Raised when enrichment fails (model error, schema violation, etc.).

    The message includes a short summary; the truncated raw text is on
    `.raw_text` for caller logging.
    """

    def __init__(self, message: str, raw_text: str | None = None):
        super().__init__(message)
        self.raw_text = (raw_text or "")[:1024]


# ---------------------------------------------------------------------------
# Tool definition (matches the `enrichment` table columns)
# ---------------------------------------------------------------------------

_VALID_STANCES = {"supports", "opposes", "neutral", "mixed"}
_VALID_SCOPES = ["federal", "state", "local", "international", "sector"]
_VALID_BINDINGNESS = ["rule", "guidance", "enforcement", "statement", "proposal"]
_VALID_NOVELTY = ["new_position", "restated", "escalation", "reversal"]


_EMIT_ENRICHMENT_TOOL: dict[str, Any] = {
    "name": "emit_enrichment",
    "description": (
        "Emit the enrichment record for a single activity. Field names match "
        "the `enrichment` table columns so the orchestrator can do a near-"
        "direct insert."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2–4 sentences, plain English, factual, no hype.",
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Topic ids from the provided vocabulary, max 5, ordered "
                    "by relevance. Empty array if no topic clearly fits."
                ),
                "maxItems": 5,
            },
            "mentioned_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Entity ids from the provided directory that appear by "
                    "name or alias in the source text. Exclude the source "
                    "entity itself."
                ),
            },
            "stance": {
                "type": ["string", "null"],
                "enum": ["supports", "opposes", "neutral", "mixed", None],
                "description": (
                    "supports | opposes | neutral | mixed | null. Use null "
                    "for purely informational activities."
                ),
            },
            "stance_quote": {
                "type": ["string", "null"],
                "description": (
                    "Verbatim quote (≤30 words) from raw_text justifying the "
                    "stance. REQUIRED if stance is non-null; null otherwise."
                ),
            },
            "materiality": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": _VALID_SCOPES},
                    "bindingness": {"type": "string", "enum": _VALID_BINDINGNESS},
                    "novelty": {"type": "string", "enum": _VALID_NOVELTY},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["scope", "bindingness", "novelty", "confidence"],
                "additionalProperties": False,
            },
        },
        "required": [
            "summary",
            "topics",
            "mentioned_entities",
            "stance",
            "stance_quote",
            "materiality",
        ],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _load_topic_vocab(path: Path) -> list[dict[str, Any]]:
    """Load topics.yaml and reduce to the fields the model needs."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise EnrichmentError(f"topics.yaml at {path} is not a list")
    out: list[dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict) or "id" not in t:
            continue
        out.append(
            {
                "id": str(t["id"]),
                "name": str(t.get("name", t["id"])),
                "synonyms": [str(s) for s in (t.get("synonyms") or [])],
            }
        )
    return out


def _stable_json(obj: Any) -> str:
    """Deterministic JSON for cache-stable prompt assembly."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)


def _read_prompt(name: str) -> str | None:
    """Read a prompt file from prompts/ if it exists. None if missing."""
    p = PROMPTS_DIR / name
    if p.exists():
        return p.read_text()
    return None


def _resolve_per_type_guidance(entity_type: str | None) -> str:
    """Look up per-entity-type guidance. Empty string if no override."""
    if not entity_type:
        return ""
    text = _read_prompt(f"{entity_type.lower()}.md")
    return text or ""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SystemBlocks:
    """Cacheable system prompt material — assembled once per pipeline call.

    The whole block is sent as a single string (with cache_control) so it's
    a stable byte prefix as long as the inputs (topic_vocab, entity_directory,
    activity_type guidance) don't change. The model cache breakpoint sits at
    the end of this block; the user turn (with the activity) is appended
    fresh per call and is never cached.
    """

    text: str


class EnrichmentPipeline:
    """Sonnet 4.6 enrichment pipeline with prompt caching + tool-forced output."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        topic_vocab_path: Path | None = None,
        max_concurrent: int = 6,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set in the environment. Pipeline cannot run."
            )
        # Let the SDK pick up ANTHROPIC_API_KEY from os.environ — never pass
        # explicitly, to avoid accidental logging.
        self._client = anthropic.AsyncAnthropic()
        self._model = model
        self._topic_vocab_path = topic_vocab_path or DEFAULT_TOPIC_VOCAB_PATH
        self._topics = _load_topic_vocab(self._topic_vocab_path)
        self._topic_ids = {t["id"] for t in self._topics}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._base_prompt = _read_prompt("_base.md")
        if self._base_prompt is None:
            raise EnrichmentError(
                f"_base.md not found in {PROMPTS_DIR}. Did you run from a complete checkout?"
            )
        # Stash for unit tests and reporting.
        self.last_usage: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_system_block(
        self,
        *,
        entity_directory: list[dict[str, Any]],
        activity_type_guidance: str,
        few_shot_examples: str = "",
    ) -> _SystemBlocks:
        topic_block = _stable_json(self._topics)
        # Reduce entity_directory to {id, name, aliases} for cache stability.
        slim_dir = [
            {
                "id": str(e["id"]),
                "name": str(e.get("name", e["id"])),
                "aliases": [str(a) for a in (e.get("aliases") or [])],
            }
            for e in entity_directory
        ]
        # Sort by id so the JSON byte-prefix is stable independent of caller order.
        slim_dir.sort(key=lambda x: x["id"])
        entity_block = _stable_json(slim_dir)

        text = (
            self._base_prompt.replace("{{ TOPIC_VOCAB }}", topic_block)
            .replace("{{ ENTITY_DIRECTORY }}", entity_block)
            .replace(
                "{{ ACTIVITY_TYPE_GUIDANCE }}",
                activity_type_guidance.strip() or "(No type-specific guidance for this entity_type — apply the general schema.)",
            )
            .replace(
                "{{ FEW_SHOT_EXAMPLES }}",
                few_shot_examples.strip() or "(No few-shot examples provided.)",
            )
        )
        return _SystemBlocks(text=text)

    def _build_user_message(self, activity_row: dict) -> str:
        """One-paragraph framing of the activity for the model to enrich."""
        # Keep this compact and predictable. The activity-specific fields are
        # the only things that change between calls — everything else is
        # cached.
        fields = {
            "entity_id": activity_row.get("entity_id"),
            "entity_type": activity_row.get("entity_type"),
            "activity_type": activity_row.get("activity_type"),
            "occurred_at": activity_row.get("occurred_at"),
            "title": activity_row.get("title"),
            "source_url": activity_row.get("source_url"),
        }
        lines = [
            "Enrich the following activity. Return your output by calling the `emit_enrichment` tool exactly once.",
            "",
            "## Activity metadata",
            "",
        ]
        for k, v in fields.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("## Raw text")
        lines.append("")
        lines.append(str(activity_row.get("raw_text") or "").strip())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def enrich_activity(
        self, activity_row: dict, *, entity_directory: list[dict[str, Any]]
    ) -> dict:
        """Enrich a single activity. Returns a dict matching the enrichment table.

        Raises:
            EnrichmentError on schema violation, tool-use missing, or
            persistent API failure (after retries).
        """
        async with self._semaphore:
            return await self._enrich_one(activity_row, entity_directory)

    async def enrich_batch(
        self, activities: list[dict], *, entity_directory: list[dict[str, Any]]
    ) -> list[tuple[str, dict | EnrichmentError]]:
        """Concurrent fanout. Returns list of (activity_id, result-or-error).

        Each item is processed under the shared semaphore. Caller decides
        what to do with errors (skip, log, retry).
        """

        async def _one(act: dict) -> tuple[str, dict | EnrichmentError]:
            aid = str(act.get("id") or act.get("activity_id") or "<unknown>")
            try:
                async with self._semaphore:
                    res = await self._enrich_one(act, entity_directory)
                return (aid, res)
            except EnrichmentError as e:
                return (aid, e)
            except Exception as e:  # noqa: BLE001
                return (aid, EnrichmentError(f"unexpected error: {type(e).__name__}: {e}"))

        return await asyncio.gather(*(_one(a) for a in activities))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _enrich_one(
        self, activity_row: dict, entity_directory: list[dict[str, Any]]
    ) -> dict:
        entity_type = activity_row.get("entity_type")
        guidance = _resolve_per_type_guidance(entity_type)
        system_block = self._build_system_block(
            entity_directory=entity_directory,
            activity_type_guidance=guidance,
        )
        user_text = self._build_user_message(activity_row)

        try:
            response = await self._call_model(
                system_text=system_block.text,
                user_text=user_text,
            )
        except anthropic.APIError as e:
            raise EnrichmentError(f"API error: {type(e).__name__}: {e}", raw_text=user_text)

        # Track cache usage for visibility.
        try:
            usage = response.usage
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            }
        except Exception:  # noqa: BLE001
            self.last_usage = None

        # Extract the tool_use block.
        tool_input: dict | None = None
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_enrichment":
                tool_input = block.input or {}
                break

        if tool_input is None:
            # The model didn't call the tool — fail the activity.
            text_blocks = [
                getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
            ]
            raise EnrichmentError(
                f"model did not call emit_enrichment (stop_reason={response.stop_reason!r}); text={' '.join(text_blocks)[:200]!r}",
                raw_text=user_text,
            )

        # Validate + clean the output before returning.
        cleaned = self._validate_and_clean(
            tool_input,
            activity_row=activity_row,
            entity_directory=entity_directory,
        )
        cleaned["enriched_at"] = _now_iso()
        cleaned["enricher_model"] = self._model
        return cleaned

    @retry(
        retry=retry_if_exception_type(
            (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    async def _call_model(self, *, system_text: str, user_text: str):
        """Single model call with retries on 429/5xx/connection errors."""
        return await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_EMIT_ENRICHMENT_TOOL],
            tool_choice={"type": "tool", "name": "emit_enrichment"},
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}],
                }
            ],
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_and_clean(
        self,
        tool_input: dict,
        *,
        activity_row: dict,
        entity_directory: list[dict[str, Any]],
    ) -> dict:
        """Apply post-hoc discipline checks; never raise — clean and warn."""
        activity_id = str(activity_row.get("id") or activity_row.get("activity_id") or "<unknown>")
        source_entity_id = activity_row.get("entity_id")

        # summary — accept as-is, just coerce to string.
        summary = str(tool_input.get("summary") or "").strip()
        if not summary:
            raise EnrichmentError("model returned empty summary", raw_text=str(activity_row.get("raw_text") or "")[:1024])

        # topics — filter to known ids; cap at 5.
        raw_topics = tool_input.get("topics") or []
        topics: list[str] = []
        seen_topics: set[str] = set()
        for t in raw_topics:
            if not isinstance(t, str):
                continue
            if t in self._topic_ids and t not in seen_topics:
                topics.append(t)
                seen_topics.add(t)
            else:
                if t not in self._topic_ids:
                    log.warning(
                        "[enrich] %s: dropping unknown topic id %r", activity_id, t
                    )
        topics = topics[:5]

        # mentioned_entities — filter to known ids, exclude source entity.
        valid_entity_ids = {str(e["id"]) for e in entity_directory}
        raw_mentioned = tool_input.get("mentioned_entities") or []
        mentioned: list[str] = []
        seen_mentioned: set[str] = set()
        for eid in raw_mentioned:
            if not isinstance(eid, str):
                continue
            if eid == source_entity_id:
                continue  # silently drop self-mentions
            if eid in valid_entity_ids and eid not in seen_mentioned:
                mentioned.append(eid)
                seen_mentioned.add(eid)
            elif eid not in valid_entity_ids:
                log.warning(
                    "[enrich] %s: dropping unknown entity id %r", activity_id, eid
                )

        # stance — null or one of the enum.
        stance = tool_input.get("stance")
        if stance is not None and stance not in _VALID_STANCES:
            log.warning(
                "[enrich] %s: invalid stance %r — coercing to null", activity_id, stance
            )
            stance = None

        stance_quote = tool_input.get("stance_quote")
        if stance is None:
            stance_quote = None
        else:
            # Citation discipline: stance_quote must appear in raw_text
            # (case-insensitive, whitespace-normalized). If not, drop both.
            raw_text = str(activity_row.get("raw_text") or "")
            if not isinstance(stance_quote, str) or not stance_quote.strip():
                log.warning(
                    "[enrich] %s: stance=%r but no stance_quote — dropping stance",
                    activity_id,
                    stance,
                )
                stance = None
                stance_quote = None
            else:
                norm_quote = _normalize_ws(stance_quote)
                norm_text = _normalize_ws(raw_text)
                if norm_quote and norm_quote in norm_text:
                    pass  # valid — keep both
                else:
                    log.warning(
                        "[enrich] %s: stance_quote not a verbatim substring of raw_text — dropping stance (quote=%r)",
                        activity_id,
                        (stance_quote or "")[:80],
                    )
                    stance = None
                    stance_quote = None

        # materiality — coerce shape; never trust the model fully.
        mat_in = tool_input.get("materiality") or {}
        if not isinstance(mat_in, dict):
            mat_in = {}
        scope = mat_in.get("scope") if mat_in.get("scope") in _VALID_SCOPES else "federal"
        bindingness = (
            mat_in.get("bindingness")
            if mat_in.get("bindingness") in _VALID_BINDINGNESS
            else "statement"
        )
        novelty = (
            mat_in.get("novelty") if mat_in.get("novelty") in _VALID_NOVELTY else "new_position"
        )
        try:
            confidence = float(mat_in.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        materiality = {
            "scope": scope,
            "bindingness": bindingness,
            "novelty": novelty,
            "confidence": confidence,
        }

        return {
            "summary": summary,
            "topics": topics,
            "mentioned_entities": mentioned,
            "stance": stance,
            "stance_quote": stance_quote,
            "materiality": materiality,
        }


__all__ = [
    "EnrichmentPipeline",
    "EnrichmentError",
]
