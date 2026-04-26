"""FastAPI routes for the AI Policy Radar Next.js frontend.

All endpoints are registered on a single `APIRouter` exported as `router`,
which `radar.main` includes at app construction time.

Connection management is per-request via a `Depends(get_db)` generator so
each handler gets a fresh `sqlite3.Connection` (Row factory) and the
connection is closed on response. The `AwarenessEngine` and `ProfileBuilder`
instances are lifespan-scoped — they're stateless apart from prompt-cache
hits that can span requests.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from radar.awareness.engine import AwarenessEngine
from radar.awareness.reasoner import AwarenessItem
from radar.db.connection import EMBEDDING_DIM, get_db_path
from radar.profiles.builder import (
    ProfileBuilder,
    StructuredProfile,
    get_profile,
    insert_profile,
    list_profiles,
)

from radar.api.schemas import (
    ActivityOut,
    ActivityWithEnrichment,
    AwarenessBlock,
    AwarenessItemOut,
    DashboardOut,
    EnrichmentOut,
    EntityOut,
    EntityStats,
    EntitySummary,
    ProfileIn,
    ProfileOut,
    ProfileSummary,
    StructuredProfileOut,
    TopTopicStat,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Connection dependency
# ---------------------------------------------------------------------------


def _connect_for_request() -> sqlite3.Connection:
    """Open a per-request connection.

    We can't reuse `radar.db.connection.connect()` directly because FastAPI may
    dispatch the dependency from a threadpool (sync routes) AND the endpoint
    body from a different thread (async routes), so we need
    `check_same_thread=False`. Mirrors the rest of the connect() setup
    (Row factory + sqlite-vec extension load + foreign keys).
    """
    import sqlite_vec

    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """Per-request SQLite connection. Closes on response."""
    conn = _connect_for_request()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Lifespan-scoped helpers (accessed from app.state)
# ---------------------------------------------------------------------------


def _get_profile_builder(request: Request) -> ProfileBuilder:
    pb = getattr(request.app.state, "profile_builder", None)
    if pb is None:
        raise HTTPException(status_code=503, detail="profile builder unavailable")
    return pb


def _get_awareness_engine(request: Request) -> AwarenessEngine:
    eng = getattr(request.app.state, "awareness_engine", None)
    if eng is None:
        raise HTTPException(status_code=503, detail="awareness engine unavailable")
    return eng


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(s: str | None, default):
    if not s:
        return default
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return default
    return v


def _row_to_entity_summary(row: sqlite3.Row) -> EntitySummary:
    aliases = _safe_json_loads(row["aliases"] if "aliases" in row.keys() else None, [])
    if not isinstance(aliases, list):
        aliases = []
    return EntitySummary(
        id=row["id"],
        name=row["name"],
        entity_type=row["entity_type"],
        subcategory=row["subcategory"] if "subcategory" in row.keys() else None,
        jurisdiction=row["jurisdiction"] if "jurisdiction" in row.keys() else None,
        description=row["description"] if "description" in row.keys() else None,
        aliases=[str(a) for a in aliases],
    )


def _row_to_activity_out(row: sqlite3.Row) -> ActivityOut:
    payload = _safe_json_loads(row["payload"] if "payload" in row.keys() else None, {})
    if not isinstance(payload, dict):
        payload = {}
    return ActivityOut(
        id=row["id"],
        entity_id=row["entity_id"],
        entity_type=row["entity_type"],
        activity_type=row["activity_type"],
        occurred_at=row["occurred_at"] or "",
        ingested_at=row["ingested_at"] or "",
        source_url=row["source_url"] or "",
        source_adapter=row["source_adapter"] or "",
        title=row["title"] or "",
        raw_text=row["raw_text"] if "raw_text" in row.keys() else None,
        payload=payload,
        url_verified_at=(
            row["url_verified_at"] if "url_verified_at" in row.keys() else None
        ),
    )


def _row_to_enrichment_out(row: sqlite3.Row | None) -> EnrichmentOut | None:
    if row is None:
        return None
    # Row may be from a join — only emit if there's an enriched_at.
    enriched_at = row["enriched_at"] if "enriched_at" in row.keys() else None
    if not enriched_at:
        return None
    topics = _safe_json_loads(row["topics"] if "topics" in row.keys() else None, [])
    mentioned = _safe_json_loads(
        row["mentioned_entities"] if "mentioned_entities" in row.keys() else None, []
    )
    materiality = _safe_json_loads(
        row["materiality"] if "materiality" in row.keys() else None, {}
    )
    if not isinstance(topics, list):
        topics = []
    if not isinstance(mentioned, list):
        mentioned = []
    if not isinstance(materiality, dict):
        materiality = {}
    return EnrichmentOut(
        activity_id=row["activity_id"]
        if "activity_id" in row.keys()
        else row["id"],
        summary=row["summary"] or "",
        topics=[str(t) for t in topics],
        mentioned_entities=[str(m) for m in mentioned],
        stance=row["stance"] if "stance" in row.keys() else None,
        stance_quote=row["stance_quote"] if "stance_quote" in row.keys() else None,
        materiality=materiality,
        enriched_at=enriched_at,
        enricher_model=(
            row["enricher_model"] if "enricher_model" in row.keys() else ""
        ),
    )


# ---------------------------------------------------------------------------
# /entities
# ---------------------------------------------------------------------------


_ENTITY_HARD_CAP = 500


@router.get("/entities", response_model=list[EntitySummary])
def list_entities(
    db: DbDep,
    entity_type: list[str] | None = Query(default=None),
    jurisdiction: list[str] | None = Query(default=None),
    q: str | None = Query(default=None),
) -> list[EntitySummary]:
    where: list[str] = []
    params: list = []
    if entity_type:
        where.append(
            "entity_type IN ({})".format(",".join("?" for _ in entity_type))
        )
        params.extend(entity_type)
    if jurisdiction:
        where.append(
            "jurisdiction IN ({})".format(",".join("?" for _ in jurisdiction))
        )
        params.extend(jurisdiction)
    if q:
        where.append("(LOWER(name) LIKE ? OR LOWER(aliases) LIKE ?)")
        like = f"%{q.lower()}%"
        params.extend([like, like])

    sql = "SELECT id, name, entity_type, subcategory, jurisdiction, description, aliases FROM entity"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name LIMIT ?"
    params.append(_ENTITY_HARD_CAP)

    rows = db.execute(sql, params).fetchall()
    return [_row_to_entity_summary(r) for r in rows]


@router.get("/entities/{entity_id}", response_model=EntityOut)
def get_entity(entity_id: str, db: DbDep) -> EntityOut:
    row = db.execute(
        "SELECT id, name, entity_type, subcategory, jurisdiction, description, aliases "
        "FROM entity WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"entity {entity_id!r} not found")

    summary = _row_to_entity_summary(row)

    # activity_count
    count_row = db.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    activity_count = int(count_row["n"]) if count_row else 0

    # recent activities (last 10) joined with enrichment
    recent_rows = db.execute(
        """
        SELECT a.id, a.entity_id, a.entity_type, a.activity_type, a.occurred_at,
               a.ingested_at, a.source_url, a.source_adapter, a.title, a.raw_text,
               a.payload, a.url_verified_at,
               en.activity_id AS en_activity_id,
               en.summary, en.topics, en.mentioned_entities,
               en.stance, en.stance_quote, en.materiality,
               en.enriched_at, en.enricher_model
        FROM activity a
        LEFT JOIN enrichment en ON en.activity_id = a.id
        WHERE a.entity_id = ?
        ORDER BY a.occurred_at DESC, a.ingested_at DESC
        LIMIT 10
        """,
        (entity_id,),
    ).fetchall()

    recent: list[ActivityWithEnrichment] = []
    for r in recent_rows:
        # Build a stitched row where 'activity_id' resolves correctly for
        # the enrichment helper.
        en_row = None
        if r["en_activity_id"]:
            en_row = {
                "activity_id": r["en_activity_id"],
                "summary": r["summary"],
                "topics": r["topics"],
                "mentioned_entities": r["mentioned_entities"],
                "stance": r["stance"],
                "stance_quote": r["stance_quote"],
                "materiality": r["materiality"],
                "enriched_at": r["enriched_at"],
                "enricher_model": r["enricher_model"],
            }
        recent.append(
            ActivityWithEnrichment(
                activity=_row_to_activity_out(r),
                enrichment=_dict_to_enrichment_out(en_row),
                source_entity=summary,
            )
        )

    # top topics from enrichments
    topic_rows = db.execute(
        """
        SELECT en.topics, en.stance
        FROM enrichment en
        JOIN activity a ON a.id = en.activity_id
        WHERE a.entity_id = ?
        """,
        (entity_id,),
    ).fetchall()

    topic_counts: Counter[str] = Counter()
    topic_stances: dict[str, Counter[str]] = {}
    for tr in topic_rows:
        topics = _safe_json_loads(tr["topics"], [])
        if not isinstance(topics, list):
            continue
        stance = tr["stance"]
        for t in topics:
            if not isinstance(t, str):
                continue
            topic_counts[t] += 1
            if stance:
                topic_stances.setdefault(t, Counter())[str(stance)] += 1

    top_topics: list[TopTopicStat] = []
    for tid, cnt in topic_counts.most_common(3):
        dom = None
        if tid in topic_stances and topic_stances[tid]:
            dom = topic_stances[tid].most_common(1)[0][0]
        top_topics.append(TopTopicStat(topic_id=tid, count=cnt, dominant_stance=dom))

    return EntityOut(
        entity=summary,
        stats=EntityStats(
            activity_count=activity_count,
            recent_activities=recent,
            top_topics=top_topics,
        ),
    )


def _dict_to_enrichment_out(d: dict | None) -> EnrichmentOut | None:
    if not d or not d.get("enriched_at"):
        return None
    topics = _safe_json_loads(d.get("topics"), [])
    mentioned = _safe_json_loads(d.get("mentioned_entities"), [])
    materiality = _safe_json_loads(d.get("materiality"), {})
    if not isinstance(topics, list):
        topics = []
    if not isinstance(mentioned, list):
        mentioned = []
    if not isinstance(materiality, dict):
        materiality = {}
    return EnrichmentOut(
        activity_id=d.get("activity_id") or "",
        summary=d.get("summary") or "",
        topics=[str(t) for t in topics],
        mentioned_entities=[str(m) for m in mentioned],
        stance=d.get("stance"),
        stance_quote=d.get("stance_quote"),
        materiality=materiality,
        enriched_at=d.get("enriched_at") or "",
        enricher_model=d.get("enricher_model") or "",
    )


# ---------------------------------------------------------------------------
# /activities
# ---------------------------------------------------------------------------


@router.get("/activities", response_model=list[ActivityWithEnrichment])
def list_activities(
    db: DbDep,
    entity_id: list[str] | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ActivityWithEnrichment]:
    where: list[str] = []
    params: list = []
    if entity_id:
        where.append("a.entity_id IN ({})".format(",".join("?" for _ in entity_id)))
        params.extend(entity_id)
    if entity_type:
        where.append("a.entity_type = ?")
        params.append(entity_type)
    if activity_type:
        where.append("a.activity_type = ?")
        params.append(activity_type)
    if since:
        where.append("a.occurred_at >= ?")
        params.append(since)
    if topic:
        # Substring match against the JSON array text — cheap, good enough for MVP.
        where.append("en.topics LIKE ?")
        params.append(f'%"{topic}"%')

    sql = """
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
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.occurred_at DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    out: list[ActivityWithEnrichment] = []
    for r in rows:
        en_row = None
        if r["en_activity_id"]:
            en_row = {
                "activity_id": r["en_activity_id"],
                "summary": r["summary"],
                "topics": r["topics"],
                "mentioned_entities": r["mentioned_entities"],
                "stance": r["stance"],
                "stance_quote": r["stance_quote"],
                "materiality": r["materiality"],
                "enriched_at": r["enriched_at"],
                "enricher_model": r["enricher_model"],
            }
        ent_aliases = _safe_json_loads(r["e_aliases"], [])
        if not isinstance(ent_aliases, list):
            ent_aliases = []
        out.append(
            ActivityWithEnrichment(
                activity=_row_to_activity_out(r),
                enrichment=_dict_to_enrichment_out(en_row),
                source_entity=EntitySummary(
                    id=r["e_id"],
                    name=r["e_name"],
                    entity_type=r["e_entity_type"],
                    subcategory=r["e_subcategory"],
                    jurisdiction=r["e_jurisdiction"],
                    description=r["e_description"],
                    aliases=[str(a) for a in ent_aliases],
                ),
            )
        )
    return out


