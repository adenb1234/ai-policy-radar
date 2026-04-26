# RECAP.md — Recorded recap outline

> Bullet outline to read off, not a script. Target: 4–5 minute demo + 3–4 minute walkthrough.

---

## 1. Demo flow (4–5 min)

- **Open landing** at `localhost:3000`. Show the (intentionally sparse) profile list — point out: "no pre-baked personas, and that's the whole point."
- **Click "Create a profile"** → `/profile/new`. Paste a fresh, specific NL paragraph live (frontier-lab policy lead). Note the textarea is the only required input — the structured form gets *extracted* by Opus from the paragraph.
- **Submit** → ~15s wait → land on dashboard. Call out: in those 15s the backend ran a single Opus call that turned a paragraph into `topics_weighted + watch_entities + jurisdictions + activity_type_filters + action_thresholds`, persisted both halves, then ran the 3-layer awareness engine.
- **Walk one AwarenessCard**: relevance score → source entity chip → reasoning paragraph → stance + extracted quote → matched topics → recommended actions → citation chips. Emphasize that the citation chips name *enrichment field paths*, not just URLs.
- **Click an entity chip** → entity page. Show top-3 topics + dominant stance computed from enrichments, recent activity list, alias coverage.
- **Click an activity title** → activity detail. Show summary + stance + quote + materiality cards (scope/bindingness/novelty/confidence) + raw `payload` JSON rail. Emphasize the source URL is real and verified.
- **Spin up a second profile live** (state AG, 30 seconds) to show the dashboard rerank. Same activity table, completely different ranking — that's the customization story.

---

## 2. Architecture talking points (3–4 min)

- **Three-layer retrieval.** Layer 1 = cheap structured filter against profile (topics + watchlist + jurisdiction). Layer 2 = embedding rerank, MiniLM cosine merged 60/40. Layer 3 = Opus 4.7 rerank that produces score + reasoning + actions + citations *in one call*. No second round-trip per item.
- **Prompt caching.** System prompt for Opus = rubric + sorted-key profile JSON. Sorted keys = byte-stable cache prefix. Calls 2..N within a dashboard build hit the cache. Same pattern on the Sonnet enrichment side — ~15K-token system block (topic vocab + entity directory + activity-type guidance) cached, only the per-activity user turn pays full rate.
- **URL verification is mandatory on the research adapter.** Every Activity emitted by Opus + web search is post-processed: re-fetch source URL, confirm 200, substring-check model-supplied verify-phrases against the response. Drop on fail. This is the only thing standing between the demo and a fabricated source URL on stage.
- **Profile builder is the customization story.** The spec said no pre-baked personas — so the system literally cannot ship pre-baked. A paragraph in, a structured profile out, persisted. Three personas during the demo, each one built live.
- **Verification posture.** Every enrichment field is post-validated: topic ids not in the vocab dropped; `stance_quote` substring-checked against `raw_text`; mentioned-entity ids resolved against the entity table. The system can't quote what the source doesn't say, can't tag what doesn't exist.

---

## 3. Tradeoffs to mention (2 min)

- **SQLite + sqlite-vec over Postgres + pgvector.** 9-hour scope. Embedded, zero ops, one file. Would migrate to Postgres for multi-user.
- **Sonnet 4.6 / Opus 4.7 split.** Cheap structured extraction at scale, expensive judgment work where it matters. All-Opus would be fine on quality and ruinous on cost; all-Sonnet would risk the multi-hop reasoning in the awareness layer.
- **URL verification as a hard rule, not a heuristic.** A demo that opens to a fabricated `nist.gov` URL is a dead demo. The cost is two HTTP calls and a substring check per Activity; the benefit is sleeping at night.

---

## 4. What I'd build next given more time (priority-ordered)

- **Coalition / alignment detection.** Highest stretch ROI per `BUILD_PLAN.md` §17. Cluster entities by stance per topic per window; surface stance shifts (entity moved from oppose → mixed → support). Reuses enrichment data, no new infra. New `/coalitions` page.
- **Torch-free embeddings via fastembed / ONNX.** Kills the Mac-x86_64 wheel issue, makes Layer 2 real on every install.
- **EDGAR + congress.gov + CourtListener structured adapters.** Wired and ready; gated on API keys. 1–2 hours each once keys land.
- **Ask-the-inventory chat surface.** Tool-calling against the entity / activity / enrichment tables. Perplexity-flavored, single search box, grounded answers.
- **Weekly auto-brief generator.** Per-profile 1-page Markdown summary, mailable.

---

## 5. Things I'd surface honestly

- **The Mac torch issue.** `sentence-transformers` has no Mac-x86_64 wheel for Python 3.13. Layer 2 falls back to identity scoring — Layer 1 carries the rerank, Layer 3 (Opus) compensates because it sees the full profile + activity. Honest about it; not pretending the embeddings are doing more than they are.
- **Anthropic dev-tier rate limits.** ~30K input tokens / minute on Sonnet. Enrichment over a few hundred activities runs in real wall-clock minutes, not seconds. Bounded asyncio semaphore prevents 429s.
- **Eval ground truth requires a human.** Cases drafted by the planner; `expected_*` fields must be filled by Aden. A model that writes both the cases and the answers tests nothing — calling that out, and not pretending the eval suite is meaningful until Aden's pass lands.
- **Coverage is uneven.** Federal Register is thin for entities like NIST/AISI that publish off-FR. A few civil-society entities yielded fewer activities than expected. The research adapter compensates but isn't perfect.
- **Live-agent fallback logged but disabled.** When retrieval thins out the engine emits a `[fallback]` log line; the actual on-the-fly research-subagent call is wired but gated off until I've seen real demo coverage gaps.
