"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.posta_slovenije.const import ParcelStatus
from custom_components.posta_slovenije.parcels import (
    normalize_scans,
    parse_iso,
    planned_window,
)

from .payloads import POST_OFFICE, delivered_statuses, returned_statuses, row

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

ZONE = ZoneInfo("Europe/Ljubljana")


def _normalize(statuses) -> dict:
    return normalize_scans(
        barcode="CH000000005SI",
        statuses=statuses,
        sender=None,
        receiver=None,
        raw={},
    )


def _collected() -> list[dict]:
    return [
        row("NAJ", "2026-09-28T05:00:00Z"),
        row("PVI", "2026-09-29T09:00:00Z", **POST_OFFICE),
        row("VR", "2026-09-30T09:00:00Z"),
    ]


SAMPLES = {
    "delivered": delivered_statuses,
    "active": lambda: delivered_statuses()[:3],
    "at_pickup_point": lambda: returned_statuses()[:5],
    "collected": _collected,
    "returning": lambda: returned_statuses()[:6],
    "back_at_sender": returned_statuses,
    "problem": lambda: returned_statuses()[:4],
    "none": list,
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return _normalize(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None
    else:
        assert parse_iso(parcel["delivered_at"]) is not None


def test_planned_is_empty(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_parcel_stays_pickup():
    parcel = _normalize(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Velenje"
    assert parcel["delivered_at"] == "2026-09-30T09:00:00+00:00"


def test_the_pickup_point_is_the_name_without_the_address():
    parcel = _normalize(returned_statuses()[:5])
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup_point"] == "Velenje"


def test_a_home_delivery_is_not_pickup():
    parcel = _normalize(delivered_statuses())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_return_that_reached_the_sender_is_not_delivered():
    parcel = _normalize(returned_statuses())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_a_delivery_scan_after_a_returning_attempt_is_not_delivered():
    statuses = [*returned_statuses()[:6], row("VL", "2026-10-12T09:00:00Z")]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2026-04-29T13:00:00Z", None, zone=ZONE)
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == parse_iso(start).astimezone(ZONE).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2026-04-29", None, zone=ZONE)
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    assert (first.hour, first.minute, first.second) == (0, 0, 0)
    assert (last.hour, last.minute, last.second, last.microsecond) == (23, 59, 59, 0)
    assert first.date() == last.date()
    assert (
        first.utcoffset()
        == last.utcoffset()
        == datetime(2026, 4, 29, tzinfo=ZONE).utcoffset()
    )
    assert first < datetime(2026, 4, 29, 12, tzinfo=UTC) < last
