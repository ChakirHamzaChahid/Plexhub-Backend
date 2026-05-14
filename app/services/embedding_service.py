"""Lazy singleton wrapper around fastembed TextEmbedding for intfloat/multilingual-e5-small.

Boot: zero IO. The model is downloaded and loaded on first call (cold start ~30s).
All inference is offloaded to asyncio.to_thread to keep the event loop responsive.
On model load failure (offline, download error), get_model() raises
EmbeddingUnavailableError; verify_api_key (J3b) catches this and returns 503.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

logger = logging.getLogger("plexhub.ai.embedding")

MODEL_NAME = "intfloat/multilingual-e5-small"
EMBEDDING_DIM = 384

_MODEL_LOCK = asyncio.Lock()
_model: Any | None = None


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the embedding model cannot be loaded (offline, corrupt cache, etc.)."""


def _load_model_blocking() -> Any:
    """Synchronous model factory. Called inside asyncio.to_thread."""
    from fastembed import TextEmbedding  # local import: defer fastembed cost to first call
    return TextEmbedding(model_name=MODEL_NAME)


async def get_model() -> Any:
    """Return the singleton TextEmbedding instance, loading it on first call.

    Raises EmbeddingUnavailableError if the model cannot be instantiated
    (network failure, missing wheel, corrupt cache).
    """
    global _model
    if _model is not None:
        return _model
    async with _MODEL_LOCK:
        if _model is not None:
            return _model
        try:
            _model = await asyncio.to_thread(_load_model_blocking)
        except Exception as exc:
            logger.warning("fastembed model load failed: %s", exc)
            raise EmbeddingUnavailableError(str(exc)) from exc
        return _model


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return vec
    return vec / norm


def l2_normalize(values: list[float]) -> list[float]:
    """Public L2-normalize helper for J3a centroid combination."""
    arr = np.asarray(values, dtype=np.float32)
    return _l2_normalize(arr).tolist()


def weighted_centroid(vecs: list[list[float]], weights: list[float]) -> list[float]:
    """Weighted mean of vectors, then L2-normalize. Both lists must be same length."""
    if not vecs:
        raise ValueError("weighted_centroid requires at least one vector")
    if len(vecs) != len(weights):
        raise ValueError("vecs and weights length mismatch")
    matrix = np.asarray(vecs, dtype=np.float32)  # (N, 384)
    w = np.asarray(weights, dtype=np.float32).reshape(-1, 1)  # (N, 1)
    summed = (matrix * w).sum(axis=0)
    return _l2_normalize(summed).tolist()


def _embed_blocking(prefix: str, texts: list[str]) -> list[list[float]]:
    """Synchronous embedding call. Must be wrapped in asyncio.to_thread."""
    if _model is None:
        raise EmbeddingUnavailableError("model not loaded")
    prepared = [f"{prefix}{text}" for text in texts]
    # fastembed yields generator of np.ndarray
    raw_vecs = list(_model.embed(prepared))
    normalized = [_l2_normalize(np.asarray(v, dtype=np.float32)).tolist() for v in raw_vecs]
    return normalized


async def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed candidate documents (E5 'passage:' prefix). L2-normalized output."""
    if not texts:
        return []
    await get_model()  # ensures _model is loaded or raises EmbeddingUnavailableError
    return await asyncio.to_thread(_embed_blocking, "passage: ", texts)


async def embed_query(text: str) -> list[float]:
    """Embed a single query string (E5 'query:' prefix). L2-normalized output."""
    await get_model()
    result = await asyncio.to_thread(_embed_blocking, "query: ", [text])
    return result[0]
