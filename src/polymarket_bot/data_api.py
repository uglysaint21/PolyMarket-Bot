"""Small read-only client for Polymarket's public Data API v2 trade feed."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "https://data-api.polymarket.com/v2"
BASE_URL_ENV_VAR = "POLYMARKET_DATA_API_BASE_URL"

# Transient statuses worth retrying. 4xx other than 429 are caller errors and fail immediately.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0


class DataApiError(RuntimeError):
    """An API request failed or the response could not be safely interpreted."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def base_url_from_env(environ: dict[str, str] | None = None) -> str:
    """Return the configured base URL (env override or default); must be https."""
    env = os.environ if environ is None else environ
    return env.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("base_url must be an https URL")
    if parts.username or parts.password:
        raise ValueError("base_url must not embed credentials")
    return base_url.rstrip("/")


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before the next attempt; honours a numeric Retry-After header."""
    backoff = min(2.0**attempt, MAX_BACKOFF_SECONDS)
    if retry_after:
        try:
            requested = float(retry_after)
        except ValueError:  # e.g. an HTTP-date, which we do not parse
            requested = math.nan
        if math.isfinite(requested):
            return min(max(requested, 0.0), MAX_RETRY_AFTER_SECONDS)
    # Small jitter so concurrent processes do not retry in lock-step.
    return backoff + random.uniform(0.0, backoff * 0.1)


class PolymarketDataClient:
    """Read public wallet trades. This client intentionally has no trading methods."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._base_url = _validate_base_url(base_url)
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, headers={"Accept": "application/json"}
        )
        self._owns_client = client is None

    async def __aenter__(self) -> PolymarketDataClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def iter_wallet_trades(
        self,
        wallet: str,
        *,
        start: int,
        end: int,
        limit: int = 100,
        max_pages: int = 10_000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw trade rows using opaque cursor pagination; never use offset.

        Each page's envelope (rows *and* pagination metadata) is validated before any of its
        rows are yielded, so a malformed page never leaks partial rows to the caller.
        """
        wallet = wallet.strip().lower()
        if not wallet:
            raise ValueError("wallet must not be empty")
        if start <= 0 or end <= 0 or start > end:
            raise ValueError("start/end must be positive epoch seconds with start <= end")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        base_params: dict[str, Any] = {
            "user": wallet,
            "start": start,
            "end": end,
            "limit": limit,
            "taker_only": "false",
        }
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            params = dict(base_params)
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._get_json(params)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise DataApiError("response must contain a list at 'data'")
            pagination = payload.get("pagination")
            if not isinstance(pagination, dict):
                raise DataApiError("response must contain 'pagination' metadata")
            has_more = pagination.get("has_more")
            if not isinstance(has_more, bool) or "next_cursor" not in pagination:
                raise DataApiError("pagination must contain boolean has_more and next_cursor")
            next_cursor = pagination["next_cursor"]
            if not has_more:
                if next_cursor is not None:
                    raise DataApiError("next_cursor must be null when has_more is false")
            elif not isinstance(next_cursor, str) or not next_cursor:
                raise DataApiError("next_cursor must be a non-empty string when has_more is true")
            elif next_cursor == cursor or next_cursor in seen_cursors:
                raise DataApiError("pagination cursor repeated; refusing an infinite walk")
            if any(not isinstance(row, dict) for row in rows):
                raise DataApiError("trade row must be a JSON object")

            for row in rows:
                yield row
            if not has_more:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise DataApiError(f"pagination exceeded max_pages={max_pages}; result is incomplete")

    async def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/trades"
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt >= self._max_retries
            try:
                response = await self._client.get(url, params=params, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if last_attempt:
                    raise DataApiError(f"request failed after retries: {exc}") from exc
                await self._sleep(_retry_delay(attempt, None))
                continue
            if response.status_code in RETRYABLE_STATUS and not last_attempt:
                await self._sleep(_retry_delay(attempt, response.headers.get("Retry-After")))
                continue
            if response.is_error:
                raise DataApiError(
                    f"Data API returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise DataApiError("Data API returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise DataApiError("Data API response must be a JSON object")
            return payload
        raise DataApiError("request failed after retries")
