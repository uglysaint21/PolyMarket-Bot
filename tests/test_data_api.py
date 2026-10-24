from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from polymarket_bot.data_api import (
    BASE_URL_ENV_VAR,
    DEFAULT_BASE_URL,
    DataApiError,
    PolymarketDataClient,
    _retry_delay,
    base_url_from_env,
)


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


# --- retry / backoff -------------------------------------------------------------------------

EMPTY_PAGE = {"data": [], "pagination": {"has_more": False, "next_cursor": None}}


def _client_with(handler, **kwargs):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketDataClient(client=http, sleep=fake_sleep, **kwargs)
    return client, http, sleeps


async def _drain(client: PolymarketDataClient) -> list[dict]:
    return [row async for row in client.iter_wallet_trades("0xabc", start=100, end=200)]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_statuses_are_retried_then_succeed(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status) if calls == 1 else httpx.Response(200, json=EMPTY_PAGE)

    client, http, sleeps = _client_with(handler, max_retries=2)
    try:
        assert await _drain(client) == []
    finally:
        await http.aclose()
    assert calls == 2
    assert len(sleeps) == 1


async def test_retries_are_bounded_and_report_status_code() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502)

    client, http, sleeps = _client_with(handler, max_retries=3)
    try:
        with pytest.raises(DataApiError) as info:
            await _drain(client)
    finally:
        await http.aclose()
    assert calls == 4  # 1 attempt + 3 retries
    assert len(sleeps) == 3
    assert info.value.status_code == 502


async def test_client_errors_are_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    client, http, _ = _client_with(handler, max_retries=3)
    try:
        with pytest.raises(DataApiError, match="HTTP 400"):
            await _drain(client)
    finally:
        await http.aclose()
    assert calls == 1


async def test_network_errors_are_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=EMPTY_PAGE)

    client, http, _ = _client_with(handler, max_retries=1)
    try:
        assert await _drain(client) == []
    finally:
        await http.aclose()
    assert calls == 2


@pytest.mark.parametrize(
    "header,expected",
    [("7", 7.0), ("0", 0.0), ("-5", 0.0), ("999", 30.0)],
)
def test_retry_after_numeric_is_honoured_and_clamped(header: str, expected: float) -> None:
    assert _retry_delay(0, header) == expected


@pytest.mark.parametrize("header", ["nan", "inf", "-inf", "Wed, 21 Oct 2026 07:28:00 GMT", "", None])
def test_unusable_retry_after_falls_back_to_finite_backoff(header: str | None) -> None:
    delay = _retry_delay(1, header)
    assert math.isfinite(delay)
    assert 2.0 <= delay <= 2.2  # 2**1 plus at most 10% jitter


def test_backoff_is_capped() -> None:
    assert _retry_delay(20, None) <= 8.0 * 1.1


# --- envelope validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "pagination,match",
    [
        ({"has_more": "yes", "next_cursor": None}, "boolean has_more"),
        ({"has_more": False}, "next_cursor"),
        ({"has_more": False, "next_cursor": "x"}, "must be null"),
        ({"has_more": True, "next_cursor": None}, "non-empty string"),
        ({"has_more": True, "next_cursor": ""}, "non-empty string"),
    ],
)
async def test_malformed_pagination_yields_no_rows_from_that_page(pagination: dict, match: str) -> None:
    client, http, _ = _client_with(
        lambda request: httpx.Response(200, json={"data": [{"a": 1}], "pagination": pagination}),
        max_retries=0,
    )
    got: list[dict] = []
    try:
        with pytest.raises(DataApiError, match=match):
            async for row in client.iter_wallet_trades("0xabc", start=100, end=200):
                got.append(row)
    finally:
        await http.aclose()
    assert got == []


async def test_non_object_row_yields_nothing_from_that_page() -> None:
    page = {"data": [{"ok": 1}, "oops"], "pagination": {"has_more": False, "next_cursor": None}}
    client, http, _ = _client_with(lambda request: httpx.Response(200, json=page), max_retries=0)
    got: list[dict] = []
    try:
        with pytest.raises(DataApiError, match="JSON object"):
            async for row in client.iter_wallet_trades("0xabc", start=100, end=200):
                got.append(row)
    finally:
        await http.aclose()
    assert got == []


async def test_repeated_cursor_is_refused() -> None:
    page = {"data": [], "pagination": {"has_more": True, "next_cursor": "same"}}
    client, http, _ = _client_with(lambda request: httpx.Response(200, json=page), max_retries=0)
    try:
        with pytest.raises(DataApiError, match="cursor repeated"):
            await _drain(client)
    finally:
        await http.aclose()


async def test_exceeding_max_pages_is_an_error_not_silent_truncation() -> None:
    counter = iter(range(100))

    def handler(request: httpx.Request) -> httpx.Response:
        page = {"data": [], "pagination": {"has_more": True, "next_cursor": f"c{next(counter)}"}}
        return httpx.Response(200, json=page)

    client, http, _ = _client_with(handler, max_retries=0)
    try:
        with pytest.raises(DataApiError, match="max_pages"):
            _ = [r async for r in client.iter_wallet_trades("0xabc", start=100, end=200, max_pages=3)]
    finally:
        await http.aclose()


async def test_invalid_json_body_is_an_error() -> None:
    client, http, _ = _client_with(lambda request: httpx.Response(200, content=b"not json"), max_retries=0)
    try:
        with pytest.raises(DataApiError, match="invalid JSON"):
            await _drain(client)
    finally:
        await http.aclose()


# --- construction ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://data-api.polymarket.com/v2", "ftp://x.example/v2", "data-api.polymarket.com", "https://u:p@x.example/v2"]
)
def test_base_url_must_be_plain_https(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        PolymarketDataClient(base_url=url)


def test_base_url_env_override_and_default() -> None:
    assert base_url_from_env({}) == DEFAULT_BASE_URL
    assert base_url_from_env({BASE_URL_ENV_VAR: "https://example.test/v2"}) == "https://example.test/v2"
    assert base_url_from_env({BASE_URL_ENV_VAR: ""}) == DEFAULT_BASE_URL


async def test_async_context_manager_closes_owned_client() -> None:
    async with PolymarketDataClient() as client:
        inner = client._client
        assert not inner.is_closed
    assert inner.is_closed
