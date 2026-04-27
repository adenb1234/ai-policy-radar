"""Lazy singleton wrapper around a torch-free static-embedding model.

Originally targeted `sentence-transformers/all-MiniLM-L6-v2`, but `torch` has
no Mac-x86_64 wheel for the active Python build, so we substitute model2vec
(pure-Python, numpy + tokenizers only). The default model is
`minishlab/potion-base-8M` — a 256-dim distillation. See
`radar.db.connection.EMBEDDING_DIM` (env-overridable, default 256) — the
schema's `vec0(... FLOAT[N])` virtual table must match.

The package is an optional extra (`pip install ai-policy-radar-backend[embeddings]`).
If unavailable, `EmbeddingModel.get()` returns None — callers must handle None
gracefully and skip embedding-based scoring.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


class EmbeddingModel:
    """Process-singleton wrapper around a model2vec StaticModel."""

    _instance: Optional["EmbeddingModel"] = None
    _resolution_attempted: bool = False
    _lock = threading.Lock()
    # Default model — 256-dim. Override with RADAR_EMBEDDING_MODEL env.
    DEFAULT_MODEL_NAME = "minishlab/potion-base-8M"

    def __init__(self, model: object, model_name: str, dim: int) -> None:
        # `model` is a model2vec StaticModel; typed as object to keep
        # the import optional.
        self._model = model
        self._model_name = model_name
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    @classmethod
    def get(cls) -> Optional["EmbeddingModel"]:
        """Lazily load and return the singleton, or None if unavailable.

        On the first call, attempt to import model2vec and load the model.
        Subsequent calls return the cached instance (or cached None if
        loading failed).

        Honors `RADAR_DISABLE_EMBEDDING=1` — returns None without trying
        to import (useful for benchmarking the identity-fallback path).
        """
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is not None:
                return cls._instance
            if cls._resolution_attempted:
                return cls._instance  # already tried, gave up
            cls._resolution_attempted = True

            if os.environ.get("RADAR_DISABLE_EMBEDDING", "").strip() in ("1", "true", "yes"):
                log.info(
                    "[awareness.embedding_model] RADAR_DISABLE_EMBEDDING set; "
                    "embeddings disabled"
                )
                return None

            try:
                from model2vec import StaticModel  # type: ignore
            except ImportError:
                log.info(
                    "[awareness.embedding_model] model2vec not installed; "
                    "embeddings disabled (install with the [embeddings] extra)"
                )
                return None

            model_name = os.environ.get("RADAR_EMBEDDING_MODEL", cls.DEFAULT_MODEL_NAME)
            try:
                model = StaticModel.from_pretrained(model_name)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[awareness.embedding_model] failed to load %s: %s",
                    model_name,
                    e,
                )
                return None

            # Probe the dimensionality on a single token so callers (and the
            # embed_all script) can confirm the schema agrees.
            try:
                probe = model.encode(["dim probe"])
                # model2vec returns a numpy array shape (N, D)
                dim = int(probe.shape[-1])
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[awareness.embedding_model] failed to probe dim for %s: %s",
                    model_name,
                    e,
                )
                return None

            cls._instance = cls(model, model_name, dim)
            log.info(
                "[awareness.embedding_model] loaded %s dim=%d (singleton)",
                model_name,
                dim,
            )
            return cls._instance

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a list of texts into normalized embedding vectors.

        Returns L2-normalized rows so cosine similarity is a dot product.
        """
        if not texts:
            return []
        # model2vec returns numpy arrays directly.
        vecs = self._model.encode(texts)  # type: ignore[attr-defined]
        out: list[list[float]] = []
        for v in vecs:
            # L2 normalize for cosine-friendliness (model2vec doesn't do this
            # by default; sentence-transformers `normalize_embeddings=True`
            # used to give us the same property).
            norm_sq = 0.0
            for x in v:
                norm_sq += float(x) * float(x)
            if norm_sq <= 0.0:
                out.append([float(x) for x in v])
                continue
            inv = 1.0 / (norm_sq ** 0.5)
            out.append([float(x) * inv for x in v])
        return out


__all__ = ["EmbeddingModel"]
