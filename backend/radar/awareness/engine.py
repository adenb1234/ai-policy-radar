"""Awareness engine coordinator (BUILD_PLAN §9).

Glues Layer 1 (structured retrieval), Layer 2 (embedding rerank), and
Layer 3 (Opus reasoner) into a single end-to-end call. Persists the
top_k AwarenessItems into the `awareness_item` table so the dashboard
can read them without re-running the LLM rerank.

Live-agent fallback (BUILD_PLAN §9.2) is **deferred** — when retrieval
returns thinner-than-threshold coverage we log a `[fallback]` placeholder
and continue with what we have. TODO 4.4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from radar.awareness.actions import serialize_actions, serialize_citations
from radar.awareness.reasoner import AwarenessItem, AwarenessReasoner
from radar.awareness.retrieval import (
    CandidateActivity,
    layer1_structured,
    layer2_embedding_rerank,
)
from radar.profiles.builder import StructuredProfile, get_profile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_awareness_item_id(user_id: str, activity_id: str, generated_at: str) -> str:
    """Stable id per (user, activity, generation timestamp).

    Hashing keeps it short while still letting two regenerations for the
    same user+activity coexist (different generated_at).
    """
    payload = f"{user_id}|{activity_id}|{generated_at}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AwarenessEngine:
    """End-to-end awareness pipeline."""

    def __init__(self, *, reasoner: Optional[AwarenessReasoner] = None) -> None:
        self._reasoner = reasoner or AwarenessReasoner()
        # Keep a place for the most recent run's diagnostics so test scripts
        # can show layer counts + cache stats.
        self.last_diagnostics: dict = {}

    async def build_dashboard(
        self,
        conn: sqlite3.Connection,
        profile_id: str,
        *,
        since: Optional[date] = None,
        top_k: int = 15,
        live_agent_threshold: int = 10,
    ) -> list[AwarenessItem]:
        """Build the awareness dashboard for a profile.

        Steps:
          1. Load the profile from the DB.
          2. Layer 1 structured retrieval (filter by since).
          3. Layer 2 embedding rerank (graceful fallback to identity).
          4. If candidate count < live_agent_threshold: log a deferred-fallback
             placeholder and continue.
          5. Layer 3 Opus rerank → top_k AwarenessItems.
          6. Persist to `awareness_item`.
          7. Return.
        """
        loaded = get_profile(conn, profile_id)
        if loaded is None:
            raise ValueError(f"profile {profile_id!r} not found")
        _name, nl_description, profile = loaded

        if since is None:
            recency_days = profile.recency_days or 30
            since = date.today() - timedelta(days=int(recency_days))

        # Layer 1
        layer1_candidates = layer1_structured(conn, profile, since=since)
        n_layer1 = len(layer1_candidates)
        log.info(
            "[awareness.engine] Layer 1 returned %d candidates (since=%s)",
            n_layer1,
            since,
        )

        # Layer 2
        layer2_candidates = layer2_embedding_rerank(
            conn,
            profile,
            nl_description,
            layer1_candidates,
        )
        n_layer2 = len(layer2_candidates)
        log.info(
            "[awareness.engine] Layer 2 returned %d candidates", n_layer2
        )

        # §9.2 fallback — deferred. If retrieval is thin, log + continue.
        if n_layer2 < live_agent_threshold:
            log.info(
                "[awareness.engine] [fallback] thin coverage (%d < %d) — live-agent "
                "fallback DEFERRED (TODO 4.4); continuing with %d candidates",
                n_layer2,
                live_agent_threshold,
                n_layer2,
            )

        # Layer 3
        items = await self._reasoner.rerank(
            profile=profile,
            profile_nl_description=nl_description,
            candidates=layer2_candidates,
            top_k=top_k,
        )
        n_layer3 = len(items)
        log.info("[awareness.engine] Layer 3 returned %d items", n_layer3)

        # Persist
        self._persist_items(conn, user_id=profile_id, items=items)

        self.last_diagnostics = {
            "since": since.isoformat(),
            "layer1_count": n_layer1,
            "layer2_count": n_layer2,
            "layer3_count": n_layer3,
            "batch_usages": list(self._reasoner.batch_usages),
        }
        return items

    # ------------------------------------------------------------------

    @staticmethod
    def _persist_items(
        conn: sqlite3.Connection,
        *,
        user_id: str,
        items: list[AwarenessItem],
    ) -> None:
        """INSERT each AwarenessItem into `awareness_item`.

        We don't dedupe — a fresh build_dashboard() generates a new set of
        rows (different generated_at). Callers that want a single live row
        per (user, activity) should DELETE FROM awareness_item WHERE
        user_id = ? before invoking. The schema has no unique constraint
        on (user_id, activity_id), so doing both is safe.
        """
        if not items:
            return
        now = _now_iso()
        rows = []
        for it in items:
            row_id = _make_awareness_item_id(user_id, it.activity_id, now)
            rows.append(
                (
                    row_id,
                    user_id,
                    it.activity_id,
                    now,
                    float(it.relevance_score),
                    it.reasoning or "",
                    serialize_actions(it.recommended_actions),
                    serialize_citations(it.citations),
                )
            )
        try:
            conn.executemany(
                """
                INSERT INTO awareness_item (
                    id, user_id, activity_id, generated_at,
                    relevance_score, reasoning, recommended_actions, citations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # Most likely a FK violation — log but don't fail the call.
            log.warning(
                "[awareness.engine] failed to persist awareness_items "
                "(IntegrityError: %s) — caller can re-run after upserting refs",
                e,
            )


__all__ = ["AwarenessEngine"]
