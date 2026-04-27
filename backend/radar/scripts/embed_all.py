"""Populate the `activity_embedding` vec0 virtual table.

Reads activities (joined with their enrichment) that lack an
`activity_embedding` row, embeds `title + "\\n\\n" + summary`, and INSERTs
into the vec0 table.

CLI:
    PYTHONPATH=backend uv run python -m radar.scripts.embed_all
    PYTHONPATH=backend uv run python -m radar.scripts.embed_all --limit 200
    PYTHONPATH=backend uv run python -m radar.scripts.embed_all --reembed
    PYTHONPATH=backend uv run python -m radar.scripts.embed_all --dry-run

Notes:
- The vec0 virtual table's embedding column has a fixed FLOAT[N] width set
  by `connection.EMBEDDING_DIM`. If the model's output dim doesn't match,
  this script aborts early with a clear error message rather than writing
  truncated/zero-padded vectors.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Match the convention used by enrich.py — load .env if present.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from radar.awareness.embedding_model import EmbeddingModel  # noqa: E402
from radar.db.connection import EMBEDDING_DIM, bootstrap  # noqa: E402

log = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="radar.scripts.embed_all",
        description="Embed activities and populate the activity_embedding vec0 table.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Embed at most N activities (default: 200).",
    )
    p.add_argument(
        "--reembed",
        action="store_true",
        help="Overwrite existing rows in activity_embedding.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the embed step but don't write to the DB.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Embed batch size (default: 32).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return p.parse_args(argv)


def _select_targets(conn, *, reembed: bool, limit: int) -> list[dict]:
    """Pull (activity_id, title, summary) rows to embed."""
    if reembed:
        sql = """
            SELECT a.id AS activity_id, a.title AS title, en.summary AS summary
            FROM activity a
            LEFT JOIN enrichment en ON en.activity_id = a.id
            ORDER BY a.occurred_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (limit,)).fetchall()
    else:
        # Skip activities that already have a vec0 row.
        sql = """
            SELECT a.id AS activity_id, a.title AS title, en.summary AS summary
            FROM activity a
            LEFT JOIN enrichment en ON en.activity_id = a.id
            WHERE NOT EXISTS (
                SELECT 1 FROM activity_embedding ae WHERE ae.activity_id = a.id
            )
            ORDER BY a.occurred_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def _compose_text(row: dict) -> str:
    title = (row.get("title") or "").strip()
    summary = (row.get("summary") or "").strip()
    if title and summary:
        return f"{title}\n\n{summary}"
    return title or summary or ""


def _upsert_embedding(
    conn, activity_id: str, vec: list[float], *, reembed: bool
) -> None:
    """Insert or replace an embedding row.

    The vec0 virtual table doesn't support ON CONFLICT, so we do a DELETE +
    INSERT under --reembed, and a plain INSERT otherwise. The selection
    queries above guarantee no duplicate on the non-reembed path.
    """
    emb_json = json.dumps(vec)
    if reembed:
        conn.execute(
            "DELETE FROM activity_embedding WHERE activity_id = ?", (activity_id,)
        )
    conn.execute(
        "INSERT INTO activity_embedding(activity_id, embedding) VALUES (?, ?)",
        (activity_id, emb_json),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = bootstrap()
    try:
        model = EmbeddingModel.get()
        if model is None:
            print(
                "[embed_all] embedding model unavailable — install the [embeddings] "
                "extra (model2vec) and ensure RADAR_DISABLE_EMBEDDING is unset."
            )
            return 2

        if model.dim != EMBEDDING_DIM:
            print(
                f"[embed_all] DIM MISMATCH: model {model.model_name} produces "
                f"{model.dim}-dim vectors, but the vec0 schema is FLOAT[{EMBEDDING_DIM}]. "
                f"Set RADAR_EMBEDDING_DIM={model.dim} (or use a different model) "
                f"and re-bootstrap."
            )
            return 3

        targets = _select_targets(
            conn, reembed=args.reembed, limit=args.limit
        )
        if not targets:
            print(
                "[embed_all] no activities to embed (use --reembed to overwrite "
                "existing rows)."
            )
            return 0

        print(
            f"[embed_all] model={model.model_name} dim={model.dim} "
            f"targets={len(targets)} batch_size={args.batch_size} "
            f"dry_run={args.dry_run} reembed={args.reembed}"
        )

        embedded = 0
        skipped_empty = 0
        t0 = time.monotonic()
        batch_size = max(1, args.batch_size)
        for start in range(0, len(targets), batch_size):
            batch = targets[start : start + batch_size]
            texts = [_compose_text(r) for r in batch]

            # Filter out empty texts — model2vec encode returns zero-vectors
            # there and they pollute cosine ranking.
            usable: list[tuple[dict, str]] = [
                (r, t) for r, t in zip(batch, texts) if t.strip()
            ]
            skipped_empty += len(batch) - len(usable)
            if not usable:
                continue

            vecs = model.encode([t for _, t in usable])
            if not args.dry_run:
                for (row, _t), v in zip(usable, vecs):
                    _upsert_embedding(
                        conn, row["activity_id"], v, reembed=args.reembed
                    )
            embedded += len(usable)
            print(
                f"[embed_all] batch {start // batch_size + 1}: "
                f"{len(usable)} embedded ({embedded}/{len(targets)})"
            )

        if not args.dry_run:
            conn.commit()

        elapsed = time.monotonic() - t0
        print()
        print("=== summary ===")
        print(f"  embedded:      {embedded}")
        print(f"  skipped_empty: {skipped_empty}")
        print(f"  elapsed:       {elapsed:.2f}s")
        if not args.dry_run:
            count = conn.execute(
                "SELECT COUNT(*) FROM activity_embedding"
            ).fetchone()[0]
            print(f"  total rows in activity_embedding: {count}")
        else:
            print("  (dry-run — no rows written)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
