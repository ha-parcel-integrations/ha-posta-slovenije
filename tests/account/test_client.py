"""Tests for the account client: paging, the 401 ladder and error handling."""

import logging
import time

import aiohttp
import pytest

from custom_components.posta_slovenije.account.auth import PostaSlovenijeTokens
from custom_components.posta_slovenije.account.client import (
    PostaSlovenijeAccountAuthError,
    PostaSlovenijeAccountClient,
)
from custom_components.posta_slovenije.const import PARCELS_URL, USER_AGENT
from custom_components.posta_slovenije.transport import (
    PostaSlovenijeApiError,
    PostaSlovenijeGuardError,
)
from tests.fakes import FakeResponse, FakeSession
from tests.payloads import (
    GUARD_PAYLOAD,
    delivered_payload,
    inbox_item,
    inbox_page,
    token_response,
)

UNAUTHORIZED = {
    "result": None,
    "errorCode": "error.unauthorized",
    "errorMessage": "User is not authorized.",
}
BAD_PASSWORD = {
    "error": "invalid_grant",
    "error_description": "Invalid user credentials",
}
FRESH = PostaSlovenijeTokens("fresh", "fresh-refresh", time.time() + 3600)
STALE = PostaSlovenijeTokens("stale", "stale-refresh", time.time() - 10)


def _client(*responses, tokens=FRESH, on_tokens=None):
    session = FakeSession(*responses)
    client = PostaSlovenijeAccountClient(
        session,
        username="someone",
        password="hunter2",
        tokens=tokens,
        on_tokens=on_tokens,
    )
    return client, session


def _auth_of(call):
    return call[2]["headers"]["Authorization"]


async def test_inbox_is_paged_until_total_count_is_reached():
    page1 = [inbox_item(i, f"CH{i}00000003SI") for i in range(1, 3)]
    page2 = [inbox_item(3, "CH300000003SI")]
    client, session = _client(
        FakeResponse(200, inbox_page(page1, 3)), FakeResponse(200, inbox_page(page2, 3))
    )
    rows = await client.async_get_inbox()
    assert [r["parcelId"] for r in rows] == [1, 2, 3]
    assert len(session.calls) == 2
    first, second = (c[2]["params"] for c in session.calls)
    assert first == {"page": 1, "pageSize": 10, "incoming": "true"}
    assert second["page"] == 2  # pages are 1-based
    assert session.calls[0][1] == PARCELS_URL
    headers = session.calls[0][2]["headers"]
    assert headers["Authorization"] == "Bearer fresh"
    assert headers["User-Agent"] == USER_AGENT
    assert session.calls[0][2]["allow_redirects"] is False


async def test_empty_inbox_is_complete():
    client, _ = _client(FakeResponse(200, inbox_page([], 0)))
    assert await client.async_get_inbox() == []


async def test_duplicate_and_odd_rows_are_skipped(caplog):
    items = [
        inbox_item(1, "CH100000003SI"),
        inbox_item(1, "CH100000003SI"),
        "junk",
        {"parcelId": 9},
    ]
    # the repeat collapses into one row, so the service's count is the one
    # parcel plus the two rows we could not identify
    client, _ = _client(FakeResponse(200, inbox_page(items, 3)))
    with caplog.at_level(logging.WARNING):
        rows = await client.async_get_inbox()
    assert [r["parcelId"] for r in rows] == [1]
    assert "no parcelId or trackingNumber" in caplog.text


async def test_a_repeated_page_does_not_satisfy_the_total():
    # Two rows are promised and the service hands out the same page twice: a
    # count of rows fetched would call that complete and drop a parcel.
    page = inbox_page([inbox_item(1, "CH100000003SI")], 2, total_pages=2)
    client, _ = _client(FakeResponse(200, page), FakeResponse(200, page))
    with pytest.raises(PostaSlovenijeApiError, match="ended before"):
        await client.async_get_inbox()


async def test_a_page_answered_out_of_order_is_reported(caplog):
    page1 = inbox_page([inbox_item(1, "CH100000003SI")], 2, total_pages=2)
    page2 = inbox_page([inbox_item(2, "CH200000006SI")], 2, page=99, total_pages=2)
    client, _ = _client(FakeResponse(200, page1), FakeResponse(200, page2))
    with caplog.at_level(logging.WARNING):
        rows = await client.async_get_inbox()
    assert [r["parcelId"] for r in rows] == [1, 2]
    assert "answered page 99" in caplog.text


async def test_an_inbox_without_a_page_count_still_walks_to_the_total():
    page1 = inbox_page([inbox_item(1, "CH100000003SI")], 2)
    del page1["result"]["totalPages"]
    page2 = inbox_page([inbox_item(2, "CH200000006SI")], 2)
    del page2["result"]["totalPages"]
    client, _ = _client(FakeResponse(200, page1), FakeResponse(200, page2))
    rows = await client.async_get_inbox()
    assert [r["parcelId"] for r in rows] == [1, 2]


