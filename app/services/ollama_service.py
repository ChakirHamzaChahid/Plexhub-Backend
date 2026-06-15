"""Async client for the Ollama HTTP API (local LLM — gemma4 by default).

Exposes two primitives:
  - generate(prompt)          → full text response (await)
  - stream_generate(prompt)   → async iterator of text chunks (SSE-friendly)

Model and URL are driven by OLLAMA_URL / OLLAMA_MODEL env vars (config.py).
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from app.config import settings

logger = logging.getLogger("plexhub.ollama")

_GENERATE_TIMEOUT = 120.0
_STATUS_TIMEOUT = 5.0


async def generate(prompt: str) -> str:
    """Send a prompt to Ollama /api/generate and return the full response."""
    payload = {"model": settings.OLLAMA_MODEL, "prompt": prompt, "stream": False}
    async with httpx.AsyncClient(timeout=_GENERATE_TIMEOUT) as client:
        r = await client.post(f"{settings.OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return r.json()["response"]


async def chat(messages: list[dict[str, str]]) -> str:
    """Send a messages list to Ollama /api/chat and return the assistant reply."""
    payload = {"model": settings.OLLAMA_MODEL, "messages": messages, "stream": False}
    async with httpx.AsyncClient(timeout=_GENERATE_TIMEOUT) as client:
        r = await client.post(f"{settings.OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]


async def stream_generate(prompt: str) -> AsyncIterator[str]:
    """Streaming version of generate — yields text chunks as they arrive."""
    payload = {"model": settings.OLLAMA_MODEL, "prompt": prompt, "stream": True}
    async with httpx.AsyncClient(timeout=_GENERATE_TIMEOUT) as client:
        async with client.stream(
            "POST", f"{settings.OLLAMA_URL}/api/generate", json=payload
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                text = chunk.get("response", "")
                if text:
                    yield text
                if chunk.get("done"):
                    break


async def is_healthy() -> tuple[bool, str]:
    """Return (ok, detail) — checks that Ollama is up and the model is available."""
    try:
        async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT) as client:
            r = await client.get(f"{settings.OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            ok = settings.OLLAMA_MODEL in models
            detail = "ok" if ok else f"model {settings.OLLAMA_MODEL!r} not found in {models}"
            return ok, detail
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
