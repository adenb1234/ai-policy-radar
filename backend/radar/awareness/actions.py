"""Helpers for serializing recommended_actions to/from the SQL JSON column.

`awareness_item.recommended_actions` is a TEXT column holding a JSON array.
"""
from __future__ import annotations

import json


def serialize_actions(actions: list[str]) -> str:
    """Serialize a list of action strings to a JSON array string.

    Coerces each entry to str and drops empty/whitespace-only entries.
    """
    cleaned: list[str] = []
    for a in actions or []:
        if not isinstance(a, str):
            a = str(a)
        s = a.strip()
        if s:
            cleaned.append(s)
    return json.dumps(cleaned, ensure_ascii=False)


def deserialize_actions(blob: str) -> list[str]:
    """Inverse of `serialize_actions`. Empty/invalid blobs return []."""
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, (str, int, float))]


def serialize_citations(citations: list[str]) -> str:
    """Same shape as recommended_actions — a JSON array of strings."""
    return serialize_actions(citations)


def deserialize_citations(blob: str) -> list[str]:
    return deserialize_actions(blob)


__all__ = [
    "serialize_actions",
    "deserialize_actions",
    "serialize_citations",
    "deserialize_citations",
]
