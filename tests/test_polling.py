"""Tests for the dynamic, status-driven polling helpers and the shared coordinator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.posta_slovenije.coordinator import (
    PostaSlovenijeBaseCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)


def _out_for_delivery(planned_from: str | None) -> dict:
    return {"status": "out_for_delivery", "planned_from": planned_from}


def _mid(status: str = "in_transit") -> dict:
    return {"status": status, "planned_from": None}


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor():
    assert _next_anchor(datetime(2026, 1, 1, 2, 30, tzinfo=UTC)) == datetime(
        2026, 1, 1, 6, 0, tzinfo=UTC
    )
    assert _next_anchor(datetime(2026, 1, 1, 14, 0, tzinfo=UTC)) == datetime(
        2026, 1, 2, 0, 0, tzinfo=UTC
    )


def test_stagger_is_stable_and_bounded():
    assert _stagger_minutes("entry-1") == _stagger_minutes("entry-1")
    assert 0 <= _stagger_minutes("entry-2") < STAGGER_MINUTES


def test_tiers():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _hottest_tier_minutes([], now) is None
    assert (
        _hottest_tier_minutes([_mid("registered"), _mid("problem")], now)
        == MID_INTERVAL_MINUTES
    )
    assert (
        _hottest_tier_minutes([_mid(), _out_for_delivery(None)], now)
        == HOT_INTERVAL_MINUTES
    )
    assert (
        _hottest_tier_minutes([_out_for_delivery("garbage")], now)
        == HOT_INTERVAL_MINUTES
    )
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert (
        _hottest_tier_minutes(
            [_out_for_delivery(planned.isoformat())], planned - timedelta(minutes=30)
        )
        == HOT_INTERVAL_MINUTES
    )
    assert (
        _hottest_tier_minutes(
            [_out_for_delivery(planned.isoformat())], planned - timedelta(hours=3)
        )
        == MID_INTERVAL_MINUTES
    )


def test_intervals():
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _next_update_interval(noon, None, "e") is None
    assert _next_update_interval(noon, MID_INTERVAL_MINUTES, "e") == timedelta(
        minutes=MID_INTERVAL_MINUTES + _stagger_minutes("e")
    )
    night = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    assert night + _next_update_interval(night, HOT_INTERVAL_MINUTES, "e") == datetime(
        2026, 1, 1, 6, 0, tzinfo=UTC
    )
    late = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    assert late + _next_update_interval(late, MID_INTERVAL_MINUTES, "e") == datetime(
        2026, 1, 2, 0, 0, tzinfo=UTC
    )


def _parcel(barcode, status, **changes):
    return {
        "barcode": barcode,
        "status": status,
        "delivered": status is ParcelStatus.DELIVERED,
        "delivered_at": "2026-01-01T00:00:00+00:00"
        if status is ParcelStatus.DELIVERED
        else None,
        "planned_from": None,
        "planned_to": None,
        **changes,
    }


class _Scripted(PostaSlovenijeBaseCoordinator):
    """A coordinator whose 'poll' publishes whatever the test set."""

    parcels: list[dict] = []
    sent: list[dict] = []
    fail: Exception | None = None

    async def _async_poll(self):
        if self.fail:
            raise self.fail
        return self._publish(self.parcels, self.sent)


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _listen(hass, suffix):
    events = []
    hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: events.append(e))
    return events


async def test_base_poll_is_abstract(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    with pytest.raises(NotImplementedError):
        await PostaSlovenijeBaseCoordinator(hass, entry)._async_poll()


async def test_first_refresh_fires_nothing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    coordinator.parcels = [_parcel("A", ParcelStatus.IN_TRANSIT)]
    events = [
        _listen(hass, s)
        for s in ("parcel_registered", "parcel_status_changed", "parcel_delivered")
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert all(e == [] for e in events)
    assert coordinator.last_success_time is not None


async def test_events_follow_the_suite_contract(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    coordinator = _Scripted(hass, entry)
    registered = _listen(hass, "parcel_registered")
    changed = _listen(hass, "parcel_status_changed")
    delivered = _listen(hass, "parcel_delivered")

    coordinator.parcels = [_parcel("A", ParcelStatus.IN_TRANSIT)]
    await coordinator._async_update_data()  # seeds the state silently

    coordinator.parcels = [
        _parcel("A", ParcelStatus.OUT_FOR_DELIVERY),
        _parcel("B", ParcelStatus.REGISTERED),
        _parcel("C", ParcelStatus.DELIVERED),
        _parcel("", ParcelStatus.REGISTERED),
    ]
    await coordinator._async_update_data()
    coordinator.parcels = [
        _parcel("A", ParcelStatus.DELIVERED),
        _parcel("B", ParcelStatus.REGISTERED),
        _parcel("C", ParcelStatus.DELIVERED),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["barcode"] for e in registered] == [
        "B"
    ]  # not C: first seen delivered
    assert len(changed) == 1
    assert changed[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert changed[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert changed[0].data["device_id"] == device.id
    # The hop to delivered fires only the dedicated event.
    assert [e.data["barcode"] for e in delivered] == ["A"]


async def test_delivery_time_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    events = _listen(hass, "parcel_delivery_time_changed")

    coordinator.parcels = [_parcel("A", ParcelStatus.OUT_FOR_DELIVERY)]
    await coordinator._async_update_data()
    coordinator.parcels = [
        _parcel("A", ParcelStatus.OUT_FOR_DELIVERY, planned_from="2099-01-01T10:00:00Z")
    ]
    await coordinator._async_update_data()
    coordinator.parcels = [
        _parcel("A", ParcelStatus.OUT_FOR_DELIVERY, planned_from="2099-01-01T10:00:00Z")
    ]
    await coordinator._async_update_data()  # unchanged: silent
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2099-01-01T10:00:00Z"


async def test_idle_hub_stops_and_account_style_never_does(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    hub = _Scripted(hass, entry)
    hub.parcels = []
    await hub._async_update_data()
    assert hub.update_interval is None
    assert hub.current_tier_minutes is None

    class _Account(_Scripted):
        _stops_when_idle = False

    account = _Account(hass, entry)
    account.parcels = []
    await account._async_update_data()
    assert account.update_interval is not None
    assert account.current_tier_minutes == MID_INTERVAL_MINUTES


async def test_rate_limit_backoff_grows_and_honours_retry_after(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    first = coordinator._rate_limited(None)
    second = coordinator._rate_limited(None)
    assert isinstance(first, UpdateFailed)
    assert second.retry_after > first.retry_after
    assert coordinator._rate_limited(90).retry_after == 90
    coordinator.parcels = []
    await coordinator._async_update_data()
    assert coordinator._consecutive_429 == 0


async def test_failed_poll_drops_the_overnight_interval(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    coordinator.fail = RuntimeError("boom")
    coordinator.update_interval = timedelta(hours=5, minutes=40)
    noon = datetime(2026, 1, 15, 12, tzinfo=UTC)
    with (
        patch(
            "custom_components.posta_slovenije.coordinator.dt_util.now",
            return_value=noon,
        ),
        pytest.raises(RuntimeError),
    ):
        await coordinator._async_update_data()
    assert coordinator.update_interval <= timedelta(
        minutes=MID_INTERVAL_MINUTES + STAGGER_MINUTES
    )


async def test_device_id_is_none_without_a_device(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    assert coordinator._device_id() is None
    assert coordinator.delivered_codes == set()


async def test_outgoing_parcels_are_split_from_incoming(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    coordinator.parcels = [_parcel("IN", ParcelStatus.IN_TRANSIT)]
    coordinator.sent = [
        _parcel("OUT", ParcelStatus.IN_TRANSIT),
        _parcel("SENT", ParcelStatus.DELIVERED),
    ]
    data = await coordinator._async_update_data()
    assert [p["barcode"] for p in data] == ["IN"]
    assert [p["barcode"] for p in coordinator.outgoing] == ["OUT"]
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == ["SENT"]
    assert coordinator.delivered == []


async def test_an_outgoing_parcel_keeps_a_hub_polling(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    coordinator.parcels = []
    coordinator.sent = [_parcel("OUT", ParcelStatus.IN_TRANSIT)]
    await coordinator._async_update_data()
    assert coordinator.update_interval is not None
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES


async def test_a_parcel_refiled_as_incoming_is_not_announced_as_new(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = _Scripted(hass, entry)
    registered = _listen(hass, "parcel_registered")
    changed = _listen(hass, "parcel_status_changed")

    coordinator.sent = [_parcel("MOVED", ParcelStatus.IN_TRANSIT)]
    await coordinator._async_update_data()  # first refresh: silent
    coordinator.sent = [_parcel("MOVED", ParcelStatus.OUT_FOR_DELIVERY)]
    await coordinator._async_update_data()

    # the user re-files the code as incoming: same barcode, other list
    coordinator.sent = []
    coordinator.parcels = [_parcel("MOVED", ParcelStatus.OUT_FOR_DELIVERY)]
    await coordinator._async_update_data()
    coordinator.parcels = [_parcel("MOVED", ParcelStatus.DELIVERED)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert registered == []
    assert [e.data["barcode"] for e in changed] == []


async def test_outgoing_events_have_no_registered_and_no_delivery_time(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    coordinator = _Scripted(hass, entry)
    registered = _listen(hass, "parcel_registered")
    timed = _listen(hass, "parcel_delivery_time_changed")
    incoming_changed = _listen(hass, "parcel_status_changed")
    changed = _listen(hass, "outgoing_parcel_status_changed")
    delivered = _listen(hass, "outgoing_parcel_delivered")
    plain_delivered = _listen(hass, "parcel_delivered")

    coordinator.sent = [_parcel("OUT", ParcelStatus.REGISTERED)]
    await coordinator._async_update_data()  # first refresh: silent
    coordinator.sent = [
        _parcel("OUT", ParcelStatus.IN_TRANSIT, planned_from="2099-01-01T10:00:00Z"),
        _parcel("NEW", ParcelStatus.IN_TRANSIT),  # new outgoing: no event at all
        _parcel("", ParcelStatus.IN_TRANSIT),
    ]
    await coordinator._async_update_data()
    coordinator.sent = [
        _parcel("OUT", ParcelStatus.DELIVERED),
        _parcel("NEW", ParcelStatus.IN_TRANSIT),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(changed) == 1
    assert changed[0].data["old_status"] == ParcelStatus.REGISTERED
    assert changed[0].data["new_status"] == ParcelStatus.IN_TRANSIT
    assert changed[0].data["device_id"] == device.id
    # The hop to delivered fires only the dedicated outgoing event.
    assert [e.data["barcode"] for e in delivered] == ["OUT"]
    assert registered == timed == incoming_changed == plain_delivered == []
