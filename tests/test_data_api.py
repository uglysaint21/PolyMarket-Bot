from __future__ import annotations

import asyncio

import httpx
import pytest

from polymarket_bot.data_api import DataApiError, PolymarketDataClient


@pytest.mark.asyncio
async def test_cursor_pagination_repeats_filters_and_never_uses_offset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("cursor") == "next-page":
            return httpx.Response(200, json={"data": [{"id": "two"}], "pagination": {"has_more": False, "next_cursor": None}})
        return httpx.Response(200, json={"data": [{"id": "one"}], "pagination": {"has_more": True, "next_cursor": "next-page"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketDataClient(client=http, max_retries=0)
    try:
        rows = [row async for row in client.iter_wallet_trades("0xABC", start=100, end=200, limit=10)]
    finally:
        await client.aclose()
        await http.aclose()

    assert rows == [{"id": "one"}, {"id": "two"}]
    assert len(requests) == 2
    for request in requests:
        assert request.url.params["user"] == "0xabc"
        assert request.url.params["start"] == "100"
        assert request.url.params["end"] == "200"
        assert request.url.params["taker_only"] == "false"
        assert "offset" not in request.url.params


@pytest.mark.asyncio
async def test_malformed_envelope_fails_closed() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    )
    client = PolymarketDataClient(client=http, max_retries=0)
    try:
        with pytest.raises(DataApiError, match="pagination"):
            _ = [row async for row in client.iter_wallet_trades("0xabc", start=100, end=200)]
    finally:
        await client.aclose()
        await http.aclose()


def test_invalid_window_is_rejected_before_request() -> None:
    async def collect() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("network call unexpected")))
        client = PolymarketDataClient(client=http, max_retries=0)
        try:
            with pytest.raises(ValueError, match="start/end"):
                _ = [row async for row in client.iter_wallet_trades("0xabc", start=200, end=100)]
        finally:
            await client.aclose()
            await http.aclose()

    asyncio.run(collect())
