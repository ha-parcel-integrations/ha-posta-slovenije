"""Tests for the tracking hub coordinator: fetching, caching and degradation."""

import asyncio
from unittest.mock import AsyncMock

import aiohttp
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DIRECTION_OUTGOING,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    ParcelStatus,
)
from custom_components.posta_slovenije.tracking.api import PostaSlovenijeApiError
from custom_components.posta_slovenije.tracking.coordinator import (
    PostaSlovenijeCoordinator,
)
from tests.payloads import (
    DELIVERED_CODE,
    PICKUP_CODE,
    RETURNED_CODE,
    delivered_payload,
    envelope,
    in_transit_payload,
    out_for_delivery_payload,
    returned_payload,
)


def _entry(*codes: str, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            **options,
        },
    )


def _setup(hass, *codes, **options):
    entry = _entry(*codes, **options)
    entry.add_to_hass(hass)
    client = AsyncMock()
    return entry, client, PostaSlovenijeCoordinator(hass, client, entry)


async def test_merges_several_codes_into_active_and_delivered(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE, DELIVERED_CODE)
    client.async_get_parcel.side_effect = lambda code: (
        out_for_delivery_payload(code)
        if code == PICKUP_CODE
        else delivered_payload(code)
    )
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [PICKUP_CODE]
    assert [p["barcode"] for p in coordinator.delivered] == [DELIVERED_CODE]
    assert coordinator.last_success_time is not None
    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES


async def test_history_follows_the_option(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE, **{CONF_INCLUDE_HISTORY: True})
    client.async_get_parcel.return_value = out_for_delivery_payload()
    data = await coordinator._async_update_data()
    assert len(data[0]["history"]) == 3


