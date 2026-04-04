"""
utils/http.py
──────────────
Retry-aware httpx async client factory.

Provides a pre-configured httpx.AsyncClient with:
  • Exponential back-off retries (3 attempts by default).
  • Sensible default timeouts.
  • Optional Bearer token injection.
  • Request/response logging at DEBUG level.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("washfix.utils.http")


class RetryTransport(httpx.AsyncBaseTransport):
    """
    HTTPX transport that retries on transient errors.
    Retries on 429, 500, 502, 503, 504 and network errors.
    """

    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        self._wrapped       = wrapped
        self._max_retries   = max_retries
        self._backoff_factor = backoff_factor

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._wrapped.handle_async_request(request)
                if response.status_code not in self.RETRYABLE or attempt == self._max_retries:
                    return response
                wait = self._backoff_factor * (2 ** attempt)
                logger.debug(
                    f"Retry {attempt+1}/{self._max_retries} for "
                    f"{request.method} {request.url} (HTTP {response.status_code}) "
                    f"after {wait:.1f}s"
                )
                await asyncio.sleep(wait)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise
                wait = self._backoff_factor * (2 ** attempt)
                logger.debug(f"Network error on attempt {attempt+1}: {exc}. Retrying in {wait:.1f}s")
                await asyncio.sleep(wait)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unreachable")

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def make_client(
    bearer_token: Optional[str] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> httpx.AsyncClient:
    """
    Create a configured httpx.AsyncClient.

    Example::
        async with make_client(bearer_token=token) as client:
            resp = await client.get("https://api.example.com/data")
    """
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    transport = RetryTransport(
        wrapped        = httpx.AsyncHTTPTransport(),
        max_retries    = max_retries,
        backoff_factor = backoff_factor,
    )
    return httpx.AsyncClient(
        headers   = headers,
        timeout   = httpx.Timeout(timeout),
        transport = transport,
    )
