"""Rebuild the static JSON snapshots used by the frontend's DEMO_MODE.

Hits a running FastAPI backend on http://127.0.0.1:8000 and writes:
  frontend/public/snapshots/profiles.json
  frontend/public/snapshots/profile_{id}.json
  frontend/public/snapshots/{id}.json                (dashboard)
  frontend/public/snapshots/entities.json
  frontend/public/snapshots/entities/{slug}.json     (entity detail)
  frontend/public/snapshots/activities/{id}.json     (activity detail)

Idempotent. Skips the "Test Frontier" placeholder profile.
"""
from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

API = "http://127.0.0.1:8000"
SINCE = "2024-01-01"
TOP_K = 15

DEMO_PROFILE_IDS = {
    "2c4771bfe57e",  # Frontier Lab Policy Lead
    "3d27db24e009",  # Healthcare AI Startup
    "94cbcfb90d10",  # State AG (CA-style)
    "b80d5d3ddb26",  # Datacenter Buildout Investor
}
SKIP_PROFILE_IDS = {"8b4f6e492b9c"}  # Test Frontier

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAP = REPO_ROOT / "frontend" / "public" / "snapshots"


def fetch(path: str) -> dict | list:
    url = f"{API}{path}"
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.loads(r.read())


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None, separators=(",", ":")))


def main() -> int:
    SNAP.mkdir(parents=True, exist_ok=True)
    (SNAP / "entities").mkdir(exist_ok=True)
    (SNAP / "activities").mkdir(exist_ok=True)

    print(f"[snapshot] backend={API} since={SINCE} top_k={TOP_K}")

    # 1. profiles.json (filter out skipped)
    all_profiles = fetch("/profiles")
    kept_profiles = [p for p in all_profiles if p["id"] not in SKIP_PROFILE_IDS]
    write_json(SNAP / "profiles.json", kept_profiles)
    print(f"[snapshot] profiles.json — {len(kept_profiles)} profiles")

    seen_activities: set[str] = set()
    seen_entity_ids: set[str] = set()

    # 2. per-profile dashboard + profile metadata
    for p in kept_profiles:
        pid = p["id"]
        if pid not in DEMO_PROFILE_IDS:
            print(f"[snapshot]   skipping {pid} (not in demo set)")
            continue

        profile = fetch(f"/profiles/{pid}")
        write_json(SNAP / f"profile_{pid}.json", profile)

        dash = fetch(f"/dashboard/{pid}?since={SINCE}&top_k={TOP_K}")
        write_json(SNAP / f"{pid}.json", dash)

        items = dash.get("items", [])
        for it in items:
            act = it.get("activity") or {}
            aid = act.get("id")
            if aid:
                seen_activities.add(aid)
            ent = it.get("source_entity")
            if ent and ent.get("id"):
                seen_entity_ids.add(ent["id"])
            if act.get("entity_id"):
                seen_entity_ids.add(act["entity_id"])

        print(f"[snapshot]   {pid} '{p['name']}' — {len(items)} items")

    # 3. entities.json (all entities, full list)
    entities_list = fetch("/entities")
    write_json(SNAP / "entities.json", entities_list)
    print(f"[snapshot] entities.json — {len(entities_list)} entities")

    # 4. per-entity detail (only for entities referenced by snapshot dashboards
    #    + any already-snapped to keep parity; cheap to do all)
    referenced_or_all = {e["id"] for e in entities_list}
    for eid in sorted(referenced_or_all):
        try:
            detail = fetch(f"/entities/{eid}")
            write_json(SNAP / "entities" / f"{eid}.json", detail)
        except urllib.error.HTTPError as e:
            print(f"[snapshot]   entity {eid} — http {e.code}, skipping")

    # 5. per-activity detail (only those referenced by demo dashboards)
    for aid in sorted(seen_activities):
        try:
            detail = fetch(f"/activities/{aid}")
            write_json(SNAP / "activities" / f"{aid}.json", detail)
        except urllib.error.HTTPError as e:
            print(f"[snapshot]   activity {aid} — http {e.code}, skipping")

    print(
        f"[snapshot] done. activities={len(seen_activities)} "
        f"entities={len(referenced_or_all)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
