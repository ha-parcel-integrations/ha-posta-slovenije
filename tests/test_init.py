"""Tests for Pošta Slovenije setup, unload and the two source kinds."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije import async_unload_entry
from custom_components.posta_slovenije.account.auth import PostaSlovenijeTokens
from custom_components.posta_slovenije.account.client import (
    PostaSlovenijeAccountAuthError,
)
from custom_components.posta_slovenije.const import (
    CONF_ACCESS_TOKEN,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TRACKING_CODE,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posta_slovenije.transport import PostaSlovenijeApiError

from .payloads import (
    DELIVERED_CODE,
    PICKUP_CODE,
    delivered_payload,
    inbox_item,
    out_for_delivery_payload,
    sent_item,
)

TRACK = "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient"
ACCOUNT = "custom_components.posta_slovenije.account.client.PostaSlovenijeAccountClient"


def _hub(codes=(PICKUP_CODE,), **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data=data,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


def _account(**data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:someone",
        title="Pošta Slovenije (someone)",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: "someone",
            CONF_PASSWORD: "hunter2",
            CONF_ACCESS_TOKEN: "access",
            CONF_REFRESH_TOKEN: "refresh",
            CONF_TOKEN_EXPIRES_AT: 4102444800.0,
            **data,
        },
    )


async def test_tracking_setup_and_unload(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    with patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(return_value=out_for_delivery_payload()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.posta_slovenije_incoming_parcels").state == "1"
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_an_entry_without_a_source_is_a_tracking_hub(hass):
    """Entries are tracking hubs unless they say otherwise."""
    entry = _hub()
    assert CONF_SOURCE not in entry.data
    entry.add_to_hass(hass)
    with patch(f"{TRACK}.async_get_parcel", new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_tracking_setup_retries_when_the_first_refresh_fails(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    with patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(side_effect=PostaSlovenijeApiError("down")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    mock = AsyncMock(return_value=out_for_delivery_payload())
    with patch(f"{TRACK}.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{PICKUP_CODE}"
        )
        hass.config_entries.async_update_entry(
            entry, options={CONF_PARCELS: [{CONF_TRACKING_CODE: DELIVERED_CODE}]}
        )
        mock.return_value = out_for_delivery_payload(DELIVERED_CODE)
        await hass.async_block_till_done()
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{DELIVERED_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{PICKUP_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    with patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: out_for_delivery_payload(code)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: PICKUP_CODE},
                    {CONF_TRACKING_CODE: DELIVERED_CODE},
                ]
            },
        )
        await hass.async_block_till_done()
    assert hass.states.get("sensor.posta_slovenije_incoming_parcels").state == "2"


async def test_account_setup_reads_the_inbox_and_persists_renewed_tokens(hass):
    entry = _account()
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(
            f"{ACCOUNT}.async_get_inbox",
            new=AsyncMock(
                return_value=[inbox_item(1, PICKUP_CODE), inbox_item(2, DELIVERED_CODE)]
            ),
        ),
        patch(
            f"{ACCOUNT}.async_get_parcel",
            new=AsyncMock(
                side_effect=lambda pid: (
                    out_for_delivery_payload(PICKUP_CODE)
                    if pid == 1
                    else delivered_payload()
                )
            ),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert (
            hass.states.get("sensor.posta_slovenije_someone_incoming_parcels").state
            == "1"
        )
        # no tracking services for an account-only install
        assert not hass.services.has_service(DOMAIN, "track_parcel")

        # the client persists a renewed token pair back into the entry
        entry.runtime_data.client._on_tokens(
            PostaSlovenijeTokens("new-a", "new-r", 5.0)
        )
        assert entry.data[CONF_ACCESS_TOKEN] == "new-a"
        assert entry.data[CONF_REFRESH_TOKEN] == "new-r"
        assert entry.data[CONF_USERNAME] == "someone"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_account_without_stored_tokens_or_refresh_token_still_sets_up(hass):
    entry = _account()
    data = dict(entry.data)
    data.pop(CONF_ACCESS_TOKEN)
    data.pop(CONF_REFRESH_TOKEN)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="account:someone", data=data)
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(f"{ACCOUNT}.async_get_inbox", new=AsyncMock(return_value=[])),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client._tokens is None


async def test_account_keeps_a_token_without_a_refresh_token(hass):
    entry = _account(**{CONF_REFRESH_TOKEN: None})
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(f"{ACCOUNT}.async_get_inbox", new=AsyncMock(return_value=[])),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.client._tokens.refresh_token is None


async def test_a_rejected_password_starts_reauth(hass):
    entry = _account()
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(
            f"{ACCOUNT}.async_get_inbox",
            new=AsyncMock(side_effect=PostaSlovenijeAccountAuthError("rejected")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_services_survive_while_another_tracking_hub_is_loaded(hass):
    """Services leave with the last loaded tracking hub, not before."""
    first = _hub()
    first.add_to_hass(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other-hub",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: []},
    )
    second.add_to_hass(hass)
    with patch(f"{TRACK}.async_get_parcel", new=AsyncMock(return_value=None)):
        # Setting up one entry of a domain sets up all of them.
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(first.entry_id)
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert await hass.config_entries.async_unload(second.entry_id)
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_stale_parcel_sensors_are_swept_on_setup(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_CH900000008SI", config_entry=entry
    )
    summary = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_incoming_parcels", config_entry=entry
    )
    with patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(return_value=out_for_delivery_payload()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get(summary.entity_id) is not None


async def test_a_failed_platform_unload_keeps_the_services(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    with patch(f"{TRACK}.async_get_parcel", new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert await async_unload_entry(hass, entry) is False
    assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_tracking_hub_publishes_outgoing_entities(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            # keep the old fixture date inside the retention window
            "delivered_filter_type": "parcels",
            "delivered_filter_amount": 10,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
                {
                    CONF_TRACKING_CODE: DELIVERED_CODE,
                    CONF_DIRECTION: DIRECTION_OUTGOING,
                },
            ],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(
            side_effect=lambda code: (
                out_for_delivery_payload(code)
                if code == PICKUP_CODE
                else delivered_payload(code)
            )
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.posta_slovenije_incoming_parcels").state == "0"
    assert hass.states.get("sensor.posta_slovenije_outgoing_parcels").state == "1"
    assert (
        hass.states.get("sensor.posta_slovenije_outgoing_delivered_parcels").state
        == "1"
    )
    # incoming-only views never see a parcel the user sent
    assert hass.states.get("sensor.posta_slovenije_awaiting_pickup").state == "0"
    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{PICKUP_CODE}"
    )


async def test_account_publishes_outgoing_entities_and_keeps_them_in_the_sweep(hass):
    entry = _account()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    summary = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_outgoing_delivered_parcels",
        config_entry=entry,
    )
    with (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(
            f"{ACCOUNT}.async_get_inbox",
            new=AsyncMock(
                side_effect=lambda direction: (
                    [inbox_item(1, DELIVERED_CODE)]
                    if direction == "incoming"
                    else [sent_item(2, PICKUP_CODE)]
                )
            ),
        ),
        patch(
            f"{ACCOUNT}.async_get_parcel",
            new=AsyncMock(
                side_effect=lambda pid: out_for_delivery_payload(
                    DELIVERED_CODE if pid == 1 else PICKUP_CODE,
                    outgoing=pid != 1,
                )
            ),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.posta_slovenije_someone_incoming_parcels").state == "1"
    )
    assert (
        hass.states.get("sensor.posta_slovenije_someone_outgoing_parcels").state == "1"
    )
    assert registry.async_get(summary.entity_id) is not None
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{PICKUP_CODE}"
    )
