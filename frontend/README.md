# AI Policy Radar — frontend

Next.js 16 (App Router) + TypeScript + Tailwind v4 + shadcn/ui. Pure UI; all
data flows through the FastAPI backend.

## Dev workflow

Two shells, from the repo root (`ai-policy-radar/`):

```bash
make dev-backend   # FastAPI on :8000
make dev-frontend  # Next.js on :3000
```

Or directly:

```bash
cd backend && PYTHONPATH=. uv run uvicorn radar.main:app --port 8000
cd frontend && pnpm dev
```

## Configuration

Copy `.env.local.example` → `.env.local` and adjust if your backend is not on
`localhost:8000`:

```
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

## Pages

- `/` — profile list + create CTA
- `/profile/new` — natural-language profile builder
- `/dashboard/[profileId]` — ranked awareness items, sidebar facets
- `/entities` — directory of tracked entities
- `/entities/[id]` — entity detail (recent activities + topic stats)
- `/activity/[id]` — full enrichment view + raw payload

## Build

```bash
pnpm build
```

The frontend never sees the Anthropic API key — all LLM calls happen on the
backend. Do not introduce `sk-ant-*` references into this package.
