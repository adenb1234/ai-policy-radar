"""Awareness engine — Layer 1 (structured) + Layer 2 (embedding rerank).

BUILD_PLAN §9.1.

Layer 1: cheap SQL filter. Joins activity + enrichment, applies profile
filters (entity_type, activity_type, jurisdiction, topic intersection, watch
entity intersection), produces up to 200 candidates with a structured score.

Layer 2: vector rerank. Embeds the profile NL description + top-topic names
to one query vector, looks up each candidate's stored embedding from the
sqlite-vec virtual table, computes cosine similarity, and merges with the
structured score using configurable weights. Returns top 30.

Graceful degradation: if either the embedding model isn't installed OR no
candidate has a stored embedding, Layer 2 falls back to identity scoring —
combined_score = structured_score, embedding_score = None. The pipeline
continues without erroring.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from radar.awareness.embedding_model import EmbeddingModel
from radar.profiles.builder import StructuredProfile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Caps from BUILD_PLAN §9.1
# ---------------------------------------------------------------------------

LAYER1_MAX = 200
LAYER2_MAX = 30
RECENT_DAYS_BOOST = 7


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------


@dataclass
class CandidateActivity:
    """A scored candidate flowing through the awareness pipeline."""

    activity_id: str
    activity_row: dict  # joined activity + enrichment columns
    structured_score: float
    embedding_score: Optional[float] = None
    combined_score: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(blob: Any, default: Any) -> Any:
    """Parse a JSON-text column. Return `default` on any failure."""
    if blob is None or blob == "":
        return default
    if isinstance(blob, (list, dict)):
        return blob
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return default


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, decoding the JSON columns."""
    d = dict(row)
    d["topics_list"] = _parse_json(d.get("topics"), [])
    d["mentioned_entities_list"] = _parse_json(d.get("mentioned_entities"), [])
    d["materiality_obj"] = _parse_json(d.get("materiality"), {})
    return d


# ---------------------------------------------------------------------------
# Layer 1 — structured filter + score
# ---------------------------------------------------------------------------


