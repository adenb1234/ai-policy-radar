SHELL := /bin/bash
.PHONY: help install install-backend install-frontend dev dev-backend dev-frontend seed ingest enrich eval clean

help:
	@echo "AI Policy Radar — Makefile targets"
	@echo "  install         Install backend (uv) + frontend (pnpm) deps"
	@echo "  dev             Run backend (:8000) and frontend (:3000)"
	@echo "  seed            Seed entities + topics from YAML into SQLite"
	@echo "  ingest          Run source adapters and ingest activities"
	@echo "  enrich          Enrich activities (topics, stance, summary, embeddings)"
	@echo "  eval            Run eval harness and produce report"

install: install-backend install-frontend

install-backend:
	uv sync --extra dev

install-frontend:
	cd frontend && pnpm install

dev-backend:
	uv run uvicorn radar.main:app --app-dir backend --reload --port 8000

dev-frontend:
	cd frontend && pnpm dev

dev:
	@echo "Run 'make dev-backend' and 'make dev-frontend' in separate shells."

seed:
	uv run python -m radar.scripts.seed_entities

ingest:
	PYTHONPATH=backend uv run python -m radar.scripts.ingest $(ARGS)

enrich:
	PYTHONPATH=backend uv run python -m radar.scripts.enrich $(ARGS)

eval:
	uv run python evals/run.py
