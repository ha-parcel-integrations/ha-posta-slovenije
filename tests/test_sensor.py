"""Tests for Pošta Slovenije sensor property logic."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.posta_slovenije.const import ParcelStatus
from custom_components.posta_slovenije.sensor import (
    PostaSlovenijeAwaitingPickupSensor,
    PostaSlovenijeDeliveredParcelsSensor,
    PostaSlovenijeIncomingParcelsSensor,
    PostaSlovenijeLastUpdateSensor,
    PostaSlovenijeNextDeliverySensor,
    PostaSlovenijeOutgoingDeliveredParcelsSensor,
    PostaSlovenijeOutgoingParcelsSensor,
    PostaSlovenijeParcelSensor,
    _PostaSlovenijePickupListSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(
    data: list[dict],
    delivered: list[dict] | None = None,
    outgoing: list[dict] | None = None,
    delivered_outgoing: list[dict] | None = None,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    coordinator.outgoing = outgoing if outgoing is not None else []
    coordinator.delivered_outgoing = (
        delivered_outgoing if delivered_outgoing is not None else []
    )
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "Pošta Slovenije",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = PostaSlovenijeIncomingParcelsSensor(
        coordinator, _entry(), lambda _: None, set()
    )
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = PostaSlovenijeParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = PostaSlovenijeParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2026-05-02T10:00:00Z"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = PostaSlovenijeNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_none_without_moments():
    sensor = PostaSlovenijeNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="not-a-date"),
            _parcel("B", planned_from="2026-05-01T10:00:00Z"),
        ]
    )
    sensor = PostaSlovenijeNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_awaiting_pickup_lists_only_parcels_at_a_pickup_point():
    parcels = [
        _parcel("READY", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
        _parcel("HOME"),
    ]
    awaiting = PostaSlovenijeAwaitingPickupSensor(_coordinator(parcels), _entry())
    assert awaiting.unique_id == "e1_awaiting_pickup"
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == [
        "READY"
    ]
    assert awaiting.native_value == 1


def test_awaiting_pickup_zero_without_data():
    coordinator = _coordinator([])
    coordinator.data = None
    assert PostaSlovenijeAwaitingPickupSensor(coordinator, _entry()).native_value == 0


def test_delivered_sensor():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = PostaSlovenijeDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    coordinator.last_success_time = moment
    sensor = PostaSlovenijeLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment


def test_the_pickup_list_base_is_abstract():
    _PostaSlovenijePickupListSensor._suffix = "x"
    sensor = _PostaSlovenijePickupListSensor(_coordinator([]), _entry())
    with pytest.raises(NotImplementedError):
        sensor._parcels()


def test_outgoing_sensors_count_only_sent_parcels():
    coordinator = _coordinator(
        [_parcel("IN")],
        outgoing=[_parcel("OUT1"), _parcel("OUT2")],
        delivered_outgoing=[_parcel("SENT", status=ParcelStatus.DELIVERED)],
    )
    active = PostaSlovenijeOutgoingParcelsSensor(coordinator, _entry())
    delivered = PostaSlovenijeOutgoingDeliveredParcelsSensor(coordinator, _entry())
    assert active.unique_id == "e1_outgoing_parcels"
    assert delivered.unique_id == "e1_outgoing_delivered_parcels"
    assert active.native_value == 2
    assert [p["barcode"] for p in active.extra_state_attributes["parcels"]] == [
        "OUT1",
        "OUT2",
    ]
    assert delivered.native_value == 1
    assert delivered.extra_state_attributes["parcels"][0]["barcode"] == "SENT"


def test_outgoing_sensors_are_zero_without_data():
    coordinator = _coordinator([])
    coordinator.outgoing = None
    coordinator.delivered_outgoing = None
    assert PostaSlovenijeOutgoingParcelsSensor(coordinator, _entry()).native_value == 0
    assert (
        PostaSlovenijeOutgoingDeliveredParcelsSensor(coordinator, _entry()).native_value
        == 0
    )


def test_incoming_count_excludes_outgoing_parcels():
    coordinator = _coordinator([_parcel("IN")], outgoing=[_parcel("OUT")])
    sensor = PostaSlovenijeIncomingParcelsSensor(
        coordinator, _entry(), lambda _: None, set()
    )
    assert sensor.native_value == 1
    assert [p["barcode"] for p in sensor.extra_state_attributes["parcels"]] == ["IN"]


def test_a_parcel_sensor_finds_an_outgoing_parcel():
    coordinator = _coordinator([_parcel("IN")], outgoing=[_parcel("OUT")])
    sensor = PostaSlovenijeParcelSensor(coordinator, _entry(), "OUT")
    assert sensor.native_value == ParcelStatus.IN_TRANSIT
    assert sensor.extra_state_attributes["barcode"] == "OUT"


def test_new_outgoing_parcels_spawn_sensors():
    coordinator = _coordinator([_parcel("IN")], outgoing=[_parcel("OUT")])
    added = []
    sensor = PostaSlovenijeIncomingParcelsSensor(
        coordinator, _entry(), added.extend, {"IN"}
    )
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor._handle_coordinator_update()
    assert [e._barcode for e in added] == ["OUT"]


def test_awaiting_pickup_and_next_delivery_ignore_outgoing_parcels():
    sent = _parcel(
        "OUT",
        status=ParcelStatus.AT_PICKUP_POINT,
        pickup=True,
        planned_from="2026-05-01T10:00:00Z",
    )
    coordinator = _coordinator([], outgoing=[sent])
    assert PostaSlovenijeAwaitingPickupSensor(coordinator, _entry()).native_value == 0
    next_delivery = PostaSlovenijeNextDeliverySensor(coordinator, _entry())
    assert next_delivery.native_value is None
