-- AI Policy Radar — SQLite schema (BUILD_PLAN.md §6)
-- sqlite-vec extension is loaded by connection.py before this runs.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- §6.1 Entity ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity (
    id            TEXT PRIMARY KEY,        -- slug, e.g. "openai", "sen-schumer"
    name          TEXT NOT NULL,
    entity_type   TEXT NOT NULL,           -- company | legislator | legislative_body | judiciary
                                           -- | executive_agency | state_local | civil_society
                                           -- | international | party_faction
    subcategory   TEXT,
    jurisdiction  TEXT,
    description   TEXT,
    aliases       TEXT NOT NULL DEFAULT '[]', -- JSON array of alternate names
    metadata      TEXT NOT NULL DEFAULT '{}', -- JSON, type-specific
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_type        ON entity(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_jurisdiction ON entity(jurisdiction);

-- §6.2 Activity (polymorphic) ----------------------------------------------
CREATE TABLE IF NOT EXISTS activity (
    id              TEXT PRIMARY KEY,      -- hash of source_url + entity_id
    entity_id       TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    entity_type     TEXT NOT NULL,         -- denormalized for fast filtering
    activity_type   TEXT NOT NULL,         -- see §15
    occurred_at     TEXT NOT NULL,         -- ISO date
    ingested_at     TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    source_adapter  TEXT NOT NULL,
    title           TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}', -- JSON, type-specific fields
    url_verified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_entity      ON activity(entity_id);
CREATE INDEX IF NOT EXISTS idx_activity_occurred    ON activity(occurred_at);
CREATE INDEX IF NOT EXISTS idx_activity_type        ON activity(activity_type);
CREATE INDEX IF NOT EXISTS idx_activity_entity_type ON activity(entity_type);

-- §6.3 Enrichment -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment (
    activity_id        TEXT PRIMARY KEY REFERENCES activity(id) ON DELETE CASCADE,
    summary            TEXT NOT NULL,
    topics             TEXT NOT NULL DEFAULT '[]', -- JSON array of topic ids
    mentioned_entities TEXT NOT NULL DEFAULT '[]', -- JSON array of entity ids
    stance             TEXT,                       -- supports | opposes | neutral | mixed | NULL
    stance_quote       TEXT,
    materiality        TEXT NOT NULL DEFAULT '{}', -- JSON: {scope, bindingness, novelty, confidence}
    enriched_at        TEXT NOT NULL,
    enricher_model     TEXT NOT NULL
);

-- Vector embeddings live in a sqlite-vec virtual table; created in connection.py.
-- Schema reference (created at runtime):
--   CREATE VIRTUAL TABLE activity_embedding USING vec0(
--       activity_id TEXT PRIMARY KEY,
--       embedding   FLOAT[<dim>]
--   );

-- §6.4 Topic ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topic (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id TEXT REFERENCES topic(id),
    synonyms  TEXT NOT NULL DEFAULT '[]'   -- JSON array
);

-- §6.5 Membership -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS membership (
    group_id  TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    role      TEXT,
    PRIMARY KEY (group_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_membership_member ON membership(member_id);

-- §6.6 User profile ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profile (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    nl_description TEXT NOT NULL,
    structured     TEXT NOT NULL DEFAULT '{}',    -- JSON
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- §6.7 Awareness items (cached dashboard outputs) --------------------------
CREATE TABLE IF NOT EXISTS awareness_item (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES user_profile(id) ON DELETE CASCADE,
    activity_id         TEXT NOT NULL REFERENCES activity(id) ON DELETE CASCADE,
    generated_at        TEXT NOT NULL,
    relevance_score     REAL NOT NULL,
    reasoning           TEXT NOT NULL,
    recommended_actions TEXT NOT NULL DEFAULT '[]',
    citations           TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_awareness_user ON awareness_item(user_id);
CREATE INDEX IF NOT EXISTS idx_awareness_gen  ON awareness_item(generated_at);
