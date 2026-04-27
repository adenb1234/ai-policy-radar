# AI Policy Radar

> **🔗 Live demo:** [**ai-policy-radar.vercel.app**](https://ai-policy-radar.vercel.app/) — read-only static deploy with four pre-built analyst personas. Try the [State AG dashboard](https://ai-policy-radar.vercel.app/dashboard/94cbcfb90d10) first (richest at 14 awareness items); also see [Frontier Lab](https://ai-policy-radar.vercel.app/dashboard/2c4771bfe57e), [Healthcare AI](https://ai-policy-radar.vercel.app/dashboard/3d27db24e009), and [Datacenter Investor](https://ai-policy-radar.vercel.app/dashboard/b80d5d3ddb26).
>
> The deploy serves pre-computed snapshots (`NEXT_PUBLIC_DEMO_MODE=1`). The full live system — profile creation, awareness refresh, ingestion — runs locally per the [Live demo flow](#2-live-demo-flow) below.

A working inventory of the entities active in AI policy and regulation — companies, agencies, legislators, courts, civil-society orgs, foreign bodies — together with a per-user "awareness dashboard" that surfaces the developments most relevant to a stated set of equities, with grounded reasoning, source citations, and recommended actions. Built for a working policy/geopolitics analyst (Aden Barton); designed to feel like the analyst tool he would have wanted at Bridgewater or CEA, not a consumer chat surface.

This README is the document a Perplexity reviewer reads first. The full spec, the architecture rationale, the decision log, and the phase plan all live in [`../BUILD_PLAN.md`](../BUILD_PLAN.md) — that file is the durable source of truth and is referenced throughout below.

---

## 1. The customization story

The spec's load-bearing requirement is **customization** — not breadth. There are **no pre-baked personas** in this system. A user opens `/profile/new`, types a paragraph describing their organization and what they care about, and clicks Create. A single Claude Opus 4.7 call (`backend/radar/profiles/builder.py`) reads that natural-language description and extracts a structured profile:

- `topics_weighted` — which entries from the controlled topic vocabulary matter, and how much
- `watch_entities` — which specific entities (drawn from the entity table) the user wants tracked
- `jurisdictions` — federal, state, EU, etc.
- `activity_type_filters` — bills, rules, opinions, comment letters, etc.
- `action_thresholds` — informational vs. actionable

Both halves are persisted on `user_profile`: the raw NL string (used downstream for nuance the structured form loses) and the JSON-structured projection (used for cheap layer-1 retrieval). The dashboard then runs the awareness engine against that profile. No two profiles produce the same dashboard.

The demo loop (§11 of `BUILD_PLAN.md`): in the recap, three to four distinct personas — a frontier-lab policy lead, a state AG, a healthcare-AI startup, a datacenter-buildout investor — get spun up live in the UI. The fact that they're built on the spot, from a paragraph each, *is* the product story.

---

## 2. Live demo flow

```bash
# 0. Prereqs: Python 3.11+, Node 20+, uv, pnpm. ANTHROPIC_API_KEY in .env.

# 1. Install Python (uv) + Node (pnpm) deps
make install

# 2. Seed the entity universe (52 entities) + topic vocab (44 topics) into SQLite
make seed

# 3. Run source adapters — Federal Register (keyless) + research adapter (Opus + web search)
make ingest

# 4. Enrich every Activity via Claude Sonnet 4.6 (topics, stance, summary, materiality, embeddings)
make enrich

# 5. Run dev — backend on :8000, frontend on :3000 (use two shells)
make dev-backend
make dev-frontend
```

Then open <http://localhost:3000>:

1. Click **Create a profile**.
2. Paste a natural-language description, e.g.:
   > "I run policy at a US frontier AI lab. I care most about export controls, compute thresholds, and chip controls; the EU AI Act implementing acts; California AI legislation; NIST/AISI safety evals; and federal procurement guidance. Surface anything that escalates from voluntary to binding, and anything that touches our model-release posture."
3. Click **Create profile**. Backend extracts the structured form via Opus, persists the profile, redirects to the dashboard.
4. The dashboard renders 10–15 ranked awareness items. Each card shows a relevance score (0–10), the source entity, the occurred-at date, the activity type, the title, a 2–3-sentence Opus-generated reasoning paragraph grounded in specific enrichment fields, the activity's stance + a quoted substring from the source, the matched topics as chips, up to three recommended actions, and "drawn from:" citation chips naming the enrichment field paths.
5. Click an entity chip → entity page with recent activities, top-3 topics, dominant stance, and full alias list.
6. Click an activity title → activity detail page with summary, full enrichment view (stance + quote, topics, materiality cards, mentioned entities), and the raw `payload` JSON for ingestion verification.

Every URL in every card is real — see §6 on URL verification.

---

## 3. Architecture

The §5 diagram from `BUILD_PLAN.md`, faithfully:

```
┌────────────────────────────────────────────────────────────────────┐
│  SOURCE ADAPTERS (Python)                                          │
│  ┌──────────────────┐    ┌───────────────────────────────────────┐ │
│  │ Structured       │    │ Research                              │ │
│  │ - congress.gov   │    │ - Claude Opus 4.7 + web search        │ │
│  │ - FederalReg     │    │ - per-entity discovery + ingestion    │ │
│  │ - CourtListener  │    │ - URL verification step (mandatory)   │ │
│  │ - EDGAR          │    └───────────────────────────────────────┘ │
│  │ - EUR-Lex        │                                              │
│  │ - Regulations.gov│    All adapters → normalized Activity record │
│  └──────────────────┘                                              │
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  ENRICHMENT PIPELINE (Python + Claude Sonnet 4.6)                  │
│  per Activity:                                                     │
│   - topic tags (controlled vocab)                                  │
│   - mentioned entities (resolved against entity table)             │
│   - stance (supports/opposes/neutral/mixed + extracted quote)      │
│   - one-paragraph summary                                          │
│   - materiality features (scope, bindingness, novelty)             │
│   - embedding (for hybrid retrieval)                               │
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  STORAGE: SQLite + sqlite-vec                                      │
│  tables: entity, activity, enrichment, topic, membership,          │
│          user_profile, awareness_item                              │
└────────────────────────────┬───────────────────────────────────────┘
                             │
        ┌────────────────────┴────────────────────┐
        ▼                                         ▼
┌──────────────────────┐              ┌──────────────────────────────┐
│ AWARENESS ENGINE     │              │ FastAPI BACKEND              │
│ (Python + Opus 4.7)  │◄─────────────│ /entities /activities        │
│ - 3-layer retrieval  │              │ /profiles /dashboard /eval   │
│ - LLM rerank+reason  │              └──────────────┬───────────────┘
│ - actions generation │                             │
└──────────────────────┘                             ▼
                                       ┌──────────────────────────────┐
                                       │ NEXT.JS FRONTEND (TS)        │
                                       │ - Dashboard (per profile)    │
                                       │ - Profile builder (NL + form)│
                                       │ - Entity directory + pages   │
                                       │ - Activity feed + filters    │
                                       │ - (stretch) Coalition view   │
                                       └──────────────────────────────┘
```

**Data flow.** Two adapter families produce a uniform `Activity` record: structured adapters (Federal Register today; congress.gov / CourtListener / EDGAR / Regulations.gov / EUR-Lex specified) hit known APIs; the research adapter wraps Claude Opus 4.7 + web search and is parameterized per-entity for the long tail of civil-society, international, and state/local entities that don't have clean APIs. Every Activity lands in SQLite via `db/storage.py:upsert_activity`, keyed by a deterministic hash of `(entity_id, source_url)` so re-ingestion is idempotent.

**Enrichment.** Each new Activity is processed by `enrich/pipeline.py` — a Claude Sonnet 4.6 call with prompt caching on a ~15K-token system block (topic vocab + entity directory + activity-type guidance + few-shot examples). The model emits a structured tool call (`emit_enrichment`) whose schema fields map 1:1 to the `enrichment` table columns. Post-hoc validation drops any topic_id or entity_id the model invented and substring-checks `stance_quote` against `raw_text` so quoted-but-not-present is rejected. After enrichment the model also produces a 384-d MiniLM embedding (when the local model is available — see §10) into the `activity_embedding` vec0 virtual table.

**Awareness engine** (`awareness/engine.py`) is the centerpiece. Three layers in sequence: a cheap structured filter against the profile's topic/entity/jurisdiction set (caps at ~200 candidates); an embedding rerank that scores those candidates by cosine distance from a profile embedding (NL description + top-5 topic synonyms), merged 60/40 with structured score; then a single Opus 4.7 call that processes the top ~30 in batches of 5, returning `relevance_score`, `reasoning`, `recommended_actions`, and `citations` — all four fields together, in one call, via a forced tool. Prompt caching is applied to the system prompt (rubric + sorted-key profile JSON for byte-stable cache prefix) so calls 2..N hit the cache; the per-batch user turn carries a compact projection of activity + enrichment (no `raw_text`, which keeps tokens cheap).

---

## 4. Data model

SQLite at `data/radar.db`. Schemas in `backend/radar/db/schema.sql`. Full definitions in `BUILD_PLAN.md` §6.

| Table | Purpose |
|---|---|
| `entity` | The 52-entity universe — companies, agencies, legislators, courts, civil-society orgs, factions, international bodies. |
| `activity` | Polymorphic activity row: hash id + uniform fields (occurred_at, source_url, title, raw_text) + `payload` JSON whose shape is dictated by `activity_type` (per-type pydantic schemas in `db/payload_schemas.py`). |
| `enrichment` | One row per activity: summary, topics (JSON array of topic ids), mentioned_entities, stance + stance_quote, materiality JSON, enricher_model + timestamp. |
| `activity_embedding` | sqlite-vec `vec0` virtual table holding 384-d MiniLM embeddings keyed by activity_id. |
| `topic` | Controlled vocabulary — 44 topics with synonyms and optional parent_id hierarchy. |
| `membership` | Faction ↔ legislator / executive joins. Faction-level activity is *computed* (union of members), not stored. |
| `user_profile` | Profile row: raw NL description + structured JSON projection. |
| `awareness_item` | Cached dashboard outputs: relevance_score, reasoning, recommended_actions, citations, generated_at. |

---

## 5. Source adapters

Two interfaces, one output shape (a normalized `Activity` dict ready for `upsert_activity`).

**Federal Register adapter** (`adapters/structured/federal_register.py`). Keyless public API at `federalregister.gov/api/v1`. Per-entity, per-AI-keyword fan-out (the boolean-OR query collapsed recall in testing), then per-document fetch. Maps FR `type` → activity_type (RFI, NPRM, FINAL_RULE, OFFICIAL_STATEMENT). FR is thin for entities like NIST/AISI that publish off-FR (see §11 limitations).

**Research adapter** (`adapters/research/per_entity.py`). One adapter, parameterized per entity. System prompt instructs Claude Opus 4.7 to use the web search tool to discover recent activities for the named entity, fit each into one of the activity_type schemas (the prompt inlines the relevant payload spec), and emit a structured list. **URL verification** (`BUILD_PLAN.md` §7.4) is the hallucination guard: every emitted Activity is post-processed by re-fetching `source_url`, confirming HTTP 200, and substring-checking the model-supplied `_verify_phrases` against the response body. Failures are dropped and logged. No exceptions. This is the only thing standing between the demo and a fabricated source URL on stage.

---

## 6. Enrichment

For each Activity the Sonnet 4.6 call extracts: `summary` (one factual paragraph), `topics` (≤5 ids from the controlled vocab), `mentioned_entities` (entity ids resolved by name + alias matching), `stance` ∈ {supports, opposes, neutral, mixed, null} with a `stance_quote` taken verbatim from the source, and `materiality` ({scope, bindingness, novelty, confidence}). Per-activity-type prompt overrides live in `backend/radar/enrich/prompts/{company,legislator,judiciary,executive_agency,civil_society}.md` — e.g. judiciary opinions default `stance=null` because courts don't take positions; civil-society activities almost always carry stance because that's what advocacy is.

**Citation discipline.** The base prompt (`prompts/_base.md`) inlines the full topic vocabulary and a per-run entity directory. Two post-hoc filters run on every response: (a) topic ids not in the vocabulary are silently dropped; (b) `stance_quote` must appear as a literal substring of `raw_text` or it's nulled. Both filters mean the enrichment row never references a topic the system doesn't know about, and never quotes something the source doesn't say. A subagent's smoke test on a synthetic EFF biometric-surveillance open letter confirmed both filters fire on adversarial inputs.

**Prompt caching.** The ~15K-token system block (topic vocab + entity directory + activity-type guidance + few-shot examples + schema) is marked `cache_control: ephemeral`. First request pays full token cost; subsequent requests in the same enrichment run pay cache-read rates. Smoke test: 15,073 cached input tokens read on call 2 vs. 1,023 fresh user-turn tokens.

---

## 7. Awareness engine

Three layers, in `backend/radar/awareness/`.

**Layer 1 — structured filter** (`retrieval.py:layer1_structured`). Joins `activity + entity + enrichment`. Filters by `since`, `entity_type`, `activity_type`, `jurisdiction` (with a global-or-sector escape that lets sectoral entities through even if their jurisdiction doesn't match exactly). Requires at least one of: a topic match against `topics_weighted`, a mentioned-entity match against the watchlist, or a direct watch-entity hit on the activity's `entity_id`. Scores each candidate via `topics_weighted` × topic match + watch-entity boost + 7-day recency boost. Caps at 200.

**Layer 2 — embedding rerank** (`retrieval.py:layer2_embedding_rerank`). Embeds `(profile.nl_description + top-5 topic synonyms)` via local MiniLM, computes cosine similarity to each candidate's stored embedding, normalizes to 0–1, merges 60/40 with the structured score, and returns the top 30. Graceful identity fallback when sentence-transformers isn't importable or `vec0` rows don't exist (see §11).

**Layer 3 — Opus rerank + reasoning + actions + citations** (`reasoner.py`). One Opus 4.7 call per batch of 5, with the rubric and sorted-key profile JSON as the cached system prompt and a compact activity projection as the user turn. The model is forced to call `emit_awareness_items`, returning per item: `relevance_score` (0–10), `reasoning` (2–3 sentences referencing specific enrichment field paths), `recommended_actions` (≤3, concrete), and `citations` (field paths into the source enrichment). All four are produced in a single call — no second round-trip — which is why the dashboard renders fast even for 15 items. Calls 2..N within a single dashboard build hit the cache prefix.

**Live-agent fallback** (§9.2 of `BUILD_PLAN.md`). When layer-1 returns < threshold candidates for an interest the user explicitly named, the engine logs a `[fallback]` line and continues with what it has. Activating the fallback (dispatching a research subagent at query time) is wired but disabled — see §11.

---

## 8. Frontend

Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui. Server components by default; client components only where interaction demands them (profile form, dashboard chip filters, entity search box). Dense analyst-tool feel — information per pixel matters more than whitespace.

| Route | Purpose |
|---|---|
| `/` | Landing — list profiles + create-profile CTA. |
| `/profile/new` | NL textarea + name input → `POST /profiles` → redirect to dashboard. |
| `/dashboard/[profileId]` | The centerpiece. Header strip with profile summary; 260px sticky sidebar with date presets + chip facets derived client-side from loaded items; ranked AwarenessCard list. |
| `/entities` | Directory grouped by `entity_type`, with a client-side search by name + aliases. |
| `/entities/[id]` | Tabs: recent activities, stats (top topics + dominant stance + activity volume). |
| `/activity/[id]` | Two-column: summary + stance + quote + topics + materiality cards + mentioned-entity links + raw `payload` JSON rail. |

The typed API client (`frontend/lib/api.ts`) wraps every backend route. Every fetch is `cache: 'no-store'`. Non-2xx responses raise `ApiError(status, body)`. Loading skeletons on every fetching route. Empty states are explicit (e.g. "no activities matched your profile in this window — try widening the date range or adding more topics"). Backend-down errors render a destructive card, not a stack trace.

---

## 9. Eval harness

`evals/cases.yaml` holds ~10 hand-curated tuples. Each is `(profile_nl, profile_structured, expected_top_activities, expected_topics_surfaced, expected_action_themes)`. **The expected fields must be filled in by Aden, not by an LLM.** A model that writes both the cases and the answers tests nothing — this is called out explicitly in `BUILD_PLAN.md` §12.1. The MVP cases file is currently a draft awaiting Aden's pass.

`evals/run.py` (planned) loads cases, freezes a snapshot of the activity table, calls the awareness engine for each profile, captures the output. `evals/judge.py` (planned) is an Opus-as-judge: given `(profile, expected, actual)`, scores `relevance_recall`, `reasoning_quality`, `extras_quality` 0–10 each with a one-line rationale per axis.

`make eval` runs the suite and writes `evals/report.md` — per-case scores, aggregate, delta vs. previous run. The post-MVP iteration loop (`BUILD_PLAN.md` §12.5) picks the lowest-scoring case, diagnoses (missing entities? bad topic vocab? weak prompts? retrieval issue?), fixes, re-runs.

---

## 10. Decisions & tradeoffs

Adapted from the decision log in `BUILD_PLAN.md` §21. Each row was locked through human ↔ planner ideation; future amendments require human approval.

| # | Decision | Rationale | Alternatives |
|---|---|---|---|
| 1 | **Single LLM provider (Anthropic).** | One key, one billing surface, one SDK. Simplifies scope. | Mixed Claude + Perplexity Sonar (better web search signal but more keys + accounts). |
| 2 | **Sonnet 4.6 for enrichment, Opus 4.7 for research + awareness.** | Cost/quality split: cheap structured extraction at scale; expensive judgment work where it matters. | All-Opus (cost); all-Sonnet (quality risk on multi-hop reasoning). |
| 3 | **Embedded SQLite + sqlite-vec.** | Right-sized for a 9h scope. Zero ops. One file, gitignored. | Postgres + pgvector (real ops cost); DuckDB (less mature vector). |
| 4 | **Three-layer retrieval (structured + embedding + Opus rerank).** | Embeddings alone don't catch hard structured signal (entity match, jurisdiction, recency); structured alone is brittle to vocab gaps; Opus rerank is the only layer that can produce grounded reasoning. | Pure RAG (misses structured signal); pure structured (brittle). |
| 5 | **Opus rerank produces reasoning + actions + citations in one call.** | One round-trip per batch of 5; system prompt cache hits across batches. | Separate calls per field (3× the round-trips, no cache benefit). |
| 6 | **Mandatory URL verification on research-adapter outputs.** | Hallucinated source URLs are project-killing at demo time. Re-fetch + substring check is cheap insurance. | Trust the LLM's URLs (one fabricated URL on stage and the demo is over). |
| 7 | **No pre-built personas — profiles are built live from NL.** | Customization *is* the spec's centerpiece. | Pre-bake 2–3 personas (less work, undermines the demo). |
| 8 | **Polymorphic activity table (typed `payload` JSON).** | Activity types differ structurally; downstream enrichment is uniform. | Per-type tables (boilerplate); fully unstructured (loses signal). |
| 9 | **Faction = computed from membership rows, not first-class activities.** | Factions don't have native activities. Aggregating member statements is honest; synthesizing faction activities would be slop. | First-class faction activity (synthetic). |
| 10 | **Eval harness post-MVP, drives iteration loop.** | Concrete signal for the post-MVP planner. Senior-engineer recap moment. | No evals (nothing to optimize against). |

---

## 11. What's not done / known limitations

This section is honest about what's still rough.

- **Embeddings degrade to identity scoring.** `sentence-transformers` pulls torch wheels that have no Mac-x86_64 build path on Python 3.13. Layer 2 has a graceful fallback: when the model isn't importable or `vec0` rows are missing, embedding scores collapse to a constant and Layer 1's structured score carries the rerank. The Opus layer compensates because it sees the full profile + activity. Future work: torch-free embeddings via `fastembed` / ONNX (§13).
- **Federal Register coverage is thin for NIST / AISI.** Both publish much of their guidance off-FR (NIST AI 600-1, AISI evals reports). The research adapter compensates, but for "first 90 days of NIST output" the FR slice underrepresents reality.
- **Anthropic dev-tier rate limits make ingestion + enrichment slow.** Sonnet 4.6 is capped at ~30K input tokens / minute on the dev tier; with prompt caching active, a full enrichment pass over a few hundred activities runs in real wall-clock time, not seconds. Concurrency is bounded by an asyncio semaphore so we don't 429.
- **Live-agent fallback (§9.2) logs but doesn't yet dispatch.** The retrieval layer emits a `[fallback]` line when candidates fall below threshold, but the actual on-the-fly research-subagent call is wired and gated off. Re-opens after Phase 6 once we've seen real coverage gaps in demo personas.
- **Eval ground truth requires Aden.** The fixtures file is drafted; the `expected_*` fields must be filled in by hand, not by LLM. Today the eval suite runs but reports against incomplete ground truth.
- **Structured adapters beyond Federal Register need API keys.** congress.gov + Regulations.gov go through `api.data.gov` (one key); CourtListener has its own. The keys are an open question for Aden (`BUILD_STATE.md` Q1). EDGAR is keyless and queued.
- **Coverage is thin for entities without web presence.** Party factions are computed from members, which works only if the members themselves are well-covered. A few civil-society entities yielded < 5 activities in research-adapter ingestion runs.
- **No auth, no multi-user, no deployment.** Single-user local app; reviewer runs `make dev`. Out of scope per `BUILD_PLAN.md` §18.

---

## 12. Future work

In rough ROI order, drawn from `BUILD_PLAN.md` §17.

1. **Coalition / alignment detection** (highest stretch ROI). For each topic, cluster entities by stance over the last N days. Render a "who's converging with whom" view. Detects stance shifts (entity moved from oppose → mixed → support on topic X). Reuses enrichment data, no new infra. New `/coalitions` page.
2. **Torch-free embeddings.** `fastembed` or ONNX runtime instead of `sentence-transformers`. Eliminates the Mac-x86_64 wheel issue. Layer 2 becomes real on every install.
3. **EDGAR + congress.gov + CourtListener structured adapters.** Wired and ready; gated on API keys. Once the keys land these are 1–2-hour additions each.
4. **Ask-the-inventory chat surface.** Single search box → Claude with tool-calling against the `entity` / `activity` / `enrichment` tables → grounded answer with citations. Read-only, Perplexity-flavored.
5. **Weekly auto-brief generator.** Per profile, generate a 1-page Markdown summary of the week's highest-relevance activities, themes, and recommended actions.
6. **Stance-shift alerts.** Background job that surfaces meaningful stance flips on tracked topics into dashboards filtering on those topics.

---

## 13. Run commands

| Command | What it does |
|---|---|
| `make install` | Installs backend (`uv sync --extra dev`) + frontend (`pnpm install`) deps. |
| `make seed` | Seeds the 52 entities + 44 topics + faction memberships from `backend/data/{entity_seed,topics}.yaml` into SQLite. Idempotent. |
| `make ingest` | Runs source adapters. `ARGS=` passthrough supports `--since`, `--entity-type`, `--entity-id`, `--dry-run`, `--limit`, `--no-research`. |
| `make enrich` | Runs the Sonnet 4.6 enrichment pipeline over un-enriched activities. `ARGS=` supports `--limit`, `--activity-id`, `--reenrich`, `--dry-run`, `--max-concurrent`. |
| `make dev-backend` | `uvicorn radar.main:app --reload --port 8000`. |
| `make dev-frontend` | `cd frontend && pnpm dev` (port 3000). |
| `make eval` | Runs the eval harness and writes `evals/report.md` (planned — fixture ground-truth needs Aden's pass first). |

`.env` lives at the repo root with `ANTHROPIC_API_KEY=...` and is gitignored from commit 1. The frontend never sees the key — every LLM call is brokered through the backend.

---

## 14. Repo layout

```
ai-policy-radar/
├── README.md                       # this file
├── Makefile
├── pyproject.toml
├── .env                            # gitignored
├── backend/
│   ├── radar/
│   │   ├── main.py                 # FastAPI app + lifespan-scoped engine/builder
│   │   ├── api/                    # routes.py + schemas.py
│   │   ├── db/                     # schema.sql, connection.py, payload_schemas.py, storage.py
│   │   ├── adapters/               # structured/{federal_register,...}, research/per_entity.py
│   │   ├── enrich/                 # pipeline.py, prompts/, embedding_model.py
│   │   ├── awareness/              # engine.py, retrieval.py, reasoner.py, actions.py
│   │   ├── profiles/               # builder.py
│   │   └── scripts/                # seed_entities.py, ingest.py, enrich.py
│   └── data/                       # entity_seed.yaml, topics.yaml, source_plan.json
├── frontend/
│   ├── app/                        # /, /profile/new, /dashboard/[id], /entities, /activity/[id]
│   ├── components/                 # ui/ (shadcn) + radar/ (AwarenessCard, DashboardView, ...)
│   └── lib/                        # api.ts, format.ts
├── evals/                          # cases.yaml, run.py, judge.py, report.md
└── data/
    └── radar.db                    # gitignored
```

Full layout in `BUILD_PLAN.md` §20.

---

## 15. License & credits

Single-author 9-hour build by Aden Barton for the Perplexity Spike Round (Apr 26 2026), with a long-running planner agent driving subagent dispatch (see `BUILD_PLAN.md` §3 for the planner pattern). License: TBD; treat as private until a license file is added.
