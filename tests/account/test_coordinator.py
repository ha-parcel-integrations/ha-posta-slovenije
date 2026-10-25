"""Tests for the account coordinator: inbox, change-driven detail, degradation."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije.account import coordinator as coordinator_module
from custom_components.posta_slovenije.account.client import (
    PostaSlovenijeAccountAuthError,
)
from custom_components.posta_slovenije.account.coordinator import (
    PostaSlovenijeAccountCoordinator,
)
from custom_components.posta_slovenije.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_SOURCE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    SOURCE_ACCOUNT,
    ParcelStatus,
)
from custom_components.posta_slovenije.transport import PostaSlovenijeApiError
from tests.payloads import (
    delivered_payload,
    delivered_statuses,
    envelope,
    inbox_item,
    sent_item,
)

A, B, C = "CH100000003SI", "CH200000006SI", "CH000000005SI"


def _setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:someone",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_prepare = AsyncMock()
    client.incoming = []
    client.outgoing = []

    def by_direction(direction="incoming"):
        return client.incoming if direction == "incoming" else client.outgoing

    client.by_direction = by_direction
    client.async_get_inbox = AsyncMock(side_effect=by_direction)
    client.async_get_parcel = AsyncMock()
    return client, PostaSlovenijeAccountCoordinator(hass, client, entry)


def _detail(number, n_scans, *, outgoing=False):
    return envelope(number, delivered_statuses()[:n_scans], outgoing=outgoing)


async def test_publishes_incoming_parcels_with_parties(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A), inbox_item(2, B)]
    client.async_get_parcel.side_effect = lambda pid: (
        delivered_payload(A) if pid == 1 else _detail(B, 3)
    )
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [B]
    assert [p["barcode"] for p in coordinator.delivered] == [A]
    assert data[0]["sender"] == "Example Shop"
    assert coordinator.inbox_size == 2
    client.async_prepare.assert_awaited()


async def test_archived_rows_are_not_tracked(hass):
    client, coordinator = _setup(hass)
    client.incoming = [
        inbox_item(1, A),
        inbox_item(2, B, isArchived=True),
    ]
    client.async_get_parcel.return_value = _detail(A, 3)
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [A]
    assert coordinator.inbox_size == 1
    client.async_get_parcel.assert_awaited_once_with(1)


async def test_detail_is_fetched_only_for_changed_parcels(hass):
    client, coordinator = _setup(hass)
    client.incoming = [
        inbox_item(1, A, stamp="t1"),
        inbox_item(2, B, stamp="t1"),
    ]
    client.async_get_parcel.side_effect = lambda pid: _detail(A if pid == 1 else B, 3)
    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 2

    client.async_get_parcel.reset_mock()
    await coordinator._async_update_data()  # nothing moved
    client.async_get_parcel.assert_not_awaited()

    client.incoming = [
        inbox_item(1, A, stamp="t2"),
        inbox_item(2, B, stamp="t1"),
    ]
    await coordinator._async_update_data()
    client.async_get_parcel.assert_awaited_once_with(1)


async def test_a_lagging_summary_does_not_freeze_an_active_parcel(hass, monkeypatch):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A, stamp="t1")]
    client.async_get_parcel.return_value = _detail(A, 3)
    await coordinator._async_update_data()
    client.async_get_parcel.reset_mock()

    later = time.monotonic() + coordinator_module.DETAIL_MAX_AGE_SECONDS + 1
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: later)
    await coordinator._async_update_data()
    client.async_get_parcel.assert_awaited_once()


async def test_a_delivered_parcel_is_never_read_again(hass, monkeypatch):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A, stamp="t1")]
    client.async_get_parcel.return_value = delivered_payload(A)
    await coordinator._async_update_data()
    client.async_get_parcel.reset_mock()
    later = time.monotonic() + 10 * coordinator_module.DETAIL_MAX_AGE_SECONDS
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: later)
    await coordinator._async_update_data()
    client.async_get_parcel.assert_not_awaited()


async def test_a_parcel_that_drops_out_of_the_list_disappears(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A), inbox_item(2, B)]
    client.async_get_parcel.side_effect = lambda pid: _detail(A if pid == 1 else B, 3)
    await coordinator._async_update_data()
    client.incoming = [inbox_item(2, B)]
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [B]
    assert set(coordinator._details) == {"2"}


async def test_a_gone_detail_keeps_last_good_or_waits(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A, stamp="t1")]
    client.async_get_parcel.return_value = _detail(A, 3)
    await coordinator._async_update_data()

    client.incoming = [
        inbox_item(1, A, stamp="t2"),
        inbox_item(2, B),
    ]
    client.async_get_parcel.return_value = None  # 404: parcelId no longer resolves
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [A]  # A keeps last-good, B has nothing yet


async def test_scans_on_the_inbox_row_skip_the_detail_need(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A, statuses=delivered_statuses()[:2])]
    client.async_get_parcel.return_value = None
    data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT


@pytest.mark.parametrize(
    "error",
    [PostaSlovenijeApiError("HTTP 500", status_code=500), aiohttp.ClientError("x")],
)
async def test_detail_failures_degrade_to_last_good(hass, error, caplog):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A, stamp="t1")]
    client.async_get_parcel.return_value = _detail(A, 3)
    await coordinator._async_update_data()
    client.incoming = [inbox_item(1, A, stamp="t2")]
    client.async_get_parcel.side_effect = error
    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert "detail fetch failed" in caplog.text


async def test_unauthorized_inbox_raises_reauth(hass):
    client, coordinator = _setup(hass)
    client.async_get_inbox.side_effect = PostaSlovenijeAccountAuthError("rejected")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_unauthorized_prepare_raises_reauth(hass):
    client, coordinator = _setup(hass)
    client.async_prepare.side_effect = PostaSlovenijeAccountAuthError("rejected")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_unauthorized_detail_raises_reauth(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A)]
    client.async_get_parcel.side_effect = PostaSlovenijeAccountAuthError("rejected")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_inbox_outage_is_update_failed(hass):
    client, coordinator = _setup(hass)
    client.async_get_inbox.side_effect = PostaSlovenijeApiError(
        "HTTP 500", status_code=500
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_429_on_the_inbox_and_on_a_detail_back_off(hass):
    client, coordinator = _setup(hass)
    client.async_get_inbox.side_effect = PostaSlovenijeApiError(
        "HTTP 429", status_code=429, retry_after=45
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 45

    client.async_get_inbox.side_effect = client.by_direction
    client.incoming = [inbox_item(1, A)]
    client.async_get_parcel.side_effect = PostaSlovenijeApiError(
        "HTTP 429", status_code=429
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after


async def test_an_account_never_stops_polling(hass):
    _, coordinator = _setup(hass)
    await coordinator._async_update_data()
    assert coordinator.update_interval is not None
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES


async def test_both_lists_are_read_and_split_by_direction(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A)]
    client.outgoing = [sent_item(2, B), sent_item(3, C)]
    client.async_get_parcel.side_effect = lambda pid: {
        1: _detail(A, 3),
        2: _detail(B, 3, outgoing=True),
        3: delivered_payload(C, outgoing=True),
    }[pid]
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [A]
    assert [p["barcode"] for p in coordinator.outgoing] == [B]
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [C]
    assert coordinator.delivered == []
    assert coordinator.inbox_size == 3
    assert [c.args for c in client.async_get_inbox.await_args_list] == [
        ("incoming",),
        ("outgoing",),
    ]


async def test_the_detail_flag_is_the_authority_over_the_list(hass, caplog):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A), inbox_item(2, B)]
    client.outgoing = [sent_item(3, C)]
    # the detail's flag contradicts the list A and C arrived in
    client.async_get_parcel.side_effect = lambda pid: {
        1: _detail(A, 3, outgoing=True),
        2: _detail(B, 3),
        3: _detail(C, 3),
    }[pid]
    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
        await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [B, C]
    assert [p["barcode"] for p in coordinator.outgoing] == [A]
    warnings = [r for r in caplog.records if "contradicts" in r.getMessage()]
    assert len(warnings) == 1  # one-shot, field name only
    assert "isOutgoingParcel" in warnings[0].getMessage()
    assert "Example Shop" not in warnings[0].getMessage()


async def test_the_row_flag_answers_when_the_detail_has_none(hass):
    client, coordinator = _setup(hass)
    detail = _detail(A, 3)
    del detail["result"]["isOutgoingParcel"]
    client.incoming = [inbox_item(1, A, isOutgoingParcel=True)]
    client.async_get_parcel.return_value = detail
    await coordinator._async_update_data()
    assert [p["barcode"] for p in coordinator.outgoing] == [A]


async def test_neither_place_carrying_the_flag_falls_back_to_the_list(hass, caplog):
    client, coordinator = _setup(hass)
    row = inbox_item(1, A)
    del row["isOutgoingParcel"]
    detail = _detail(A, 3)
    del detail["result"]["isOutgoingParcel"]
    client.outgoing = [row]
    client.async_get_parcel.return_value = detail
    with caplog.at_level(logging.WARNING):
        await coordinator._async_update_data()
        await coordinator._async_update_data()
    assert [p["barcode"] for p in coordinator.outgoing] == [A]
    assert (
        sum(
            "neither a parcel's detail nor its inbox row has" in r.getMessage()
            for r in caplog.records
        )
        == 1
    )


async def test_a_flag_matching_neither_side_counts_as_incoming_with_a_warning(
    hass, caplog
):
    client, coordinator = _setup(hass)
    client.outgoing = [sent_item(1, A)]
    client.async_get_parcel.return_value = _detail(A, 3, outgoing="maybe")
    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
        await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [A]
    assert coordinator.outgoing == []
    unrecognised = [
        r for r in caplog.records if "neither true nor false" in r.getMessage()
    ]
    assert len(unrecognised) == 1
    assert "maybe" not in unrecognised[0].getMessage()


async def test_a_parcel_listed_twice_is_shown_once(hass):
    client, coordinator = _setup(hass)
    client.incoming = [inbox_item(1, A)]
    client.outgoing = [inbox_item(1, A)]
    client.async_get_parcel.return_value = _detail(A, 3)
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == [A]
    assert coordinator.outgoing == []
    client.async_get_parcel.assert_awaited_once()


async def test_archived_outgoing_rows_are_not_tracked(hass):
    client, coordinator = _setup(hass)
    client.outgoing = [sent_item(1, A, isArchived=True), sent_item(2, B)]
    client.async_get_parcel.return_value = _detail(B, 3, outgoing=True)
    await coordinator._async_update_data()
    assert [p["barcode"] for p in coordinator.outgoing] == [B]


async def test_outgoing_parcels_fire_only_the_outgoing_events(hass):
    client, coordinator = _setup(hass)
    seen = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: seen.append(e.event_type))

    client.outgoing = [sent_item(1, A, stamp="t1")]
    client.async_get_parcel.return_value = _detail(A, 2, outgoing=True)
    await coordinator._async_update_data()
    client.outgoing = [sent_item(1, A, stamp="t2"), sent_item(2, B)]
    client.async_get_parcel.side_effect = lambda pid: (
        _detail(A, 3, outgoing=True) if pid == 1 else _detail(B, 2, outgoing=True)
    )
    await coordinator._async_update_data()
    client.outgoing = [sent_item(1, A, stamp="t3"), sent_item(2, B)]
    client.async_get_parcel.side_effect = lambda pid: (
        delivered_payload(A, outgoing=True)
        if pid == 1
        else _detail(B, 2, outgoing=True)
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert seen == [
        f"{DOMAIN}_outgoing_parcel_status_changed",
        f"{DOMAIN}_outgoing_parcel_delivered",
    ]
