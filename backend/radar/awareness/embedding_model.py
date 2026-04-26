"""Lazy singleton wrapper around sentence-transformers/all-MiniLM-L6-v2.

The package is an optional extra (`pip install ai-policy-radar-backend[embeddings]`).
If unavailable, `EmbeddingModel.get()` returns None — callers must handle None
gracefully and skip embedding-based scoring.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class EmbeddingModel:
    """Process-singleton wrapper around sentence-transformers MiniLM."""

    _instance: Optional["EmbeddingModel"] = None
    _resolution_attempted: bool = False
    _lock = threading.Lock()
    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self, model: object) -> None:
        # `model` is a SentenceTransformer instance; typed as object to keep
        # the import optional.
        self._model = model

    @classmethod
    def get(cls) -> Optional["EmbeddingModel"]:
        """Lazily load and return the singleton, or None if unavailable.

        On the first call, attempt to import sentence_transformers and load
        the MiniLM model. Subsequent calls return the cached instance (or
        cached None if loading failed).
        """
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is not None:
                return cls._instance
            if cls._resolution_attempted:
                return cls._instance  # already tried, gave up
            cls._resolution_attempted = True
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError:
                log.info(
                    "[awareness.embedding_model] sentence_transformers not installed; "
                    "embeddings disabled (install with the [embeddings] extra)"
                )
                return None
            try:
                model = SentenceTransformer(cls.MODEL_NAME)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[awareness.embedding_model] failed to load %s: %s",
                    cls.MODEL_NAME,
                    e,
                )
                return None
            cls._instance = cls(model)
            log.info(
                "[awareness.embedding_model] loaded %s (singleton)", cls.MODEL_NAME
            )
            return cls._instance

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a list of texts into normalized embedding vectors."""
        if not texts:
            return []
        # convert_to_numpy=True for speed; we'll list-cast at the end.
        vecs = self._model.encode(  # type: ignore[attr-defined]
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [list(map(float, v)) for v in vecs]


__all__ = ["EmbeddingModel"]
