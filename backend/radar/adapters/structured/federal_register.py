"""Federal Register structured adapter (BUILD_PLAN §7.1, TODO 2.5).

Hits the keyless public Federal Register API to surface AI-related rulemakings,
executive orders, RFIs, and notices issued by `executive_agency` entities.

API root: https://www.federalregister.gov/api/v1/

`discover()` issues an AI-keyword-tagged query and filters the result list to
documents whose `agencies` array matches the supplied entity (by slug, name,
or alias). `fetch()` pulls the full document detail and constructs an Activity
with a type-specific payload validated against `payload_schemas.validate_payload`.

The FR API is keyless and rate-friendly. We're polite: a sensible User-Agent,
exponential-backoff retries on 5xx / 429 / network errors, and serial requests
in any test harness that wraps this adapter.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from radar.adapters.base import Activity, ActivityStub, EntityRef
from radar.db.payload_schemas import validate_payload

log = logging.getLogger(__name__)

_API_ROOT = "https://www.federalregister.gov/api/v1"
_USER_AGENT = "ai-policy-radar/0.1 (research)"
_MAX_STUBS = 50
_LIST_FIELDS = [
    "document_number",
    "title",
    "type",
    "publication_date",
    "html_url",
    "abstract",
    "agencies",
    "docket_ids",
    "action",
    "cfr_references",
    "comments_close_on",
    "executive_order_number",
]


# ---------------------------------------------------------------------------
# Retryable transient-error envelope
# ---------------------------------------------------------------------------


class _TransientFRError(Exception):
    """Raised on 5xx/429/network errors so tenacity retries them."""


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (_TransientFRError, httpx.TransportError, httpx.TimeoutException))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class FederalRegisterAdapter:
    """Structured adapter for federalregister.gov."""

    name = "federal_register"
    handles_entity_types = ["executive_agency"]

    AI_KEYWORDS = [
        "artificial intelligence",
        "machine learning",
        "automated decision",
        "generative AI",
        "foundation model",
        "large language model",
        "algorithmic system",
        "AI safety",
    ]

    def __init__(self, *, http_timeout: float = 20.0) -> None:
        self._timeout = http_timeout
        self._client = httpx.Client(
            timeout=http_timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (_TransientFRError, httpx.TransportError, httpx.TimeoutException)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.0, min=1.0, max=10.0),
        reraise=True,
    )
    def _get_json(self, url: str, params: dict | list[tuple[str, str]] | None = None) -> dict:
        try:
            resp = self._client.get(url, params=params)
        except (httpx.TransportError, httpx.TimeoutException):
            raise
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            raise _TransientFRError(f"transient {resp.status_code} from {url}")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # discover()
    # ------------------------------------------------------------------

    def discover(self, entity: EntityRef, since: date) -> list[ActivityStub]:
        """Query FR for AI-keyword docs since `since`; filter to entity match.

        Returns up to 50 ActivityStubs. The stub's `extra` carries the FR
        `document_number` and the resolved activity_type so `fetch()` can
        skip re-deriving the mapping.
        """
        match_keys = self._entity_match_keys(entity)
        if not match_keys:
            log.warning("federal_register.discover: entity %s has no usable match keys", entity.id)
            return []

        # First try a single OR-joined query. Fall back to per-keyword if it
        # errors OR returns suspiciously few results (FR's term parser has
        # quirks with quoted-OR — empirically the boolean-OR form sometimes
        # returns 0 hits while per-keyword unions hundreds).
        items = self._query_or_joined(since)
        if items is None or len(items) < 5:
            log.info(
                "federal_register: OR query returned %s items; using per-keyword fallback",
                "None" if items is None else len(items),
            )
            items = self._query_per_keyword(since)

        stubs: list[ActivityStub] = []
        seen: set[str] = set()
        for doc in items:
            docnum = doc.get("document_number")
            if not docnum or docnum in seen:
                continue
            if not self._agencies_match(doc.get("agencies") or [], match_keys):
                continue

            seen.add(docnum)
            activity_type = self._map_activity_type(doc)
            stub = ActivityStub(
                source_url=doc.get("html_url") or "",
                title=doc.get("title") or "",
                occurred_at=doc.get("publication_date") or "",
                activity_type=activity_type,
                extra={
                    "document_number": docnum,
                    "fr_type": doc.get("type"),
                    "resolved_activity_type": activity_type,
                },
            )
            stubs.append(stub)
            if len(stubs) >= _MAX_STUBS:
                break

        return stubs

    def _query_or_joined(self, since: date) -> list[dict] | None:
        # FR's term parser narrows hard when quotes are used; rely on its own
        # phrase handling. Boolean OR is supported but empirically collapses
        # recall in some windows, so this is best-effort and we fall back to
        # per-keyword on either error or low-yield.
        term = " OR ".join(self.AI_KEYWORDS)
        params: list[tuple[str, str]] = [
            ("conditions[term]", term),
            ("conditions[publication_date][gte]", since.isoformat()),
            ("per_page", "100"),
            ("order", "newest"),
        ]
        for f in _LIST_FIELDS:
            params.append(("fields[]", f))
        try:
            data = self._get_json(f"{_API_ROOT}/documents", params=params)
        except Exception as exc:  # noqa: BLE001
            log.warning("federal_register: OR-joined query failed (%s); falling back per-keyword", exc)
            return None
        return list(data.get("results") or [])

    def _query_per_keyword(self, since: date) -> list[dict]:
        merged: dict[str, dict] = {}
        for kw in self.AI_KEYWORDS:
            params: list[tuple[str, str]] = [
                ("conditions[term]", kw),
                ("conditions[publication_date][gte]", since.isoformat()),
                ("per_page", "100"),
                ("order", "newest"),
            ]
            for f in _LIST_FIELDS:
                params.append(("fields[]", f))
            try:
                data = self._get_json(f"{_API_ROOT}/documents", params=params)
            except Exception as exc:  # noqa: BLE001
                log.warning("federal_register: per-keyword query failed for %r (%s)", kw, exc)
                continue
            for item in data.get("results") or []:
                docnum = item.get("document_number")
                if docnum and docnum not in merged:
                    merged[docnum] = item
        return list(merged.values())

    # ------------------------------------------------------------------
    # fetch()
    # ------------------------------------------------------------------

    def fetch(self, entity: EntityRef, stub: ActivityStub) -> Activity | None:
        """Build a full Activity from a stub. Returns None on failure."""
        docnum = stub.extra.get("document_number")
        if not docnum:
            log.warning("federal_register.fetch: stub missing document_number; skipping")
            return None

        try:
            doc = self._get_json(f"{_API_ROOT}/documents/{docnum}.json")
        except Exception as exc:  # noqa: BLE001
            log.warning("federal_register.fetch: detail fetch failed for %s (%s)", docnum, exc)
            return None

        activity_type = stub.extra.get("resolved_activity_type") or self._map_activity_type(doc)
        payload = self._build_payload(activity_type, doc)

        try:
            validate_payload(activity_type, payload)
        except (ValidationError, ValueError) as exc:
            # Pull a short summary of missing/invalid fields if available.
            missing = self._summarize_validation_error(exc)
            log.warning(
                "federal_register.fetch: payload validation failed doc=%s type=%s missing=%s",
                docnum,
                activity_type,
                missing,
            )
            return None

        title = doc.get("title") or stub.title or ""
        abstract = doc.get("abstract") or ""
        action = doc.get("action") or ""
        raw_text = f"{title}\n\n{abstract}\n\n{action}".strip()

        occurred_at = self._parse_date(doc.get("publication_date")) or stub.occurred_at
        source_url = doc.get("html_url") or stub.source_url

        return Activity(
            entity_id=entity.id,
            entity_type=entity.entity_type,
            activity_type=activity_type,
            occurred_at=occurred_at,
            source_url=source_url,
            source_adapter=self.name,
            title=title,
            raw_text=raw_text,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entity_match_keys(entity: EntityRef) -> set[str]:
        keys: set[str] = set()
        if entity.name:
            keys.add(entity.name.strip().lower())
        for alias in entity.aliases or []:
            if alias:
                keys.add(alias.strip().lower())
        # Also accept the entity id itself (often a slug-like form).
        if entity.id:
            keys.add(entity.id.strip().lower())
        slug = (entity.metadata or {}).get("fr_agency_slug")
        if slug:
            keys.add(str(slug).strip().lower())
        # Drop obvious non-discriminating tokens.
        keys.discard("")
        return keys

    @staticmethod
    def _tokenize(s: str) -> set[str]:
        out: set[str] = set()
        token = []
        for ch in s.lower():
            if ch.isalnum():
                token.append(ch)
            else:
                if token:
                    out.add("".join(token))
                    token = []
        if token:
            out.add("".join(token))
        return out

    @classmethod
    def _agencies_match(cls, agencies: list[dict], match_keys: set[str]) -> bool:
        """Match an FR `agencies` array against entity match keys.

        Strategy (avoids the 'nist' ⊂ 'administration' false-positive):
          - Exact match on slug / name / raw_name (case-insensitive, normalized).
          - Token-boundary match: tokenize the agency strings, tokenize the
            key, and require every token of the key to appear in the agency's
            token set. (So "nist" matches "...nist..." only if "nist" is a
            standalone token, not a substring of "administration".)
          - Multi-word phrase match for long keys (len >= 12) via substring
            on the lowercase joined name — handles "national institute of
            standards and technology" matching "...national institute of...".
        """
        for agency in agencies or []:
            slug = (agency.get("slug") or "").strip().lower()
            name = (agency.get("name") or "").strip().lower()
            raw_name = (agency.get("raw_name") or "").strip().lower()
            if not (slug or name or raw_name):
                continue

            agency_strs = [s for s in (slug, name, raw_name) if s]
            agency_tokens: set[str] = set()
            for s in agency_strs:
                agency_tokens |= cls._tokenize(s)

            for key in match_keys:
                if not key:
                    continue
                key_norm = key.strip().lower()
                # 1. Exact match on any agency string.
                if key_norm in agency_strs:
                    return True
                # 2. Slug equality after normalizing dashes to spaces.
                if slug and key_norm == slug.replace("-", " "):
                    return True
                # 3. Token-set match: every token in the key must appear in
                #    the agency's token set as a standalone token.
                key_tokens = cls._tokenize(key_norm)
                if key_tokens and key_tokens.issubset(agency_tokens):
                    return True
                # 4. Long phrase substring match on full names.
                if len(key_norm) >= 12 and (key_norm in name or key_norm in raw_name):
                    return True
        return False

    @staticmethod
    def _map_activity_type(doc: dict) -> str:
        fr_type = (doc.get("type") or "").strip().lower()
        title = (doc.get("title") or "").lower()
        action = (doc.get("action") or "").lower()
        haystack = f"{title} {action}"

        if fr_type == "rule":
            return "FINAL_RULE"
        if fr_type == "proposed rule":
            return "NPRM"
        if fr_type == "presidential document":
            if doc.get("executive_order_number"):
                return "EXECUTIVE_ORDER"
            return "OFFICIAL_STATEMENT"
        if fr_type == "notice":
            if "request for information" in haystack or "request for comment" in haystack:
                return "RFI"
            if "guidance" in haystack or "policy statement" in haystack:
                return "GUIDANCE"
            return "OFFICIAL_STATEMENT"
        # Unknown FR type — default to OFFICIAL_STATEMENT to keep the row.
        return "OFFICIAL_STATEMENT"

    @staticmethod
    def _cfr_strings(doc: dict) -> list[str]:
        out: list[str] = []
        for ref in doc.get("cfr_references") or []:
            if isinstance(ref, dict):
                title = ref.get("title")
                part = ref.get("part")
                chapter = ref.get("chapter")
                if title and part:
                    out.append(f"{title} CFR {part}")
                elif title and chapter:
                    out.append(f"{title} CFR Chapter {chapter}")
                elif title:
                    out.append(f"{title} CFR")
            elif isinstance(ref, str):
                out.append(ref)
        return out

    @staticmethod
    def _agencies_summary(doc: dict) -> list[dict]:
        out = []
        for ag in doc.get("agencies") or []:
            if isinstance(ag, dict):
                out.append(
                    {
                        "id": ag.get("id"),
                        "slug": ag.get("slug"),
                        "name": ag.get("name") or ag.get("raw_name"),
                    }
                )
        return out

    @staticmethod
    def _parse_date(s: Any) -> date | str:
        if not s:
            return ""
        if isinstance(s, date):
            return s
        try:
            return datetime.strptime(str(s), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return str(s)

    @staticmethod
    def _signing_president_for(pub_date: Any) -> str | None:
        d = FederalRegisterAdapter._parse_date(pub_date)
        if not isinstance(d, date):
            return None
        # Inauguration dates: each EO carries the president-of-the-day at
        # publication. The FR API doesn't tag this, so we infer.
        if d >= date(2025, 1, 20):
            return "Trump"
        if d >= date(2021, 1, 20):
            return "Biden"
        if d >= date(2017, 1, 20):
            return "Trump"
        if d >= date(2009, 1, 20):
            return "Obama"
        return None

    def _build_payload(self, activity_type: str, doc: dict) -> dict:
        agencies = self._agencies_summary(doc)
        docket_ids = list(doc.get("docket_ids") or [])
        cfr = self._cfr_strings(doc)
        action = doc.get("action") or None
        fr_type = doc.get("type")

        common_extras = {
            "agencies": agencies,
            "docket_ids": docket_ids,
            "fr_type": fr_type,
            "action": action,
            "document_number": doc.get("document_number"),
        }

        if activity_type == "NPRM":
            payload = {
                "docket_id": docket_ids[0] if docket_ids else "",
                "cfr_citations": cfr,
                "comment_period_close": self._parse_date(doc.get("comments_close_on")) or None,
            }
            payload.update({k: v for k, v in common_extras.items() if k != "docket_ids"})
            # Coerce date-like values back to date objects for pydantic.
            if isinstance(payload["comment_period_close"], str) and not payload["comment_period_close"]:
                payload["comment_period_close"] = None
            return payload

        if activity_type == "FINAL_RULE":
            payload = {
                "docket_id": docket_ids[0] if docket_ids else "",
                "cfr_citations": cfr,
            }
            payload.update({k: v for k, v in common_extras.items() if k != "docket_ids"})
            return payload

        if activity_type == "EXECUTIVE_ORDER":
            payload: dict[str, Any] = {
                "eo_number": str(doc.get("executive_order_number"))
                if doc.get("executive_order_number")
                else None,
            }
            pres = self._signing_president_for(doc.get("publication_date"))
            if pres is not None:
                payload["signing_president"] = pres
            # Note: signing_president is required by the schema. If we genuinely
            # can't infer, the validator will drop the activity (and we log).
            payload.update(common_extras)
            return payload

        if activity_type == "RFI":
            payload = {
                "docket_id": docket_ids[0] if docket_ids else "",
                "response_due": self._parse_date(doc.get("comments_close_on")) or None,
            }
            if isinstance(payload["response_due"], str) and not payload["response_due"]:
                payload["response_due"] = None
            payload.update({k: v for k, v in common_extras.items() if k != "docket_ids"})
            return payload

        if activity_type == "GUIDANCE":
            payload = {}
            payload.update(common_extras)
            return payload

        # OFFICIAL_STATEMENT (default fall-through)
        payload = {}
        payload.update(common_extras)
        return payload

    @staticmethod
    def _summarize_validation_error(exc: BaseException) -> str:
        if isinstance(exc, ValidationError):
            try:
                fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
                return ",".join(fields) or "<unknown>"
            except Exception:  # noqa: BLE001
                return "<errors>"
        return str(exc)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "FederalRegisterAdapter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["FederalRegisterAdapter"]