def layer1_structured(
    conn: sqlite3.Connection,
    profile: StructuredProfile,
    *,
    since: date,
) -> list[CandidateActivity]:
    """Return up to LAYER1_MAX candidates scored by structured features.

    Filters (any one of the topic/entity matches qualifies a row):
      - occurred_at >= since
      - jurisdiction match: profile.jurisdictions ∩ entity.jurisdiction OR
        entity.jurisdiction in {"global", "sector"} OR profile has no
        jurisdiction filter
      - entity_type filter (if profile.entity_types is non-empty)
      - activity_type filter (if profile.activity_type_filters is set)
      - AT LEAST ONE OF:
          * topics intersects profile.topics_weighted keys
          * mentioned_entities intersects profile.watch_entities
          * activity.entity_id ∈ profile.watch_entities

    Score components (sum):
      + sum(profile.topics_weighted[t] for t in matched topics)
      + 1.5 if activity.entity_id ∈ profile.watch_entities
      + 1.0 per mentioned_entity ∈ profile.watch_entities
      + 0.5 if occurred_at within last RECENT_DAYS_BOOST days

    Activities with score 0 are excluded.
    """
    since_iso = since.isoformat() if isinstance(since, date) else str(since)

    # Pull a generous superset, then filter+score in Python. The SQL filter
    # narrows by date and entity_type/activity_type/jurisdiction; topic and
    # watch-entity intersection is done in Python because SQLite doesn't have
    # a native JSON intersection operator.
    where_parts = ["a.occurred_at >= ?"]
    params: list[Any] = [since_iso]

    if profile.entity_types:
        placeholders = ",".join("?" for _ in profile.entity_types)
        where_parts.append(f"a.entity_type IN ({placeholders})")
        params.extend(profile.entity_types)

    if profile.activity_type_filters:
        placeholders = ",".join("?" for _ in profile.activity_type_filters)
        where_parts.append(f"a.activity_type IN ({placeholders})")
        params.extend(profile.activity_type_filters)

    if profile.jurisdictions:
        # Match if entity jurisdiction is in profile list OR is global/sector
        # OR is NULL (treat NULL as global by convention).
        juris_placeholders = ",".join("?" for _ in profile.jurisdictions)
        where_parts.append(
            "(e.jurisdiction IN ({plc}) OR e.jurisdiction IN ('global','sector') "
            "OR e.jurisdiction IS NULL OR e.jurisdiction = '')".format(
                plc=juris_placeholders
            )
        )
        params.extend(profile.jurisdictions)

    where_sql = " AND ".join(where_parts)
    sql = f"""
        SELECT
            a.id              AS activity_id,
            a.entity_id       AS entity_id,
            a.entity_type     AS entity_type,
            a.activity_type   AS activity_type,
            a.occurred_at     AS occurred_at,
            a.source_url      AS source_url,
            a.title           AS title,
            a.payload         AS payload,
            e.name            AS entity_name,
            e.jurisdiction    AS jurisdiction,
            en.summary        AS summary,
            en.topics         AS topics,
            en.mentioned_entities AS mentioned_entities,
            en.stance         AS stance,
            en.stance_quote   AS stance_quote,
            en.materiality    AS materiality
        FROM activity a
        JOIN entity e ON e.id = a.entity_id
        LEFT JOIN enrichment en ON en.activity_id = a.id
        WHERE {where_sql}
        ORDER BY a.occurred_at DESC
        LIMIT ?
    """
    # Pull a generous bound so post-filter still has plenty.
    params.append(LAYER1_MAX * 4)

    cursor = conn.execute(sql, params)
    rows = [_row_to_dict(r) for r in cursor.fetchall()]

    topic_weights = profile.topics_weighted or {}
    watch_set = set(profile.watch_entities or [])
    today = date.today()

    candidates: list[CandidateActivity] = []
    for row in rows:
        topics: list[str] = [str(t) for t in row.get("topics_list", []) if isinstance(t, str)]
        mentioned: list[str] = [
            str(m) for m in row.get("mentioned_entities_list", []) if isinstance(m, str)
        ]
        entity_id = row.get("entity_id")

        matched_topics = [t for t in topics if t in topic_weights]
        matched_mentioned = [m for m in mentioned if m in watch_set]
        is_source_watched = bool(entity_id and entity_id in watch_set)

        # Must match at least one signal.
        if not matched_topics and not matched_mentioned and not is_source_watched:
            continue

        score = 0.0
        for t in matched_topics:
            try:
                score += float(topic_weights.get(t, 0.0))
            except (TypeError, ValueError):
                continue
        if is_source_watched:
            score += 1.5
        score += 1.0 * len(matched_mentioned)

        # Recency boost
        occ_str = row.get("occurred_at") or ""
        try:
            occ = datetime.fromisoformat(occ_str.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                occ = datetime.strptime(str(occ_str)[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                occ = None
        if occ is not None and (today - occ) <= timedelta(days=RECENT_DAYS_BOOST):
            score += 0.5

        if score <= 0.0:
            continue

        candidates.append(
            CandidateActivity(
                activity_id=row["activity_id"],
                activity_row=row,
                structured_score=score,
                embedding_score=None,
                combined_score=score,
            )
        )

    candidates.sort(key=lambda c: c.structured_score, reverse=True)
    return candidates[:LAYER1_MAX]


# ---------------------------------------------------------------------------
# Layer 2 — embedding rerank
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _normalize_scores(values: list[float]) -> list[float]:
    """Min-max normalize to [0, 1]. Constant input → all 0.5."""
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi - lo < 1e-9:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _profile_query_text(
    profile: StructuredProfile, profile_nl_description: str
) -> str:
    """Compose a short profile description used as the embedding query."""
    nl = (profile_nl_description or "").strip()
    if profile.topics_weighted:
        # Take top 5 topic ids by weight; using ids is fine — they're
        # readable enough (e.g. `export_controls`, `compute_thresholds`).
        ranked = sorted(
            profile.topics_weighted.items(), key=lambda kv: kv[1], reverse=True
        )
        top_ids = ", ".join(t.replace("_", " ") for t, _ in ranked[:5])
        topic_blurb = f"\n\nKey topics: {top_ids}"
    else:
        topic_blurb = ""
    return f"{nl}{topic_blurb}".strip() or "policy interests"


def _candidate_embedding_text(row: dict) -> str:
    """Compose the text we'd use to back-fill an embedding when the vec0 row
    is missing. Used only in the in-memory fallback path."""
    parts = []
    if row.get("title"):
        parts.append(str(row["title"]))
    if row.get("summary"):
        parts.append(str(row["summary"]))
    if row.get("stance_quote"):
        parts.append(str(row["stance_quote"]))
    return "\n\n".join(parts) or str(row.get("activity_id") or "")


def _fetch_embeddings(
    conn: sqlite3.Connection, activity_ids: list[str]
) -> dict[str, list[float]]:
    """Pull stored embeddings for given activity_ids from the vec0 vtable.

    Returns {activity_id: vec}. Activities with no row in `activity_embedding`
    are absent from the dict.
    """
    if not activity_ids:
        return {}
    out: dict[str, list[float]] = {}
    # vec0 stores embedding as a binary blob — sqlite-vec exposes it via the
    # SELECT pathway as a blob; we cast through `vec_to_json`.
    placeholders = ",".join("?" for _ in activity_ids)
    try:
        rows = conn.execute(
            f"SELECT activity_id, vec_to_json(embedding) AS emb_json "
            f"FROM activity_embedding WHERE activity_id IN ({placeholders})",
            activity_ids,
        ).fetchall()
    except sqlite3.OperationalError as e:
        log.info(
            "[awareness.retrieval] activity_embedding lookup failed (%s) — "
            "treating as no-embeddings",
            e,
        )
        return {}
    for r in rows:
        aid = r["activity_id"] if isinstance(r, sqlite3.Row) else r[0]
        emb_json = r["emb_json"] if isinstance(r, sqlite3.Row) else r[1]
        try:
            vec = json.loads(emb_json) if emb_json else None
        except (TypeError, ValueError):
            vec = None
        if isinstance(vec, list) and vec:
            out[aid] = [float(x) for x in vec]
    return out


def layer2_embedding_rerank(
    conn: sqlite3.Connection,
    profile: StructuredProfile,
    profile_nl_description: str,
    candidates: list[CandidateActivity],
    *,
    weight_structured: float = 0.6,
    weight_embedding: float = 0.4,
) -> list[CandidateActivity]:
    """Rerank Layer-1 candidates using cosine similarity to a profile vector.

    Falls back to identity scoring if embeddings are unavailable.
    """
    if not candidates:
        return []

    # Determine path: do we have any stored embeddings? Do we have a model?
    activity_ids = [c.activity_id for c in candidates]
    stored = _fetch_embeddings(conn, activity_ids)

    model = EmbeddingModel.get()

    if model is None and not stored:
        # Identity path — preserve structured scores and trim.
        log.info(
            "[awareness.retrieval] embeddings unavailable, using identity Layer 2 "
            "(no model + no stored vectors)"
        )
        for c in candidates:
            c.embedding_score = None
            c.combined_score = c.structured_score
        candidates.sort(key=lambda c: c.combined_score, reverse=True)
        return candidates[:LAYER2_MAX]

    # We need a profile query embedding either way.
    if model is not None:
        try:
            query_text = _profile_query_text(profile, profile_nl_description)
            profile_vec = model.encode([query_text])[0]
        except Exception as e:  # noqa: BLE001
            log.warning(
                "[awareness.retrieval] failed to encode profile vector: %s — "
                "falling back to identity Layer 2",
                e,
            )
            profile_vec = None
    else:
        profile_vec = None

    # Decide what to do for candidates missing a stored embedding.
    # If we have a model, we can compute on-the-fly. Otherwise their
    # embedding_score stays None.
    missing = [c for c in candidates if c.activity_id not in stored]
    if model is not None and missing:
        try:
            texts = [_candidate_embedding_text(c.activity_row) for c in missing]
            vecs = model.encode(texts)
            for c, v in zip(missing, vecs):
                stored[c.activity_id] = v
        except Exception as e:  # noqa: BLE001
            log.warning(
                "[awareness.retrieval] failed to backfill candidate embeddings: %s",
                e,
            )

    # Compute cosine for those we have vectors for.
    embedding_scores: list[float] = []
    paired: list[tuple[CandidateActivity, Optional[float]]] = []
    for c in candidates:
        cvec = stored.get(c.activity_id)
        if cvec is None or profile_vec is None:
            paired.append((c, None))
            continue
        sim = _cosine(profile_vec, cvec)
        embedding_scores.append(sim)
        paired.append((c, sim))

    if not embedding_scores:
        log.info(
            "[awareness.retrieval] no usable embeddings for any candidate — "
            "using identity Layer 2"
        )
        for c in candidates:
            c.embedding_score = None
            c.combined_score = c.structured_score
        candidates.sort(key=lambda c: c.combined_score, reverse=True)
        return candidates[:LAYER2_MAX]

    # Normalize the structured scores within the candidate set so the
    # combination weights are interpretable.
    structured_norm_map = {
        c.activity_id: n
        for c, n in zip(
            candidates,
            _normalize_scores([c.structured_score for c in candidates]),
        )
    }

    out: list[CandidateActivity] = []
    for c, sim in paired:
        c.embedding_score = sim
        sn = structured_norm_map.get(c.activity_id, 0.5)
        if sim is None:
            # No embedding — fall back to structured-only contribution scaled
            # to live alongside combined scores from peers.
            c.combined_score = weight_structured * sn
        else:
            c.combined_score = weight_structured * sn + weight_embedding * sim
        out.append(c)

    out.sort(key=lambda c: c.combined_score, reverse=True)
    return out[:LAYER2_MAX]


__all__ = [
    "CandidateActivity",
    "layer1_structured",
    "layer2_embedding_rerank",
    "LAYER1_MAX",
    "LAYER2_MAX",
]
