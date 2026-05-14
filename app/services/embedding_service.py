"""Lazy singleton wrapper around fastembed TextEmbedding.

Model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384 dim,
multilingual, Apache-2.0). fastembed >= 0.7 dropped native support for
intfloat/multilingual-e5-small; this MiniLM model is the official 384-dim
multilingual substitute and does NOT require E5-style "passage:"/"query:"
prefixes — input texts are embedded as-is.

Override via AI_EMBED_MODEL env var if you bring a custom model.

Boot: zero IO. The model is downloaded and loaded on first call (cold start ~30s).
All inference is offloaded to asyncio.to_thread to keep the event loop responsive.
On model load failure (offline, download error), get_model() raises
EmbeddingUnavailableError; verify_api_key catches this and returns 503.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

logger = logging.getLogger("plexhub.ai.embedding")

DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384


def _resolve_model_name() -> str:
    """Resolve the model name from settings.AI_EMBED_MODEL or fall back to default.

    Read lazily so test monkeypatch + container env override both work.
    """
    from app.config import settings
    return getattr(settings, "AI_EMBED_MODEL", "") or DEFAULT_MODEL_NAME


# Back-compat alias kept for code that still imports MODEL_NAME (status endpoint).
MODEL_NAME = DEFAULT_MODEL_NAME

_MODEL_LOCK = asyncio.Lock()
_model: Any | None = None


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the embedding model cannot be loaded (offline, corrupt cache, etc.)."""


def _load_model_blocking() -> Any:
    """Synchronous model factory. Called inside asyncio.to_thread.

    Honors settings.AI_EMBED_CACHE_DIR when set; otherwise fastembed picks its
    default (~/.cache/fastembed) which is ephemeral inside a container.
    """
    from fastembed import TextEmbedding  # local import: defer fastembed cost to first call
    from app.config import settings

    name = _resolve_model_name()
    if settings.AI_EMBED_CACHE_DIR:
        return TextEmbedding(model_name=name, cache_dir=settings.AI_EMBED_CACHE_DIR)
    return TextEmbedding(model_name=name)


async def get_model() -> Any:
    """Return the singleton TextEmbedding instance, loading it on first call.

    Raises EmbeddingUnavailableError if the model cannot be instantiated
    (network failure, missing wheel, corrupt cache, unsupported model name).
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
    """Public L2-normalize helper for centroid combination."""
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


def _embed_blocking(texts: list[str]) -> list[list[float]]:
    """Synchronous embedding call. Must be wrapped in asyncio.to_thread."""
    if _model is None:
        raise EmbeddingUnavailableError("model not loaded")
    # MiniLM does not use E5-style prefixes; embed texts as-is.
    raw_vecs = list(_model.embed(texts))
    return [_l2_normalize(np.asarray(v, dtype=np.float32)).tolist() for v in raw_vecs]


async def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed candidate documents. L2-normalized output."""
    if not texts:
        return []
    await get_model()  # ensures _model is loaded or raises EmbeddingUnavailableError
    return await asyncio.to_thread(_embed_blocking, texts)


async def embed_query(text: str) -> list[float]:
    """Embed a single query string. L2-normalized output."""
    await get_model()
    result = await asyncio.to_thread(_embed_blocking, [text])
    return result[0]
