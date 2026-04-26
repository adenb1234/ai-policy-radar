"""SQLite connection helper with sqlite-vec extension and schema bootstrap."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import sqlite_vec

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "radar.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
EMBEDDING_DIM = int(os.environ.get("RADAR_EMBEDDING_DIM", "384"))


def get_db_path() -> Path:
    return Path(os.environ.get("RADAR_DB", str(DEFAULT_DB_PATH)))


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so FastAPI's sync-Depends-into-async-route pattern
    # (threadpool open, event-loop use) doesn't trip ProgrammingError. SQLite is
    # serialized inside the C lib; per-request connections still avoid contention.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS activity_embedding "
        f"USING vec0(activity_id TEXT PRIMARY KEY, embedding FLOAT[{EMBEDDING_DIM}]);"
    )
    conn.commit()


def bootstrap(db_path: Path | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    init_schema(conn)
    return conn


if __name__ == "__main__":
    conn = bootstrap()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    print("tables:", [r[0] for r in rows])
