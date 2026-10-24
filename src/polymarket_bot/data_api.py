"""Small read-only client for Polymarket's public Data API v2 trade feed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class DataApiError(RuntimeError):
    """An API request failed or the response could not be safely interpreted."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PolymarketDataClient:
    """Read public wallet trades. This client intentionally has no trading methods."""

    def __init__(
        self,
        *,
        base_url: str = "https://data-api.polymarket.com/v2",
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, headers={"Accept": "application/json"}
        )
        self._owns_client = client is None

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
        """Yield raw trade rows using opaque cursor pagination; never use offset."""
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
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise DataApiError("response must contain a list at 'data'")
            pagination = payload.get("pagination")
            if not isinstance(pagination, dict):
                raise DataApiError("response must contain 'pagination' metadata")
            for row in payload["data"]:
                if not isinstance(row, dict):
                    raise DataApiError("trade row must be a JSON object")
                yield row
            has_more = pagination.get("has_more")
            next_cursor = pagination.get("next_cursor", object())
            if not isinstance(has_more, bool) or next_cursor.__class__ is object:
                raise DataApiError("pagination must contain boolean has_more and next_cursor")
            if not has_more:
                if next_cursor is not None:
                    raise DataApiError("next_cursor must be null when has_more is false")
                return
            if not isinstance(next_cursor, str) or not next_cursor:
                raise DataApiError("next_cursor must be a non-empty string when has_more is true")
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise DataApiError("pagination cursor repeated; refusing an infinite walk")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise DataApiError(f"pagination exceeded max_pages={max_pages}; result is incomplete")

    async def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/trades"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(url, params=params, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise DataApiError(f"request failed after retries: {exc}") from exc
                await asyncio.sleep(min(2**attempt, 8))
                continue
            if response.status_code in {429, 503} and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(max(float(retry_after), 0.0), 30.0) if retry_after else min(2**attempt, 8)
                except ValueError:
                    delay = min(2**attempt, 8)
                await asyncio.sleep(delay)
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
