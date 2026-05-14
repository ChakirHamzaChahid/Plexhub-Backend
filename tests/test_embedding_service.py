"""Unit tests for app.services.embedding_service.

All tests mock the fastembed TextEmbedding factory to avoid real model
download or ONNX runtime initialization.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.services import embedding_service
from app.services.embedding_service import (
    EMBEDDING_DIM,
    EmbeddingUnavailableError,
    MODEL_NAME,
    embed_passages,
    embed_query,
    get_model,
    weighted_centroid,
)


pytestmark = pytest.mark.asyncio


def _make_fake_text_embedding_cls(call_counter: dict[str, int]):
    """Build a fake TextEmbedding class that returns deterministic unit vectors.

    Each call to embed() yields one vector per input, dim=384.
    """
    class _Fake:
        def __init__(self, model_name: str, **_):
            assert model_name == MODEL_NAME
            call_counter["init"] = call_counter.get("init", 0) + 1

        def embed(self, texts):
            call_counter["embed"] = call_counter.get("embed", 0) + 1
            for i, _ in enumerate(texts):
                vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
                vec[i % EMBEDDING_DIM] = 1.0
                yield vec

    return _Fake


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset module-level singleton before each test."""
    embedding_service._model = None
    yield
    embedding_service._model = None


async def test_config_default_empty():
    from app.config import settings
    assert hasattr(settings, "AI_API_KEY")


async def test_embed_passage_dim_and_norm(monkeypatch):
    counter: dict[str, int] = {}
    monkeypatch.setattr(
        "app.services.embedding_service._load_model_blocking",
        lambda: _make_fake_text_embedding_cls(counter)(MODEL_NAME),
    )

    result = await embed_passages(["hello world"])
    assert len(result) == 1
    assert len(result[0]) == EMBEDDING_DIM
    # L2 norm ≈ 1
    norm = float(np.linalg.norm(np.asarray(result[0])))
    assert abs(norm - 1.0) < 1e-4


async def test_query_prefix_distinct_from_passage(monkeypatch):
    """Query and passage embeddings must differ because the prefix differs.

    Our fake returns a vector based on input INDEX (not content), so to
    distinguish we instead verify the prefix path is taken (different code path).
    """
    captured: list[str] = []

    class _Fake:
        def __init__(self, **_): pass

        def embed(self, texts):
            captured.extend(texts)
            for _ in texts:
                yield np.ones(EMBEDDING_DIM, dtype=np.float32)

    monkeypatch.setattr(
        "app.services.embedding_service._load_model_blocking",
        lambda: _Fake(),
    )
    await embed_passages(["foo"])
    await embed_query("bar")
    assert any(t.startswith("passage: ") for t in captured)
    assert any(t.startswith("query: ") for t in captured)


async def test_singleton_under_lock(monkeypatch):
    counter: dict[str, int] = {"init": 0}

    class _Fake:
        def __init__(self, **_):
            counter["init"] += 1

        def embed(self, texts):
            for _ in texts:
                yield np.ones(EMBEDDING_DIM, dtype=np.float32)

    monkeypatch.setattr(
        "app.services.embedding_service._load_model_blocking",
        lambda: _Fake(),
    )

    instances = await asyncio.gather(*(get_model() for _ in range(5)))
    assert counter["init"] == 1
    assert all(inst is instances[0] for inst in instances)


async def test_offline_raises_unavailable(monkeypatch):
    def _bomb():
        raise OSError("download failed: no network")

    monkeypatch.setattr(
        "app.services.embedding_service._load_model_blocking",
        _bomb,
    )

    with pytest.raises(EmbeddingUnavailableError) as exc_info:
        await get_model()
    assert "download failed" in str(exc_info.value)
    # Singleton not poisoned
    assert embedding_service._model is None


async def test_weighted_centroid_normalized():
    v1 = [1.0, 0.0, 0.0] + [0.0] * (EMBEDDING_DIM - 3)
    v2 = [0.0, 1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 3)
    centroid = weighted_centroid([v1, v2], [1.0, 0.9])
    arr = np.asarray(centroid)
    assert abs(float(np.linalg.norm(arr)) - 1.0) < 1e-4
    # Le poids 1.0 sur v1 doit dominer
    assert arr[0] > arr[1]