async def test_not_found_shows_an_unknown_placeholder(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = None
    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == PICKUP_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_aged_out_record_keeps_last_good(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = out_for_delivery_payload()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = None  # not found after a populated fetch
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_empty_scans_on_a_later_fetch_keep_last_good(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = out_for_delivery_payload()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = envelope(PICKUP_CODE, [])
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_empty_scans_on_a_first_fetch_are_registered(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = envelope(PICKUP_CODE, [])
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.REGISTERED
    # ...and once it has scans, they replace the empty reply.
    client.async_get_parcel.return_value = in_transit_payload()
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT


@pytest.mark.parametrize(
    "error", [PostaSlovenijeApiError("HTTP 500"), aiohttp.ClientError("down")]
)
async def test_transport_errors_keep_last_good(hass, error):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = out_for_delivery_payload()
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    client.async_get_parcel.side_effect = error
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.OUT_FOR_DELIVERY
    # A poll served entirely from cache must not look like a success.
    assert coordinator.last_success_time == stamp


async def test_one_failing_code_does_not_hide_the_others(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE, DELIVERED_CODE)

    async def fetch(code):
        if code == PICKUP_CODE:
            raise PostaSlovenijeApiError("HTTP 500")
        return delivered_payload(code)

    client.async_get_parcel.side_effect = fetch
    data = await coordinator._async_update_data()
    assert data == []  # the failing, never-seen code is simply absent
    assert [p["barcode"] for p in coordinator.delivered] == [DELIVERED_CODE]


async def test_raises_when_every_code_fails_without_a_cache(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.side_effect = PostaSlovenijeApiError("HTTP 500")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_unexpected_exceptions_are_not_swallowed(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.side_effect = ValueError("bug")
    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_429_backs_the_whole_poll_off(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE, DELIVERED_CODE)

    async def fetch(code):
        if code == PICKUP_CODE:
            raise PostaSlovenijeApiError("HTTP 429", status_code=429, retry_after=120)
        raise PostaSlovenijeApiError("HTTP 429", status_code=429)

    client.async_get_parcel.side_effect = fetch
    with pytest.raises(UpdateFailed) as excinfo:
        await coordinator._async_update_data()
    assert excinfo.value.retry_after == 120


async def test_429_without_retry_after_uses_exponential_backoff(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.side_effect = PostaSlovenijeApiError(
        "HTTP 429", status_code=429
    )
    with pytest.raises(UpdateFailed) as first:
        await coordinator._async_update_data()
    with pytest.raises(UpdateFailed) as second:
        await coordinator._async_update_data()
    assert second.value.retry_after > first.value.retry_after


async def test_delivered_codes_are_skipped_from_the_next_fetch(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE, DELIVERED_CODE)
    client.async_get_parcel.side_effect = lambda code: (
        out_for_delivery_payload(code)
        if code == PICKUP_CODE
        else delivered_payload(code)
    )
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {DELIVERED_CODE}
    client.async_get_parcel.reset_mock()
    await coordinator._async_update_data()
    client.async_get_parcel.assert_called_once_with(PICKUP_CODE)
    assert [p["barcode"] for p in coordinator.delivered] == [DELIVERED_CODE]


async def test_untracking_forgets_cache_and_skip_set(hass):
    entry, client, coordinator = _setup(hass, DELIVERED_CODE)
    client.async_get_parcel.return_value = delivered_payload()
    await coordinator._async_update_data()
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: []}
    )
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == set()
    assert coordinator._raw_cache == {}
    assert coordinator.update_interval is None  # nothing left to track


async def test_codes_without_a_value_are_never_fetched(hass):
    _, client, coordinator = _setup(hass, "", DELIVERED_CODE)
    client.async_get_parcel.return_value = delivered_payload()
    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1


async def test_lookups_run_a_few_at_a_time(hass):
    codes = [f"CH{n}00000003SI" for n in range(6)]
    _, client, coordinator = _setup(hass, *codes)
    in_flight = peak = 0

    async def fetch(code):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1

    client.async_get_parcel.side_effect = fetch
    await coordinator._async_update_data()
    assert 1 < peak <= 4


async def test_returned_parcel_is_active_and_returning(hass):
    _, client, coordinator = _setup(hass, RETURNED_CODE)
    client.async_get_parcel.return_value = returned_payload()
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.RETURNING
    assert coordinator.delivered == []


def _outgoing(*codes: str) -> list[dict]:
    return [
        {CONF_TRACKING_CODE: code, CONF_DIRECTION: DIRECTION_OUTGOING} for code in codes
    ]


async def test_declared_direction_decides_the_list(hass):
    entry, client, coordinator = _setup(hass, DELIVERED_CODE)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: DELIVERED_CODE},
                *_outgoing(PICKUP_CODE, RETURNED_CODE),
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: {
        DELIVERED_CODE: delivered_payload(code),
        PICKUP_CODE: out_for_delivery_payload(code),
        RETURNED_CODE: delivered_payload(code),
    }[code]
    data = await coordinator._async_update_data()
    assert data == []  # the only incoming parcel is delivered
    assert [p["barcode"] for p in coordinator.delivered] == [DELIVERED_CODE]
    assert [p["barcode"] for p in coordinator.outgoing] == [PICKUP_CODE]
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [RETURNED_CODE]


async def test_a_code_without_a_direction_reads_as_incoming(hass):
    _, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = out_for_delivery_payload()
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [PICKUP_CODE]
    assert coordinator.outgoing == []


async def test_moving_a_code_to_the_other_direction_moves_its_parcel(hass):
    entry, client, coordinator = _setup(hass, PICKUP_CODE)
    client.async_get_parcel.return_value = out_for_delivery_payload()
    await coordinator._async_update_data()
    assert coordinator.outgoing == []

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: _outgoing(PICKUP_CODE)}
    )
    data = await coordinator._async_update_data()
    assert data == []
    assert [p["barcode"] for p in coordinator.outgoing] == [PICKUP_CODE]


async def test_an_outgoing_parcel_counts_for_the_hot_tier_and_delivered_skip(hass):
    entry, client, coordinator = _setup(hass, DELIVERED_CODE)
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_PARCELS: _outgoing(PICKUP_CODE, DELIVERED_CODE)},
    )
    client.async_get_parcel.side_effect = lambda code: (
        out_for_delivery_payload(code)
        if code == PICKUP_CODE
        else delivered_payload(code)
    )
    await coordinator._async_update_data()
    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES
    assert coordinator.delivered_codes == {DELIVERED_CODE}
    client.async_get_parcel.reset_mock()
    await coordinator._async_update_data()
    client.async_get_parcel.assert_called_once_with(PICKUP_CODE)
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [DELIVERED_CODE]
