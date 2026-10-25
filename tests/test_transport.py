"""Tests for the shared HTTP helpers."""

import logging

import aiohttp
import pytest

from custom_components.posta_slovenije import transport
from custom_components.posta_slovenije.transport import (
    BodyTooLargeError,
    PostaSlovenijeApiError,
    PostaSlovenijeGuardError,
    error_code,
    get_json,
    is_block_page,
    parse_json,
    parse_retry_after,
    read_capped,
)

from .fakes import FakeResponse, FakeSession
from .payloads import GUARD_PAYLOAD

HEADERS = {"Accept": "application/json"}


def test_parse_retry_after():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_parse_json_and_error_code():
    assert parse_json(b'{"errorCode": "x"}') == {"errorCode": "x"}
    assert parse_json(b"<html>") is None
    assert error_code({"errorCode": "x"}) == "x"
    assert error_code({"errorCode": None}) is None
    assert error_code([]) is None


@pytest.mark.parametrize(
    "body",
    [
        b"<title>Just a moment...</title>",
        b"Access Denied",
        b"Attention Required! | Cloudflare",
        b"cf-turnstile",
        b"window._cf_chl_opt = {}",
        b"Azure Front Door",
        b"Request blocked",
    ],
)
def test_block_pages_are_recognised(body):
    assert is_block_page(body)


def test_ordinary_html_is_not_a_block_page():
    assert not is_block_page(b"<html><body>hello</body></html>")


async def test_read_capped_refuses_an_oversized_body(monkeypatch):
    monkeypatch.setattr(transport, "MAX_BODY_BYTES", 10)
    with pytest.raises(BodyTooLargeError):
        await read_capped(FakeResponse(200, b"x" * 50))
    assert await read_capped(FakeResponse(200, b"x" * 5)) == b"xxxxx"


async def test_get_json_returns_status_and_payload():
    session = FakeSession(FakeResponse(200, {"a": 1}))
    status, payload = await get_json(
        session, "https://x.test/", headers=HEADERS, params={"p": 1}
    )
    assert (status, payload) == (200, {"a": 1})
    kwargs = session.calls[0][2]
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"] == {"p": 1}


async def test_429_carries_retry_after():
    session = FakeSession(FakeResponse(429, b"", headers={"Retry-After": "30"}))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await get_json(session, "https://x.test/", headers=HEADERS)
    assert err.value.status_code == 429
    assert err.value.retry_after == 30.0


async def test_redirects_are_not_followed():
    session = FakeSession(FakeResponse(302, b""))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await get_json(session, "https://x.test/", headers=HEADERS)
    assert err.value.status_code == 302


async def test_oversized_body_is_an_error(monkeypatch):
    monkeypatch.setattr(transport, "MAX_BODY_BYTES", 10)
    with pytest.raises(PostaSlovenijeApiError, match="too large"):
        await get_json(
            FakeSession(FakeResponse(200, b"x" * 50)),
            "https://x.test/",
            headers=HEADERS,
        )


async def test_timeout_is_an_error():
    with pytest.raises(PostaSlovenijeApiError, match="timeout"):
        await get_json(FakeSession(TimeoutError()), "https://x.test/", headers=HEADERS)


async def test_client_errors_propagate():
    with pytest.raises(aiohttp.ClientError):
        await get_json(
            FakeSession(aiohttp.ClientError()), "https://x.test/", headers=HEADERS
        )


async def test_block_page_is_an_error_with_one_warning(caplog):
    session = FakeSession(
        FakeResponse(403, b"<title>Just a moment...</title>"),
        FakeResponse(403, b"<title>Just a moment...</title>"),
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(PostaSlovenijeApiError, match="edge page"):
                await get_json(session, "https://x.test/", headers=HEADERS)
    assert sum("edge block page" in r.getMessage() for r in caplog.records) == 1


async def test_guard_refusal_is_a_guard_error_with_one_warning(caplog):
    session = FakeSession(
        FakeResponse(403, GUARD_PAYLOAD), FakeResponse(403, GUARD_PAYLOAD)
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(PostaSlovenijeGuardError):
                await get_json(session, "https://x.test/", headers=HEADERS)
    assert sum("refused this client" in r.getMessage() for r in caplog.records) == 1


async def test_other_statuses_come_back_to_the_caller():
    status, payload = await get_json(
        FakeSession(FakeResponse(500, b"oops")), "https://x.test/", headers=HEADERS
    )
    assert (status, payload) == (500, None)
