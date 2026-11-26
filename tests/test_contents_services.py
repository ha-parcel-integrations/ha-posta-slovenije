"""Tests for update_contents, the track_parcel field and the store lifecycle."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije.const import (
    CONF_ACCESS_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posta_slovenije.contents import (
    CONTENTS_STORE_MAX_CODES,
    ContentsStore,
)
from custom_components.posta_slovenije.coordinator import PostaSlovenijeBaseCoordinator
from custom_components.posta_slovenije.parcels import normalize_scans
from custom_components.posta_slovenije.tracking.parcels import normalize_parcel

from .payloads import (
    DELIVERED_CODE,
    PICKUP_CODE,
    RETURNED_CODE,
    delivered_payload,
    delivered_statuses,
    inbox_item,
    out_for_delivery_payload,
)

TRACK = "custom_components.posta_slovenije.tracking.api.PostaSlovenijeApiClient"
ACCOUNT = "custom_components.posta_slovenije.account.client.PostaSlovenijeAccountClient"
REFRESH = f"{PostaSlovenijeBaseCoordinator.__module__}.PostaSlovenijeBaseCoordinator"
UNTRACKED = "CH300000009SI"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _hub(codes: list[str]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


def _account(name: str = "someone") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{name}",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: name,
            CONF_PASSWORD: "example-password",
            CONF_ACCESS_TOKEN: "access",
            CONF_REFRESH_TOKEN: "refresh",
            CONF_TOKEN_EXPIRES_AT: 4102444800.0,
        },
    )


def _track_patch(payload=None):
    return patch(
        f"{TRACK}.async_get_parcel",
        new=AsyncMock(
            side_effect=lambda code: payload or out_for_delivery_payload(code)
        ),
    )


def _account_patches(rows):
    """Patches serving ``rows`` as the incoming list; the outgoing one is empty."""

    async def inbox(direction="incoming"):
        return rows if direction == "incoming" else []

    return (
        patch(f"{ACCOUNT}.async_prepare", new=AsyncMock()),
        patch(f"{ACCOUNT}.async_get_inbox", new=AsyncMock(side_effect=inbox)),
        patch(
            f"{ACCOUNT}.async_get_parcel",
            new=AsyncMock(
                side_effect=lambda pid: out_for_delivery_payload(PICKUP_CODE)
            ),
        ),
    )


async def _setup_hub(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = _hub(codes if codes is not None else [PICKUP_CODE])
    entry.add_to_hass(hass)
    with _track_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass, name="someone", rows=None) -> MockConfigEntry:
    entry = _account(name)
    entry.add_to_hass(hass)
    rows = [inbox_item(1, PICKUP_CODE)] if rows is None else rows
    prepare, inbox, detail = _account_patches(rows)
    with prepare, inbox, detail:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_both(hass, hub_codes=None, rows=None):
    hub = _hub(hub_codes if hub_codes is not None else [PICKUP_CODE])
    hub.add_to_hass(hass)
    account = _account()
    account.add_to_hass(hass)
    rows = [inbox_item(1, PICKUP_CODE)] if rows is None else rows
    prepare, inbox, detail = _account_patches(rows)
    with _track_patch(), prepare, inbox, detail:
        await hass.config_entries.async_setup(hub.entry_id)
        await hass.async_block_till_done()
    return hub, account


async def _call(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    coordinator = entry.runtime_data.coordinator
    for parcel in [*coordinator.data, *coordinator.delivered, *coordinator.outgoing]:
        if parcel["barcode"] == code:
            return parcel["contents"]
    raise AssertionError(f"{code} is not published")


def _stored(entry: MockConfigEntry, code: str) -> str | None:
    return entry.runtime_data.coordinator.contents_store.get(code)


async def _unload_all(hass, *entries) -> None:
    for entry in entries:
        entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# the hub
# ---------------------------------------------------------------------------


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup_hub(hass)
    assert _contents(entry, PICKUP_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup_hub(hass)
    for text, expected in (
        (" Running  shoes ", "Running shoes"),
        ("Winter socks", "Winter socks"),
        ("  ", None),
    ):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": text},
        )
        assert _contents(entry, PICKUP_CODE) == expected


async def test_update_contents_without_text_clears(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    await _call(hass, "update_contents", {CONF_TRACKING_CODE: PICKUP_CODE})
    assert _contents(entry, PICKUP_CODE) is None


async def test_update_contents_trims_and_matches_the_code(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: f"  {PICKUP_CODE.lower()} ",
            "contents": "Running shoes",
        },
    )
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )
    assert _stored(entry, UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: PICKUP_CODE}]


async def test_update_contents_never_polls_and_notifies_once_per_change(hass):
    entry = await _setup_hub(hass)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{TRACK}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: UNTRACKED}, blocking=True
        )
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: PICKUP_CODE}, blocking=True
        )
        assert listeners.call_count == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, PICKUP_CODE) is None


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup_hub(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup_hub(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
        )
    await _unload_all(hass, entry)


async def test_the_value_survives_a_following_poll(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    with _track_patch():
        await entry.runtime_data.coordinator.async_refresh()
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup_hub(hass, [])
    with _track_patch():
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
        )
    assert [p[CONF_TRACKING_CODE] for p in entry.options[CONF_PARCELS]] == [PICKUP_CODE]
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup_hub(hass, [])
    with _track_patch():
        await _call(hass, "track_parcel", {CONF_TRACKING_CODE: PICKUP_CODE})
    assert _stored(entry, PICKUP_CODE) is None
    assert _contents(entry, PICKUP_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup_hub(hass)
    track = {CONF_TRACKING_CODE: PICKUP_CODE}
    for text, expected in (
        ("Running shoes", "Running shoes"),
        ("Winter socks", "Winter socks"),
        ("", None),
    ):
        await _call(hass, "track_parcel", {**track, "contents": text})
        assert _contents(entry, PICKUP_CODE) == expected
    assert len(entry.options[CONF_PARCELS]) == 1


async def test_track_parcel_on_a_tracked_code_never_polls(hass):
    entry = await _setup_hub(hass)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{TRACK}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
    assert listeners.call_count == 1
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_a_newly_tracked_code_still_refreshes_via_the_options_listener(hass):
    entry = await _setup_hub(hass, [])
    fetch = AsyncMock(side_effect=lambda code: out_for_delivery_payload(code))
    with patch(f"{TRACK}.async_get_parcel", new=fetch):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
            blocking=True,
        )
        await hass.async_block_till_done()
    fetch.assert_awaited()
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup_hub(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
        )
    await _unload_all(hass, entry)


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: PICKUP_CODE})
    assert _stored(entry, PICKUP_CODE) is None


async def test_untrack_parcel_of_an_unloaded_hub_leaves_the_store_alone(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: PICKUP_CODE})
    assert store.get(PICKUP_CODE) == "Running shoes"
    await _unload_all(hass, entry)


async def test_the_store_caps_at_200_codes_and_drops_the_oldest(hass):
    entry = await _setup_hub(hass)
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: f"CODE{number}", "contents": f"Item {number}"},
            blocking=True,
        )
    assert _stored(entry, "CODE0") is None
    assert _stored(entry, "CODE1") == "Item 1"
    assert _stored(entry, f"CODE{CONTENTS_STORE_MAX_CODES}") is not None


async def test_an_entry_without_a_store_file_reads_as_empty(hass, hass_storage):
    assert STORE_KEY % "missing" not in hass_storage
    entry = await _setup_hub(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, PICKUP_CODE) is None


async def test_a_stored_value_is_applied_at_start_up(hass, hass_storage):
    entry = _hub([PICKUP_CODE])
    entry.add_to_hass(hass)
    key = STORE_KEY % entry.entry_id
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"by_code": {PICKUP_CODE: "Running shoes"}},
    }
    with _track_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_removing_an_entry_deletes_its_store(hass, hass_storage):
    entry = await _setup_hub(hass)
    store = entry.runtime_data.coordinator.contents_store
    store.async_set(PICKUP_CODE, "Running shoes")
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_reapply_contents_covers_every_published_list(hass):
    entry = await _setup_hub(hass)
    coordinator = entry.runtime_data.coordinator
    delivered = normalize_parcel(DELIVERED_CODE, delivered_payload())
    outgoing = normalize_parcel(RETURNED_CODE, out_for_delivery_payload(RETURNED_CODE))
    coordinator.delivered = [delivered]
    coordinator.outgoing = [outgoing]
    coordinator.delivered_outgoing = [dict(delivered)]

    coordinator.contents_store.async_set(DELIVERED_CODE, "Running shoes")
    coordinator.contents_store.async_set(RETURNED_CODE, "Winter socks")
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] == "Running shoes"
    assert coordinator.delivered_outgoing[0]["contents"] == "Running shoes"
    assert coordinator.outgoing[0]["contents"] == "Winter socks"

    coordinator.contents_store.async_set(DELIVERED_CODE, None)
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] is None


async def test_a_barcode_less_parcel_keeps_its_contents_after_reapply_and_a_poll(
    hass,
):
    def barcode_less(code, payload, **kwargs):
        parcel = normalize_parcel(code, payload, **kwargs)
        parcel["barcode"] = ""
        return parcel

    entry = _hub([PICKUP_CODE])
    entry.add_to_hass(hass)
    with (
        _track_patch(),
        patch(
            "custom_components.posta_slovenije.tracking.coordinator.normalize_parcel",
            new=barcode_less,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        assert coordinator.data[0]["barcode"] == ""

        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
        )
        assert coordinator.data[0]["contents"] == "Running shoes"

        await coordinator.async_refresh()
        assert coordinator.data[0]["contents"] == "Running shoes"

        await _call(hass, "update_contents", {CONF_TRACKING_CODE: PICKUP_CODE})
        assert coordinator.data[0]["contents"] is None


def test_the_normaliser_leaves_contents_empty():
    assert (
        normalize_scans(
            barcode=PICKUP_CODE,
            statuses=delivered_statuses(),
            sender=None,
            receiver=None,
            raw={},
        )["contents"]
        is None
    )


# ---------------------------------------------------------------------------
# accounts and routing across entries
# ---------------------------------------------------------------------------


async def test_an_account_publishes_the_stored_contents(hass):
    entry = await _setup_account(hass)
    assert entry.runtime_data.coordinator.known_codes == {PICKUP_CODE}
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    assert _contents(entry, PICKUP_CODE) == "Running shoes"

    prepare, inbox, detail = _account_patches([inbox_item(1, PICKUP_CODE)])
    with prepare, inbox, detail:
        await entry.runtime_data.coordinator.async_refresh()
    assert _contents(entry, PICKUP_CODE) == "Running shoes"


async def test_an_account_only_install_serves_update_contents_alone(hass):
    entry = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_an_account_update_never_polls_and_notifies_once_per_change(hass):
    entry = await _setup_account(hass)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{ACCOUNT}.async_get_inbox", new=AsyncMock()) as inbox,
        patch(f"{ACCOUNT}.async_get_parcel", new=AsyncMock()) as detail,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
    assert listeners.call_count == 1
    refresh.assert_not_called()
    inbox.assert_not_called()
    detail.assert_not_called()


async def test_removing_an_account_deletes_its_store(hass, hass_storage):
    entry = await _setup_account(hass)
    store = entry.runtime_data.coordinator.contents_store
    store.async_set(PICKUP_CODE, "Running shoes")
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_update_contents_goes_to_every_entry_listing_the_code(hass):
    hub, account = await _setup_both(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    assert _stored(hub, PICKUP_CODE) == "Running shoes"
    assert _stored(account, PICKUP_CODE) == "Running shoes"
    assert _contents(account, PICKUP_CODE) == "Running shoes"
    await _unload_all(hass, hub, account)


async def test_update_contents_goes_only_to_the_entry_listing_the_code(hass):
    hub, account = await _setup_both(hass, hub_codes=[])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: PICKUP_CODE, "contents": "Running shoes"},
    )
    assert _stored(account, PICKUP_CODE) == "Running shoes"
    assert _stored(hub, PICKUP_CODE) is None
    await _unload_all(hass, hub, account)


async def test_update_contents_needs_an_entry_when_none_lists_the_code(hass):
    hub, account = await _setup_both(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )
    assert err.value.translation_key == "update_contents_entry_required"
    await _unload_all(hass, hub, account)


async def test_config_entry_id_targets_exactly_that_entry(hass):
    hub, account = await _setup_both(hass)
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": account.entry_id,
        },
    )
    assert _stored(account, UNTRACKED) == "Running shoes"
    assert _stored(hub, UNTRACKED) is None
    await _unload_all(hass, hub, account)


async def test_config_entry_id_of_an_unloaded_or_unknown_entry_is_rejected(hass):
    hub, account = await _setup_both(hass)
    account.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    for entry_id in (account.entry_id, "nope"):
        with pytest.raises(ServiceValidationError):
            await _call(
                hass,
                "update_contents",
                {
                    CONF_TRACKING_CODE: PICKUP_CODE,
                    "contents": "Running shoes",
                    "config_entry_id": entry_id,
                },
            )
    await _unload_all(hass, hub, account)


async def test_an_unloaded_entry_is_never_targeted(hass):
    hub, account = await _setup_both(hass)
    account.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )
    assert _stored(hub, UNTRACKED) == "Running shoes"
    assert _stored(account, UNTRACKED) is None
    await _unload_all(hass, hub, account)


async def test_a_single_loaded_entry_is_targeted_directly(hass):
    hub, account = await _setup_both(hass)
    account.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )
    assert _stored(hub, UNTRACKED) == "Running shoes"
    await _unload_all(hass, hub, account)


async def test_update_contents_stays_while_another_entry_is_loaded(hass):
    hub, account = await _setup_both(hass)
    await hass.config_entries.async_unload(account.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert hass.services.has_service(DOMAIN, "track_parcel")

    await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_the_contents_store_class_is_per_entry(hass):
    one, two = ContentsStore(hass, "one"), ContentsStore(hass, "two")
    one.async_set("A", "Shoes")
    assert two.get("A") is None
