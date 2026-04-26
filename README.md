# AI Policy Radar

A system that inventories the positions, priorities, and activities of entities active in AI policy and regulation, and powers a per-user "awareness dashboard" surfacing the developments most relevant to a given user's stated equities — with reasoning, citations, and recommended actions.

Built for the Perplexity Spike Round interview (9-hour async build, Apr 26 2026).

For the full design — architecture, data model, phase plan, decisions — see [`../BUILD_PLAN.md`](../BUILD_PLAN.md). Current progress lives in [`../BUILD_STATE.md`](../BUILD_STATE.md); subagent activity in [`../BUILD_LOG.md`](../BUILD_LOG.md).

## Stack

- **Backend:** Python 3.11+, FastAPI, SQLite + sqlite-vec, Anthropic SDK (Claude Opus 4.7 + Sonnet 4.6)
- **Frontend:** Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui

## Run (placeholder — filled in as phases land)

```bash
# 1. Install
make install

# 2. Seed entities + topics
make seed

# 3. Ingest activities from sources
make ingest

# 4. Enrich activities (topics, stance, summary, embeddings)
make enrich

# 5. Run dev (backend on :8000, frontend on :3000)
make dev

# 6. (post-MVP) Run evals
make eval
```

## Layout

See `BUILD_PLAN.md` §20 for the full layout. Top level: `backend/`, `frontend/`, `evals/`, `data/`.

## Secrets

`.env` lives at the repo root with `ANTHROPIC_API_KEY=...`. It is gitignored from commit 1. Never commit secrets.