@router.get("/activities/{activity_id}", response_model=ActivityWithEnrichment)
def get_activity(activity_id: str, db: DbDep) -> ActivityWithEnrichment:
    row = db.execute(
        """
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
        WHERE a.id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"activity {activity_id!r} not found"
        )

    en_row = None
    if row["en_activity_id"]:
        en_row = {
            "activity_id": row["en_activity_id"],
            "summary": row["summary"],
            "topics": row["topics"],
            "mentioned_entities": row["mentioned_entities"],
            "stance": row["stance"],
            "stance_quote": row["stance_quote"],
            "materiality": row["materiality"],
            "enriched_at": row["enriched_at"],
            "enricher_model": row["enricher_model"],
        }
    ent_aliases = _safe_json_loads(row["e_aliases"], [])
    if not isinstance(ent_aliases, list):
        ent_aliases = []

    return ActivityWithEnrichment(
        activity=_row_to_activity_out(row),
        enrichment=_dict_to_enrichment_out(en_row),
        source_entity=EntitySummary(
            id=row["e_id"],
            name=row["e_name"],
            entity_type=row["e_entity_type"],
            subcategory=row["e_subcategory"],
            jurisdiction=row["e_jurisdiction"],
            description=row["e_description"],
            aliases=[str(a) for a in ent_aliases],
        ),
    )


# ---------------------------------------------------------------------------
# /profiles
# ---------------------------------------------------------------------------


def _structured_to_out(profile: StructuredProfile) -> StructuredProfileOut:
    return StructuredProfileOut(**asdict(profile))


@router.post("/profiles", response_model=ProfileOut, status_code=201)
async def create_profile(
    body: ProfileIn, db: DbDep, request: Request
) -> ProfileOut:
    builder = _get_profile_builder(request)
    try:
        structured = await builder.build(
            body.nl_description,
            structured_overrides=body.structured_overrides,
        )
    except ValueError as exc:
        # Surface the validation problem as 422.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("profile builder failed")
        raise HTTPException(
            status_code=500, detail="profile builder failed"
        ) from exc

    profile_id = insert_profile(
        db,
        name=body.name,
        nl_description=body.nl_description,
        structured=structured,
    )
    return ProfileOut(
        profile_id=profile_id,
        name=body.name,
        nl_description=body.nl_description,
        structured=_structured_to_out(structured),
    )


@router.get("/profiles", response_model=list[ProfileSummary])
def list_profiles_route(db: DbDep) -> list[ProfileSummary]:
    rows = db.execute(
        "SELECT id, name, created_at FROM user_profile ORDER BY updated_at DESC"
    ).fetchall()
    return [
        ProfileSummary(id=r["id"], name=r["name"], created_at=r["created_at"])
        for r in rows
    ]


@router.get("/profiles/{profile_id}", response_model=ProfileOut)
def get_profile_route(profile_id: str, db: DbDep) -> ProfileOut:
    loaded = get_profile(db, profile_id)
    if loaded is None:
        raise HTTPException(
            status_code=404, detail=f"profile {profile_id!r} not found"
        )
    name, nl_description, structured = loaded
    return ProfileOut(
        profile_id=profile_id,
        name=name,
        nl_description=nl_description,
        structured=_structured_to_out(structured),
    )


# ---------------------------------------------------------------------------
# /dashboard + /awareness/refresh
# ---------------------------------------------------------------------------


def _parse_since(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid 'since' value: {s!r}"
            ) from exc


def _hydrate_awareness_items(
    db: sqlite3.Connection,
    items: list[AwarenessItem],
) -> list[AwarenessItemOut]:
    """Join each AwarenessItem with its activity + enrichment + entity."""
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
    by_id: dict[str, sqlite3.Row] = {r["id"]: r for r in rows}

    out: list[AwarenessItemOut] = []
    for it in items:
        r = by_id.get(it.activity_id)
        activity_out: ActivityOut | None
        enrichment_out: EnrichmentOut | None = None
        source_entity: EntitySummary | None = None
        if r is None:
            # Activity row missing — emit a stub so the client still sees the awareness block.
            activity_out = ActivityOut(
                id=it.activity_id,
                entity_id="",
                entity_type="",
                activity_type="",
                occurred_at="",
                ingested_at="",
                source_url="",
                source_adapter="",
                title="(activity not found)",
                raw_text=None,
                payload={},
                url_verified_at=None,
            )
        else:
            activity_out = _row_to_activity_out(r)
            if r["en_activity_id"]:
                enrichment_out = _dict_to_enrichment_out(
                    {
                        "activity_id": r["en_activity_id"],
                        "summary": r["summary"],
                        "topics": r["topics"],
                        "mentioned_entities": r["mentioned_entities"],
                        "stance": r["stance"],
                        "stance_quote": r["stance_quote"],
                        "materiality": r["materiality"],
                        "enriched_at": r["enriched_at"],
                        "enricher_model": r["enricher_model"],
                    }
                )
            ent_aliases = _safe_json_loads(r["e_aliases"], [])
            if not isinstance(ent_aliases, list):
                ent_aliases = []
            source_entity = EntitySummary(
                id=r["e_id"],
                name=r["e_name"],
                entity_type=r["e_entity_type"],
                subcategory=r["e_subcategory"],
                jurisdiction=r["e_jurisdiction"],
                description=r["e_description"],
                aliases=[str(a) for a in ent_aliases],
            )
        out.append(
            AwarenessItemOut(
                activity=activity_out,
                enrichment=enrichment_out,
                source_entity=source_entity,
                awareness=AwarenessBlock(
                    relevance_score=float(it.relevance_score),
                    reasoning=it.reasoning,
                    recommended_actions=list(it.recommended_actions),
                    citations=list(it.citations),
                ),
            )
        )
    return out


async def _build_dashboard_response(
    db: sqlite3.Connection,
    engine: AwarenessEngine,
    profile_id: str,
    since: str | None,
    top_k: int,
) -> DashboardOut:
    since_date = _parse_since(since)
    try:
        items = await engine.build_dashboard(
            db, profile_id, since=since_date, top_k=top_k
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("dashboard build failed for profile %s", profile_id)
        raise HTTPException(
            status_code=500, detail="awareness engine failed"
        ) from exc

    hydrated = _hydrate_awareness_items(db, items)
    return DashboardOut(
        profile_id=profile_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        items=hydrated,
    )


@router.get("/dashboard/{profile_id}", response_model=DashboardOut)
async def get_dashboard(
    profile_id: str,
    db: DbDep,
    request: Request,
    since: str | None = Query(default=None),
    top_k: int = Query(default=15, ge=1, le=30),
) -> DashboardOut:
    engine = _get_awareness_engine(request)
    return await _build_dashboard_response(db, engine, profile_id, since, top_k)


@router.post("/awareness/refresh/{profile_id}", response_model=DashboardOut)
async def refresh_awareness(
    profile_id: str,
    db: DbDep,
    request: Request,
    since: str | None = Query(default=None),
    top_k: int = Query(default=15, ge=1, le=30),
) -> DashboardOut:
    engine = _get_awareness_engine(request)
    # Drop any prior cached items for this profile so the fresh run is the
    # only set in `awareness_item`.
    try:
        db.execute("DELETE FROM awareness_item WHERE user_id = ?", (profile_id,))
        db.commit()
    except sqlite3.Error as exc:  # noqa: BLE001
        log.warning("failed to clear awareness_item for %s: %s", profile_id, exc)
    return await _build_dashboard_response(db, engine, profile_id, since, top_k)


__all__ = ["router", "get_db"]
