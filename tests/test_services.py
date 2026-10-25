"""Tests for the Pošta Slovenije services (track_parcel / untrack_parcel)."""

from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.posta_slovenije.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posta_slovenije.services import async_setup_services

from .payloads import PICKUP_CODE, out_for_delivery_payload

_SAMPLE = out_for_delivery_payload()


async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE},
            blocking=True,
        )
        await hass.async_block_till_done()

    parcels = entry.options[CONF_PARCELS]
    assert parcels == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_track_parcel_normalizes_code(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: " ch2 00000006 si "},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_track_parcel_rejects_a_code_with_a_bad_check_digit(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "CH200000007SI"}, blocking=True
        )


async def test_track_parcel_rejects_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: ""}, blocking=True
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN,
                "track_parcel",
                {CONF_TRACKING_CODE: PICKUP_CODE},
                blocking=True,
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_untrack_parcel_removes_from_options(hass):
    entry = await _setup(
        hass,
        parcels=[{CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}],
    )
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(
        hass,
        parcels=[{CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}],
    )
    with patch(
        "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "CH000000005SI"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_services_ignore_account_entries_and_need_a_tracking_hub(hass):
    account = MockConfigEntry(
        domain=DOMAIN, unique_id="account:someone", data={CONF_SOURCE: SOURCE_ACCOUNT}
    )
    account.add_to_hass(hass)
    hub = await _setup(hass)
    await hass.services.async_call(
        DOMAIN, "track_parcel", {CONF_TRACKING_CODE: PICKUP_CODE}, blocking=True
    )
    assert hub.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]
    assert CONF_PARCELS not in account.options


async def test_track_parcel_without_a_hub_is_rejected(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: PICKUP_CODE}, blocking=True
        )


async def test_track_parcel_records_the_requested_direction(hass):
    entry = await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_track_parcel_again_moves_it_to_the_other_direction(hass):
    entry = await _setup(hass, parcels=[{CONF_TRACKING_CODE: PICKUP_CODE}])
    await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING}
    ]
    # ...and back again, without a duplicate or an error.
    await hass.services.async_call(
        DOMAIN, "track_parcel", {CONF_TRACKING_CODE: PICKUP_CODE}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_track_parcel_rejects_an_unknown_direction(hass):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: "sideways"},
            blocking=True,
        )
