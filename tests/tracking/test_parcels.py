"""Tests for the guest-lookup normalizer."""

from custom_components.posta_slovenije.const import ParcelStatus
from custom_components.posta_slovenije.tracking.parcels import (
    normalize_parcel,
    scans_of,
)
from tests.payloads import (
    DELIVERED_CODE,
    RETURNED_CODE,
    delivered_payload,
    returned_payload,
)


def test_delivered_reply():
    payload = delivered_payload()
    parcel = normalize_parcel(DELIVERED_CODE, payload)
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["raw"] is payload
    assert parcel["url"].endswith(DELIVERED_CODE)


def test_returned_reply_ignores_the_lagging_parcel_summary():
    payload = returned_payload()
    assert payload["result"]["parcel"]["statusId"] == 3
    parcel = normalize_parcel(RETURNED_CODE, payload)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_unknown_number_shows_as_unknown_with_a_placeholder():
    parcel = normalize_parcel(RETURNED_CODE, None)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["barcode"] == RETURNED_CODE
    assert parcel["raw"] == {"trackingNumber": RETURNED_CODE}


def test_a_reply_without_scans_is_registered():
    payload = delivered_payload()
    payload["result"]["statuses"] = []
    assert (
        normalize_parcel(DELIVERED_CODE, payload)["status"] is ParcelStatus.REGISTERED
    )


def test_scans_of_tolerates_odd_shapes():
    assert scans_of(None) == []
    assert scans_of({"result": "x"}) == []
    assert scans_of({"result": {"statuses": "x"}}) == []