async def test_inbox_ending_early_fails_rather_than_returning_a_partial_list():
    client, _ = _client(
        FakeResponse(200, inbox_page([inbox_item(1, "CH100000003SI")], 5)),
        FakeResponse(200, inbox_page([], 5)),
    )
    with pytest.raises(PostaSlovenijeApiError, match="ended before"):
        await client.async_get_inbox()


async def test_runaway_inbox_is_a_fault(monkeypatch):
    monkeypatch.setattr(
        "custom_components.posta_slovenije.account.client.MAX_INBOX_PAGES", 2
    )
    page = inbox_page([inbox_item(1, "CH100000003SI")], 99)
    client, _ = _client(FakeResponse(200, page), FakeResponse(200, page))
    with pytest.raises(PostaSlovenijeApiError, match="too many pages"):
        await client.async_get_inbox()


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"items": "x", "totalCount": 1}},
        {"result": {"items": [], "totalCount": "1"}},
        {"result": None},
        {"result": {"items": [], "totalCount": 0}, "errorCode": "error.boom"},
        [],
    ],
)
async def test_unexpected_inbox_shapes_are_errors(payload):
    client, _ = _client(FakeResponse(200, payload))
    with pytest.raises(PostaSlovenijeApiError):
        await client.async_get_inbox()


async def test_non_200_inbox_is_an_error():
    client, _ = _client(FakeResponse(500, b""))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_inbox()
    assert err.value.status_code == 500


async def test_new_inbox_keys_are_reported_by_name(caplog):
    page = inbox_page([inbox_item(1, "CH100000003SI", surpriseKey="v")], 1)
    page["result"]["surpriseListKey"] = 1
    client, _ = _client(FakeResponse(200, page))
    with caplog.at_level(logging.WARNING):
        await client.async_get_inbox()
    assert "surpriseKey" in caplog.text and "surpriseListKey" in caplog.text


async def test_detail_is_fetched_by_parcel_id():
    client, session = _client(FakeResponse(200, delivered_payload()))
    detail = await client.async_get_parcel(1000001)
    assert detail["result"]["statuses"]
    assert session.calls[0][1] == f"{PARCELS_URL}/1000001"


async def test_detail_404_means_the_parcel_is_gone():
    client, _ = _client(FakeResponse(404, {"errorCode": "error.not.found"}))
    assert await client.async_get_parcel(5) is None


async def test_detail_without_a_parcel_object_is_accepted():
    payload = delivered_payload()
    del payload["result"]["parcel"]
    client, _ = _client(FakeResponse(200, payload))
    assert (await client.async_get_parcel(5))["result"]["statuses"]


async def test_detail_with_an_error_envelope_is_an_error():
    client, _ = _client(FakeResponse(200, {"result": None, "errorCode": "error.x"}))
    with pytest.raises(PostaSlovenijeApiError):
        await client.async_get_parcel(5)


# -- the 401 ladder ---------------------------------------------------------


async def test_401_refreshes_then_succeeds():
    seen = []
    client, session = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(200, token_response("access-2", "refresh-2")),
        FakeResponse(200, inbox_page([], 0)),
        on_tokens=seen.append,
    )
    assert await client.async_get_inbox() == []
    assert [c[1] for c in session.calls][1].endswith("/token")
    assert session.calls[1][2]["data"]["grant_type"] == "refresh_token"
    assert _auth_of(session.calls[2]) == "Bearer access-2"
    assert seen[-1].access_token == "access-2"


async def test_401_with_a_rejected_refresh_falls_back_to_the_password():
    client, session = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(
            400, {"error": "invalid_grant", "error_description": "Token is not active"}
        ),
        FakeResponse(200, token_response("access-3")),
        FakeResponse(200, inbox_page([], 0)),
    )
    assert await client.async_get_inbox() == []
    assert session.calls[2][2]["data"]["grant_type"] == "password"
    assert _auth_of(session.calls[3]) == "Bearer access-3"


async def test_401_persisting_signs_in_once_then_raises_reauth_once():
    client, session = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(200, token_response("access-2")),  # refresh
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(200, token_response("access-3")),  # password grant
        FakeResponse(401, UNAUTHORIZED),
    )
    with pytest.raises(PostaSlovenijeAccountAuthError):
        await client.async_get_inbox()
    assert not session.responses  # exactly these five requests, no loop
    grants = [c[2]["data"]["grant_type"] for c in session.calls if c[0] == "POST"]
    assert grants == ["refresh_token", "password"]


async def test_401_after_a_password_fallback_raises_reauth_straight_away():
    client, session = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(400, {"error": "invalid_grant"}),
        FakeResponse(200, token_response("access-3")),
        FakeResponse(401, UNAUTHORIZED),
    )
    with pytest.raises(PostaSlovenijeAccountAuthError):
        await client.async_get_inbox()
    assert not session.responses


