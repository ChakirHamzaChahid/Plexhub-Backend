"""Tests for app/services/ollama_service.py (CR-T03, CR-O04)."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services import ollama_service
from app.utils.metrics import ollama_requests_total


@pytest.mark.asyncio
async def test_ollama_generate_chat_stream_and_health():
    await ollama_service.close_client()
    before_gen_ok = ollama_requests_total.labels(endpoint="generate", result="ok")._value.get()
    before_stream_ok = ollama_requests_total.labels(endpoint="stream_generate", result="ok")._value.get()

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://khoj-ollama:11434/api/generate").mock(
                side_effect=[
                    httpx.Response(200, json={"response": "full text"}),
                    httpx.Response(
                        200,
                        content=b'{"response":"Hel"}\n\n{"response":"lo","done":true}\n',
                    ),
                ]
            )
            mock.post("http://khoj-ollama:11434/api/chat").mock(
                return_value=httpx.Response(200, json={"message": {"content": "reply"}})
            )
            mock.get("http://khoj-ollama:11434/api/tags").mock(
                return_value=httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
            )

            assert await ollama_service.generate("prompt") == "full text"
            assert await ollama_service.chat([{"role": "user", "content": "hi"}]) == "reply"

            chunks = [c async for c in ollama_service.stream_generate("stream")]
            assert chunks == ["Hel", "lo"]

            ok, detail = await ollama_service.is_healthy()
            assert ok is True
            assert detail == "ok"
    finally:
        await ollama_service.close_client()

    assert ollama_requests_total.labels(endpoint="generate", result="ok")._value.get() == before_gen_ok + 1
    assert ollama_requests_total.labels(endpoint="stream_generate", result="ok")._value.get() == before_stream_ok + 1


@pytest.mark.asyncio
async def test_ollama_is_healthy_missing_model_and_error():
    await ollama_service.close_client()
    before_tags_err = ollama_requests_total.labels(endpoint="tags", result="error")._value.get()

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("http://khoj-ollama:11434/api/tags").mock(
                side_effect=[
                    httpx.Response(200, json={"models": [{"name": "other-model"}]}),
                    httpx.ConnectError("connection refused"),
                ]
            )
            ok1, detail1 = await ollama_service.is_healthy()
            assert ok1 is False
            assert "not found" in detail1

            ok2, detail2 = await ollama_service.is_healthy()
            assert ok2 is False
            assert "connection refused" in detail2
    finally:
        await ollama_service.close_client()

    assert ollama_requests_total.labels(endpoint="tags", result="error")._value.get() == before_tags_err + 2
