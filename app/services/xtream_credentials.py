"""Shared lightweight credential holder for Xtream authentication.

`xtream_service.authenticate` is duck-typed on ``.base_url``/``.port``/
``.username``/``.password``. Both the account-create endpoint and the env
auto-provision path used to declare their own throwaway anonymous class for
this (CR-C10) — this dataclass is the single shared source so they stop
diverging.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class XtreamCredentials:
    """Minimal duck-typed credentials for ``xtream_service.authenticate``."""

    base_url: str
    username: str
    password: str
    port: int = 80
