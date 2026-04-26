"""URL verification for research-adapter outputs (BUILD_PLAN §7.4).

Every Activity produced by a ResearchAdapter MUST be re-fetched and the
returned page body MUST contain a substring match for at least one of:
- the activity title (lowercased + trimmed),
- the first 80 chars of activity.raw_text (lowercased), or
- any phrase in activity.payload['_verify_phrases'] (lowercased).

Failures fall into one of: http_<status>, timeout, network, no_phrase_match,
empty_body. The orchestration layer drops failures and logs the reason; we
never raise out of this module on per-activity verification.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx

from radar.adapters.base import Activity

log = logging.getLogger(__name__)

_USER_AGENT = (
    "AI-Policy-Radar/0.1 (research-adapter URL verifier; "
    "+https://example.invalid/ai-policy-radar)"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Common boilerplate suffixes/prefixes we strip from titles before matching.
_TITLE_BOILERPLATE = re.compile(
    r"\s*(?:\||—|–|-)\s*(?:electronic frontier foundation|eff|brookings|"
    r"center for ai safety|cais|ai now institute|ai now|future of life institute|"
    r"fli|aclu|the heritage foundation|heritage|home|news|press|press release)\s*$",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Lowercase, strip HTML tags, collapse whitespace."""
    text = _TAG_RE.sub(" ", text)
    text = text.lower()
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_title_boilerplate(title: str) -> str:
    title = _TITLE_BOILERPLATE.sub("", title)
    return title.strip().lower()


def _candidate_phrases(activity: Activity) -> list[str]:
    """Return de-duped, normalized phrases worth substring-matching."""
    phrases: list[str] = []

    title = (activity.title or "").strip()
    if title:
        phrases.append(_strip_title_boilerplate(title))
        # Also keep the raw lowercased title as a fallback.
        phrases.append(title.lower())

    raw = (activity.raw_text or "").strip()
    if raw:
        phrases.append(raw[:80].lower())

    payload = activity.payload or {}
    explicit = payload.get("_verify_phrases") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    for p in explicit:
        if isinstance(p, str) and p.strip():
            phrases.append(p.strip().lower())

    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        p = _WS_RE.sub(" ", p).strip()
        if len(p) < 8:  # too short to be discriminative
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def verify_activity(activity: Activity, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Re-fetch activity.source_url and confirm the page contains at least one
    of the activity's identifying phrases.

    Returns (passed, reason). On success, reason is "ok".
    On failure, reason is one of: http_<status>, timeout, network,
    no_phrase_match, empty_body.
    """
    url = (activity.source_url or "").strip()
    if not url:
        return False, "network"

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_exc_reason = "network"
    body: str | None = None
    status: int | None = None

    # Single retry on transient network errors.
    for attempt in range(2):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                headers=headers,
            ) as client:
                resp = client.get(url)
            status = resp.status_code
            if 200 <= status < 300:
                body = resp.text
                break
            # Non-2xx: stop and report.
            return False, f"http_{status}"
        except httpx.TimeoutException:
            last_exc_reason = "timeout"
        except httpx.HTTPError:
            last_exc_reason = "network"
        except Exception:  # noqa: BLE001 — defensive; we log generic upstream
            last_exc_reason = "network"

    if body is None:
        return False, last_exc_reason

    if not body.strip():
        return False, "empty_body"

    haystack = _normalize(body)
    if not haystack:
        return False, "empty_body"

    for phrase in _candidate_phrases(activity):
        if phrase and phrase in haystack:
            return True, "ok"

    return False, "no_phrase_match"


def verify_batch(
    activities: Iterable[Activity],
    *,
    timeout: float = 15.0,
) -> tuple[list[Activity], list[tuple[Activity, str]]]:
    """Verify a batch; return (passed, failed_with_reasons)."""
    passed: list[Activity] = []
    failed: list[tuple[Activity, str]] = []
    for act in activities:
        ok, reason = verify_activity(act, timeout=timeout)
        if ok:
            passed.append(act)
        else:
            failed.append((act, reason))
    return passed, failed


__all__ = ["verify_activity", "verify_batch"]
