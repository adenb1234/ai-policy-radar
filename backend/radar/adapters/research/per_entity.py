"""Per-entity research adapter (BUILD_PLAN §7.2).

Wraps Claude Opus 4.7 with the server-side `web_search` tool to discover and
ingest activities for entities without clean APIs (civil society, international,
state/local, party factions).

Discipline (enforced via the system prompt + post-parse validation):
- Every emitted item must include a fetchable source_url.
- occurred_at must be within the [since, today] window.
- raw_text_excerpt must be copied verbatim from the source page.
- verify_phrase must be a 5–12 word distinctive phrase that should appear on
  the page; URL-verification (§7.4, separate module) uses it.

This adapter does NOT verify URLs itself — that lives in `url_verify.py` and
is invoked by the orchestration layer so verification can be batched.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import anthropic
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from radar.adapters.base import Activity, EntityRef
from radar.db.payload_schemas import PAYLOAD_SCHEMAS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Activity-type allow-lists per BUILD_PLAN §15
# ---------------------------------------------------------------------------

_ACTIVITY_TYPES_BY_ENTITY_TYPE: dict[str, list[str]] = {
    "civil_society": [
        "POLICY_PAPER",
        "OPEN_LETTER",
        "AMICUS_BRIEF",
        "COMMENT_LETTER",
        "PUBLIC_STATEMENT",
    ],
    "international": [
        "FOREIGN_LEGISLATION",
        "FOREIGN_REGULATION",
        "TREATY_ACTION",
        "OFFICIAL_STATEMENT",
        "BILATERAL_AGREEMENT",
    ],
    "state_local": [
        "STATE_LEGISLATION",
        "STATE_EXEC_ORDER",
        "STATE_AG_ACTION",
        "LOCAL_ORDINANCE",
    ],
    # party_faction has no first-class activities per §15; we still allow
    # legislator-style press_statement / public_statement for member-rollup
    # research, but the planner is expected to compute faction views from
    # member activities. Keep this list minimal and conservative.
    "party_faction": [
        "PUBLIC_STATEMENT",
        "OPEN_LETTER",
    ],
    "company": [
        "PRESS_RELEASE",
        "BLOG_POST",
        "EXEC_PUBLIC_STATEMENT",
        "INVESTMENT_ANNOUNCEMENT",
        "AMICUS_BRIEF",
        "COMMENT_LETTER",
    ],
    "legislator": [
        "PRESS_STATEMENT",
        "FLOOR_SPEECH",
        "LETTER_TO_AGENCY",
    ],
    "legislative_body": [
        "HEARING_HELD",
        "REPORT_RELEASED",
    ],
    "judiciary": [
        "OPINION",
        "ORDER",
        "ORAL_ARGUMENT",
    ],
    "executive_agency": [
        "OFFICIAL_STATEMENT",
        "GUIDANCE",
        "ENFORCEMENT_ACTION",
    ],
}


# ---------------------------------------------------------------------------
# Stable system prompt (cacheable)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a research adapter for an AI-policy tracker.

Your job: given one entity (a civil-society group, international body, state/local actor, or party faction) and a date window, find recent **first-party** activities by that entity using the web_search tool, and emit them as a strict JSON array.

Hard rules — violating any of these is a critical failure:
1. **Never fabricate.** If you are not certain a date, URL, or quote is real and present on the source page, omit the item entirely. No guessing.
2. **First-party sources only.** Prefer the entity's own site (eff.org, brookings.edu, ai-now.org, etc.). Use third-party coverage only when it links to or quotes a primary document; in that case, prefer the primary URL.
3. **Source URL must resolve.** Every item's source_url must be a real, fetchable HTTP(S) URL on the entity's domain (or a primary-source domain like supremecourt.gov / regulations.gov). No paywalled aggregator links, no Google search result URLs, no archive.org links unless the original is dead.
4. **Date in window.** occurred_at must fall within the [since, today] window the user gives you. Items outside the window are silently dropped — do not include them.
5. **raw_text_excerpt is verbatim.** Copy 200–600 characters directly from the page (preferably the lede, headline, or a key sentence). Do not paraphrase. Do not translate. Do not add ellipsis around it.
6. **verify_phrase is a real 5–12 word substring of the page.** Choose a distinctive phrase from the article body that would not appear on an unrelated page. The downstream URL-verifier substring-matches this against the page body — if you invent it, the item gets dropped.
7. **activity_type must be from the allowed list** the user gives you. Pick the most specific fit. If nothing fits, drop the item.

Output format: a single JSON array. Each element:

  {
    "title": "<headline of the activity, plain text>",
    "occurred_at": "YYYY-MM-DD",
    "activity_type": "<one of the allowed types>",
    "source_url": "https://...",
    "raw_text_excerpt": "<200-600 chars copied verbatim from the page>",
    "verify_phrase": "<5-12 word distinctive substring of the page>",
    "payload": { ... optional, see rule 8 below ... }
  }

8. **Optional `payload` object — type-specific best-effort fields.** When the
   source page plainly states type-specific identifiers, include them in a
   `payload` object on the item. Examples:
     - COMMENT_LETTER: `{"agency": "FTC", "docket_id": "FTC-2025-0042"}` if a
       docket number and addressed agency are explicitly named.
     - AMICUS_BRIEF: `{"case_name": "Doe v. Roe", "court": "9th Cir.",
       "docket_number": "23-1234"}` if the case caption is on the page.
     - FOREIGN_LEGISLATION / STATE_LEGISLATION: `{"bill_or_act_id": "...",
       "bill_number": "..."}` for the bill/act ID if shown.
     - POLICY_PAPER, OPEN_LETTER, PUBLIC_STATEMENT, OFFICIAL_STATEMENT, and
       most other types: usually omit `payload` entirely — these are
       free-form and have no identifier-style fields.
   Discipline:
     - **Do NOT invent values.** If a field is not stated verbatim on the
       source page, omit that key. An empty or missing `payload` is fine.
     - Use the exact field names listed above; downstream code consumes them.
     - Do not include `_verify_phrases` in your `payload` — that is added
       automatically from your `verify_phrase`.

When the user asks you to emit, respond with ONLY the JSON array. No prose. No markdown fences. No commentary. If you have nothing, emit `[]`.
"""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PerEntityResearchAdapter:
    """ResearchAdapter using Claude Opus 4.7 + server-side web_search."""

    name = "research_per_entity"
    handles_entity_types = [
        "civil_society",
        "international",
        "state_local",
        "party_faction",
        "company",
        "legislator",
        "legislative_body",
        "judiciary",
        "executive_agency",
    ]

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_results: int = 8,
        max_web_searches: int = 6,
        max_tokens: int = 8000,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in the environment; the research "
                "adapter cannot run. Load it from .env (see radar/main.py)."
            )
        # Let the SDK read ANTHROPIC_API_KEY from os.environ — do NOT pass it
        # explicitly so we never accidentally log it.
        self._client = anthropic.Anthropic()
        self._model = model
        self._max_results = max_results
        self._max_web_searches = max_web_searches
        self._max_tokens = max_tokens

        # Track tokens used across calls in this adapter instance — handy for
        # smoke tests / driver scripts.
        self.last_usage: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Public protocol method
    # ------------------------------------------------------------------

    def discover_and_fetch(self, entity: EntityRef, since: date) -> list[Activity]:
        if entity.entity_type not in self.handles_entity_types:
            log.warning(
                "[%s] entity_type %r not handled by this adapter; skipping",
                self.name,
                entity.entity_type,
            )
            return []

        allowed_types = _ACTIVITY_TYPES_BY_ENTITY_TYPE.get(entity.entity_type, [])
        if not allowed_types:
            log.warning(
                "[%s] no allowed activity_types for entity_type %r; skipping",
                self.name,
                entity.entity_type,
            )
            return []

        today = date.today()

        try:
            raw_response = self._call_model(
                entity=entity,
                since=since,
                today=today,
                allowed_types=allowed_types,
            )
        except RetryError as e:
            log.error(
                "[%s] persistent API failure for entity=%s after retries: %s",
                self.name,
                entity.id,
                _safe_exc(e),
            )
            return []
        except anthropic.APIError as e:
            log.error(
                "[%s] non-retryable API error for entity=%s: %s",
                self.name,
                entity.id,
                _safe_exc(e),
            )
            return []

        items = self._parse_items(raw_response, entity_id=entity.id)
        if not items:
            return []

        activities: list[Activity] = []
        for item in items:
            act = self._build_activity(
                item=item,
                entity=entity,
                since=since,
                today=today,
                allowed_types=allowed_types,
            )
            if act is not None:
                activities.append(act)
            if len(activities) >= self._max_results:
                break

        return activities

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_model(
        self,
        *,
        entity: EntityRef,
        since: date,
        today: date,
        allowed_types: list[str],
    ) -> "anthropic.types.Message":
        user_msg = self._build_user_message(
            entity=entity,
            since=since,
            today=today,
            allowed_types=allowed_types,
        )

        @retry(
            retry=retry_if_exception_type(
                (
                    anthropic.RateLimitError,
                    anthropic.APIConnectionError,
                    anthropic.InternalServerError,
                )
            ),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=2, min=2, max=20),
            reraise=True,
        )
        def _do_call() -> "anthropic.types.Message":
            return self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_web_searches,
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )

        response = _do_call()

        # Capture usage for the driver script. Be defensive — fields may differ
        # across SDK versions.
        try:
            usage = response.usage
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", 0
                )
                or 0,
                "cache_read_input_tokens": getattr(
                    usage, "cache_read_input_tokens", 0
                )
                or 0,
            }
        except Exception:  # noqa: BLE001
            self.last_usage = None

        return response

    def _build_user_message(
        self,
        *,
        entity: EntityRef,
        since: date,
        today: date,
        allowed_types: list[str],
    ) -> str:
        aliases_str = ", ".join(entity.aliases) if entity.aliases else "(none)"
        jurisdiction = entity.jurisdiction or "(unspecified)"
        types_str = ", ".join(allowed_types)

        return (
            f"Entity: {entity.name}\n"
            f"Aliases: {aliases_str}\n"
            f"Entity type: {entity.entity_type}\n"
            f"Jurisdiction: {jurisdiction}\n"
            f"Date window: {since.isoformat()} to {today.isoformat()} (inclusive)\n"
            f"Allowed activity_types: {types_str}\n"
            f"Maximum items: {self._max_results}\n\n"
            "Use the web_search tool to find first-party activities by this entity "
            "in the date window. Focus on the entity's own website and primary-source "
            "domains. Then emit a single JSON array following the schema in the system "
            "prompt — JSON only, no prose, no fences. If you find nothing credible, "
            "emit []."
        )

    def _parse_items(
        self,
        response: "anthropic.types.Message",
        *,
        entity_id: str,
    ) -> list[dict[str, Any]]:
        # Concatenate any final text blocks (post-tool-use) — that's where the
        # JSON array lives.
        text_chunks: list[str] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_chunks.append(block.text)
        full_text = "\n".join(text_chunks).strip()

        if not full_text:
            log.warning(
                "[%s] empty model output for entity=%s (stop_reason=%s)",
                self.name,
                entity_id,
                getattr(response, "stop_reason", "?"),
            )
            return []

        # Try strict JSON first.
        json_text = _extract_json_array(full_text)
        if json_text is None:
            log.warning(
                "[%s] no JSON array found in model output for entity=%s; "
                "first 300 chars: %s",
                self.name,
                entity_id,
                full_text[:300].replace("\n", " "),
            )
            return []

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as e:
            log.warning(
                "[%s] JSON parse failed for entity=%s: %s; first 300 chars: %s",
                self.name,
                entity_id,
                e,
                json_text[:300].replace("\n", " "),
            )
            return []

        if not isinstance(parsed, list):
            log.warning(
                "[%s] model output was not a JSON array for entity=%s (got %s)",
                self.name,
                entity_id,
                type(parsed).__name__,
            )
            return []

        out: list[dict[str, Any]] = []
        for elem in parsed:
            if isinstance(elem, dict):
                out.append(elem)
        return out

    def _build_activity(
        self,
        *,
        item: dict[str, Any],
        entity: EntityRef,
        since: date,
        today: date,
        allowed_types: list[str],
    ) -> Activity | None:
        title = (item.get("title") or "").strip()
        source_url = (item.get("source_url") or "").strip()
        activity_type = (item.get("activity_type") or "").strip()
        raw_excerpt = (item.get("raw_text_excerpt") or "").strip()
        verify_phrase = (item.get("verify_phrase") or "").strip()
        occurred_at_raw = item.get("occurred_at")

        if not title or not source_url:
            log.info(
                "[%s] dropping item for entity=%s: missing title or source_url",
                self.name,
                entity.id,
            )
            return None

        if not source_url.lower().startswith(("http://", "https://")):
            log.info(
                "[%s] dropping item for entity=%s: non-http source_url=%r",
                self.name,
                entity.id,
                source_url,
            )
            return None

        if activity_type not in PAYLOAD_SCHEMAS:
            log.info(
                "[%s] dropping item for entity=%s: unknown activity_type=%r",
                self.name,
                entity.id,
                activity_type,
            )
            return None

        if activity_type not in allowed_types:
            log.info(
                "[%s] dropping item for entity=%s: activity_type=%r not allowed "
                "for entity_type=%r",
                self.name,
                entity.id,
                activity_type,
                entity.entity_type,
            )
            return None

        occurred_at = _parse_date(occurred_at_raw)
        if occurred_at is None:
            log.info(
                "[%s] dropping item for entity=%s: unparseable occurred_at=%r",
                self.name,
                entity.id,
                occurred_at_raw,
            )
            return None

        # Allow a small tolerance on the upper bound (clock skew / TZ).
        if occurred_at < since or occurred_at > today + timedelta(days=1):
            log.info(
                "[%s] dropping item for entity=%s: occurred_at=%s outside window "
                "[%s, %s]",
                self.name,
                entity.id,
                occurred_at.isoformat(),
                since.isoformat(),
                today.isoformat(),
            )
            return None

        if not raw_excerpt:
            log.info(
                "[%s] dropping item for entity=%s: empty raw_text_excerpt",
                self.name,
                entity.id,
            )
            return None

        verify_phrases: list[str] = []
        if verify_phrase:
            verify_phrases.append(verify_phrase)

        # Merge any LLM-emitted `payload` keys (type-specific identifiers like
        # `case_name`, `docket_id`, `agency`, etc.) alongside our internal
        # `_verify_phrases`. The LLM is instructed to omit fields it cannot
        # source from the page; we never overwrite `_verify_phrases`.
        emitted_payload = item.get("payload")
        if not isinstance(emitted_payload, dict):
            emitted_payload = {}
        # Strip any attempt by the model to set internal/reserved keys.
        emitted_payload = {
            k: v for k, v in emitted_payload.items() if not k.startswith("_")
        }

        payload: dict[str, Any] = {
            "_verify_phrases": verify_phrases,
            **emitted_payload,
        }

        return Activity(
            entity_id=entity.id,
            entity_type=entity.entity_type,
            activity_type=activity_type,
            occurred_at=occurred_at,
            source_url=source_url,
            source_adapter=self.name,
            title=title,
            raw_text=raw_excerpt,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Accept YYYY-MM-DD (most common) and YYYY/MM/DD.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Fallback: best-effort ISO parse.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", re.DOTALL)


def _extract_json_array(text: str) -> str | None:
    """Pull the first top-level JSON array from `text`.

    The model is instructed to emit JSON-only, but we tolerate a stray prefix
    like ```json … ``` or a single line of preamble.
    """
    s = text.strip()

    # Strip markdown code fences if present.
    if s.startswith("```"):
        # Drop opening fence line.
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[: -len("```")].rstrip()

    if s.startswith("["):
        return s

    # Find the first balanced array. Use a simple bracket walker — regex above
    # is unreliable for deeply nested objects.
    start = s.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _safe_exc(e: BaseException) -> str:
    """Short, key-free exception summary for log lines."""
    msg = str(e)
    # Belt-and-suspenders: never let an `sk-...` substring leak into a log line.
    msg = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-REDACTED", msg)
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return f"{type(e).__name__}: {msg}"


__all__ = ["PerEntityResearchAdapter"]
