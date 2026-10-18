"""Typed provider errors for the scrape gateway (B1-08, ADR 0009 D2.6).

`ProviderError` is the ONLY exception the strict TMDB/OMDb access methods
(`TMDBService.fetch_*`/`find_external`/`search_scored`/`_request_bounded`,
`OMDbService.fetch_ratings`) let escape. Raw httpx exceptions embed the full
request URL (`api_key=` / `apikey=` query param), so they are translated at
the source and the chain is cut (`raise ... from None`, raised OUTSIDE the
`except` block so `__context__` is empty too).

`__str__` carries the provider, the kind and the optional HTTP status only —
never a URL, a key, or any upstream text (pitfall 21c).

OMDb budget exhaustion is a plain `kind="budget_exhausted"` (no subclass):
one exception type keeps the gateway's single translation table simple.
"""
from __future__ import annotations

from typing import Literal

ProviderErrorKind = Literal[
    "not_configured", "not_found", "rate_limited", "unavailable",
    "bad_response", "timeout", "budget_exhausted",
]

PROVIDER_ERROR_KINDS: frozenset[str] = frozenset(
    ("not_configured", "not_found", "rate_limited", "unavailable",
     "bad_response", "timeout", "budget_exhausted")
)
PROVIDERS: frozenset[str] = frozenset(("tmdb", "omdb"))


class ProviderError(Exception):
    def __init__(
        self,
        kind: str,
        *,
        provider: str,
        retry_after: int | None = None,
        status_code: int | None = None,
    ) -> None:
        if kind not in PROVIDER_ERROR_KINDS:
            raise ValueError(f"unknown ProviderError kind: {kind!r}")
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {provider!r}")
        self.kind = kind
        self.provider = provider
        self.retry_after = retry_after
        self.status_code = status_code
        super().__init__(kind, provider, retry_after, status_code)

    def __str__(self) -> str:
        base = f"{self.provider} {self.kind}"
        if self.status_code is not None:
            base += f" HTTP {self.status_code}"
        return base

    def __repr__(self) -> str:
        return (
            f"ProviderError(kind={self.kind!r}, provider={self.provider!r}, "
            f"retry_after={self.retry_after!r}, status_code={self.status_code!r})"
        )
