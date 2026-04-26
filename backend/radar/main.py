"""FastAPI app entry — radar.main:app.

Wires:
  - .env loading (so ANTHROPIC_API_KEY is in the env before SDK init)
  - lifespan: schema bootstrap + lazy ProfileBuilder / AwarenessEngine
  - CORS for the Next.js dev server (3000 + 3001)
  - the router from `radar.api.routes` (all endpoints except /health)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load .env BEFORE any module that reads ANTHROPIC_API_KEY at import time.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from radar.api.routes import router as api_router  # noqa: E402
from radar.db.connection import bootstrap  # noqa: E402

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure schema exists once at startup.
    try:
        conn = bootstrap()
        conn.close()
    except Exception:  # noqa: BLE001
        log.exception("schema bootstrap failed at startup")

    # Lazy-construct lifespan-scoped helpers. These require ANTHROPIC_API_KEY;
    # if it's missing we leave them as None and the routes will return 503.
    app.state.profile_builder = None
    app.state.awareness_engine = None
    try:
        from radar.profiles.builder import ProfileBuilder

        app.state.profile_builder = ProfileBuilder()
    except Exception:  # noqa: BLE001
        log.exception("ProfileBuilder unavailable")

    try:
        from radar.awareness.engine import AwarenessEngine

        app.state.awareness_engine = AwarenessEngine()
    except Exception:  # noqa: BLE001
        log.exception("AwarenessEngine unavailable")

    yield


app = FastAPI(title="AI Policy Radar", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


app.include_router(api_router)