async def test_401_without_a_refresh_token_goes_to_the_password():
    tokens = PostaSlovenijeTokens("fresh", None, time.time() + 3600)
    client, session = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(200, token_response("access-2", None)),
        FakeResponse(200, inbox_page([], 0)),
        tokens=tokens,
    )
    await client.async_get_inbox()
    assert session.calls[1][2]["data"]["grant_type"] == "password"


async def test_rejected_password_is_reauth():
    client, _ = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(400, {"error": "invalid_grant"}),
        FakeResponse(401, BAD_PASSWORD),
    )
    with pytest.raises(PostaSlovenijeAccountAuthError):
        await client.async_get_inbox()


async def test_unauthorized_client_is_a_transport_error_not_reauth():
    body = {"error": "unauthorized_client"}
    client, _ = _client(FakeResponse(401, UNAUTHORIZED), FakeResponse(400, body))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_inbox()
    assert not isinstance(err.value, PostaSlovenijeAccountAuthError)


async def test_password_outage_during_renewal_is_a_transport_error():
    tokens = PostaSlovenijeTokens("fresh", None, time.time() + 3600)
    client, _ = _client(
        FakeResponse(401, UNAUTHORIZED), FakeResponse(503, b""), tokens=tokens
    )
    with pytest.raises(PostaSlovenijeApiError, match="sign-in failed"):
        await client.async_get_inbox()


async def test_refresh_outage_is_a_transport_error_not_reauth():
    client, _ = _client(FakeResponse(401, UNAUTHORIZED), FakeResponse(503, b""))
    with pytest.raises(PostaSlovenijeApiError, match="refresh failed") as err:
        await client.async_get_inbox()
    assert not isinstance(err.value, PostaSlovenijeAccountAuthError)


# -- tokens -----------------------------------------------------------------


async def test_prepare_keeps_a_valid_token():
    client, session = _client()
    await client.async_prepare()
    assert session.calls == []


async def test_prepare_refreshes_an_expiring_token():
    client, session = _client(
        FakeResponse(200, token_response("access-2")), tokens=STALE
    )
    await client.async_prepare()
    assert session.calls[0][2]["data"]["grant_type"] == "refresh_token"


async def test_no_token_signs_in_before_the_first_request():
    client, session = _client(
        FakeResponse(200, token_response("access-9")),
        FakeResponse(200, inbox_page([], 0)),
        tokens=None,
    )
    await client.async_get_inbox()
    assert session.calls[0][2]["data"]["grant_type"] == "password"
    assert _auth_of(session.calls[1]) == "Bearer access-9"


async def test_sign_in_for_the_config_flow():
    seen = []
    client, _ = _client(
        FakeResponse(200, token_response()), tokens=None, on_tokens=seen.append
    )
    await client.async_sign_in()
    assert seen[0].access_token == "access-1"


async def test_sign_in_with_a_bad_password_is_a_reauth_error():
    client, _ = _client(FakeResponse(401, BAD_PASSWORD), tokens=None)
    with pytest.raises(PostaSlovenijeAccountAuthError):
        await client.async_sign_in()


async def test_one_poll_uses_one_token():
    client, session = _client(
        FakeResponse(200, inbox_page([inbox_item(1, "CH100000003SI")], 1)),
        FakeResponse(200, delivered_payload()),
    )
    await client.async_prepare()
    await client.async_get_inbox()
    await client.async_get_parcel(1)
    assert {_auth_of(c) for c in session.calls} == {"Bearer fresh"}


# -- transport errors -------------------------------------------------------


async def test_429_guard_and_network_errors_surface():
    client, _ = _client(
        FakeResponse(429, b"", headers={"Retry-After": "7"}),
        FakeResponse(403, GUARD_PAYLOAD),
        aiohttp.ClientError(),
    )
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_inbox()
    assert err.value.retry_after == 7.0
    with pytest.raises(PostaSlovenijeGuardError):
        await client.async_get_inbox()
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_inbox()


async def test_tokens_never_reach_the_log(caplog):
    client, _ = _client(
        FakeResponse(401, UNAUTHORIZED),
        FakeResponse(200, token_response("access-2", "refresh-2")),
        FakeResponse(200, inbox_page([], 0)),
    )
    with caplog.at_level(logging.DEBUG):
        await client.async_get_inbox()
    for secret in ("fresh", "access-2", "refresh-2", "hunter2", "someone"):
        assert secret not in caplog.text


async def test_the_outgoing_list_is_its_own_route_parameter():
    client, session = _client(
        FakeResponse(200, inbox_page([inbox_item(4, "CH200000006SI")], 1))
    )
    rows = await client.async_get_inbox("outgoing")
    assert [r["parcelId"] for r in rows] == [4]
    assert session.calls[0][2]["params"] == {
        "page": 1,
        "pageSize": 10,
        "outgoing": "true",
    }
