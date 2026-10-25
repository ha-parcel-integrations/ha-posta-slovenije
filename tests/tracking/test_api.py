"""Tests for the guest-lookup client."""

import logging

import aiohttp
import pytest

from custom_components.posta_slovenije.const import USER_AGENT
from custom_components.posta_slovenije.tracking.api import (
    PostaSlovenijeApiClient,
    PostaSlovenijeApiError,
    PostaSlovenijeGuardError,
)
from tests.fakes import FakeResponse, FakeSession
from tests.payloads import (
    DELIVERED_CODE,
    GUARD_PAYLOAD,
    RETURNED_CODE,
    delivered_payload,
    envelope,
    not_found_payload,
)


def _client(*responses):
    session = FakeSession(*responses)
    return PostaSlovenijeApiClient(session), session


async def test_found_parcel_returns_the_whole_reply():
    client, session = _client(FakeResponse(200, delivered_payload()))
    payload = await client.async_get_parcel(DELIVERED_CODE)
    assert payload["result"]["parcel"]["trackingNumber"] == DELIVERED_CODE
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith(f"/parcels/track/?TrackingNumber={DELIVERED_CODE}")
    assert kwargs["headers"] == {"Accept": "application/json", "User-Agent": USER_AGENT}
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False


async def test_the_number_is_url_encoded():
    client, session = _client(FakeResponse(404, b""))
    with pytest.raises(PostaSlovenijeApiError):
        await client.async_get_parcel("A B&C")
    assert session.calls[0][1].endswith("TrackingNumber=A%20B%26C")


async def test_verbatim_not_found_is_none():
    client, _ = _client(FakeResponse(404, not_found_payload(RETURNED_CODE)))
    assert await client.async_get_parcel(RETURNED_CODE) is None


async def test_not_found_naming_another_number_is_an_error():
    client, _ = _client(FakeResponse(404, not_found_payload("XX000000000SI")))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_parcel(RETURNED_CODE)
    assert err.value.status_code == 404


async def test_a_404_without_the_json_means_the_route_moved():
    client, _ = _client(FakeResponse(404, b"<html>nope</html>"))
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_parcel(RETURNED_CODE)
    assert err.value.status_code == 404


async def test_guard_refusal_is_a_guard_error():
    client, _ = _client(FakeResponse(403, GUARD_PAYLOAD))
    with pytest.raises(PostaSlovenijeGuardError):
        await client.async_get_parcel(RETURNED_CODE)


async def test_server_errors_and_429_are_transport_errors():
    client, _ = _client(
        FakeResponse(500, b"boom"),
        FakeResponse(429, b"", headers={"Retry-After": "12"}),
    )
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_parcel(RETURNED_CODE)
    assert err.value.status_code == 500
    with pytest.raises(PostaSlovenijeApiError) as err:
        await client.async_get_parcel(RETURNED_CODE)
    assert err.value.retry_after == 12.0


async def test_html_body_on_200_is_a_schema_error():
    client, _ = _client(FakeResponse(200, b"<html>hello</html>"))
    with pytest.raises(PostaSlovenijeApiError, match="unparseable"):
        await client.async_get_parcel(RETURNED_CODE)


async def test_a_json_list_on_200_is_a_schema_error():
    client, _ = _client(FakeResponse(200, b"[]"))
    with pytest.raises(PostaSlovenijeApiError, match="unparseable"):
        await client.async_get_parcel(RETURNED_CODE)


@pytest.mark.parametrize(
    "payload",
    [
        {"result": None, "errorCode": None},
        {"result": {"statuses": []}, "errorCode": "error.boom"},
        {"result": {"parcel": None, "statuses": []}},
    ],
)
async def test_a_malformed_envelope_is_a_schema_error(payload):
    client, _ = _client(FakeResponse(200, payload))
    with pytest.raises(PostaSlovenijeApiError, match="unexpected envelope"):
        await client.async_get_parcel(RETURNED_CODE)


async def test_a_tracking_number_mismatch_is_inconclusive(caplog):
    client, _ = _client(
        FakeResponse(200, delivered_payload("XX000000000SI")),
        FakeResponse(200, delivered_payload("XX000000000SI")),
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(PostaSlovenijeApiError, match="different parcel"):
                await client.async_get_parcel(DELIVERED_CODE)
    assert (
        sum("different tracking number" in r.getMessage() for r in caplog.records) == 1
    )


async def test_new_keys_are_reported_by_name_only(caplog):
    payload = envelope(DELIVERED_CODE, [], mysteryParcelKey="secret")
    payload["mysteryTopKey"] = 1
    payload["result"]["mysteryResultKey"] = 1
    client, _ = _client(FakeResponse(200, payload))
    with caplog.at_level(logging.WARNING):
        await client.async_get_parcel(DELIVERED_CODE)
    text = caplog.text
    for key in ("mysteryTopKey", "mysteryResultKey", "mysteryParcelKey"):
        assert key in text
    assert "secret" not in text


async def test_network_errors_propagate():
    client, _ = _client(aiohttp.ClientError())
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(RETURNED_CODE)
