"""Tests for the account normalizer (every field shape is an assumption)."""

import logging

import pytest

from custom_components.posta_slovenije.account.parcels import (
    normalize_account_parcel,
    row_direction,
    scans_of,
)
from custom_components.posta_slovenije.const import ParcelStatus
from tests.payloads import delivered_payload, delivered_statuses, inbox_item


def test_sender_and_receiver_are_populated_from_the_account():
    item = inbox_item(1, "CH100000003SI")
    parcel = normalize_account_parcel(item, delivered_payload("CH100000003SI"))
    assert parcel["barcode"] == "CH100000003SI"
    assert parcel["sender"] == "Example Shop"
    assert parcel["receiver"] == "Example Receiver"
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["raw"]["inbox"] is item


def test_the_coarse_inbox_status_is_never_read():
    item = inbox_item(1, "CH100000003SI", statusId=3, codeDescriptionEN="Delivered")
    parcel = normalize_account_parcel(
        item, {"result": {"statuses": delivered_statuses()[:2]}}
    )
    assert parcel["status"] is ParcelStatus.IN_TRANSIT


def test_parties_fall_back_to_the_detail_parcel_then_none():
    item = inbox_item(1, "CH100000003SI", senderName=None, receiverName="  ")
    detail = delivered_payload("CH100000003SI")
    detail["result"]["parcel"]["senderName"] = "From Detail"
    parcel = normalize_account_parcel(item, detail)
    assert parcel["sender"] == "From Detail"
    assert parcel["receiver"] is None


def test_a_parcel_with_neither_party_name_reports_it_once(caplog):
    # Both key names are guesses, so an empty pair is worth a line.
    item = inbox_item(1, "CH100000003SI", senderName=None, receiverName=None)
    with caplog.at_level(logging.WARNING):
        parcel = normalize_account_parcel(item, None)
        normalize_account_parcel(item, None)
    assert parcel["sender"] is None and parcel["receiver"] is None
    warnings = [
        r
        for r in caplog.records
        if "neither senderName nor receiverName" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_scans_come_from_the_detail_else_the_row_else_nothing():
    row_scans = delivered_statuses()[:1]
    item = inbox_item(1, "CH100000003SI", statuses=row_scans)
    assert (
        scans_of(item, delivered_payload()) == delivered_payload()["result"]["statuses"]
    )
    assert scans_of(item, None) == row_scans
    assert scans_of(item, {"result": {"statuses": "x"}}) == row_scans
    assert scans_of(inbox_item(1, "CH100000003SI"), None) == []
    assert scans_of(inbox_item(1, "CH100000003SI"), {"result": "x"}) == []


def test_a_row_without_scans_or_detail_is_registered():
    parcel = normalize_account_parcel(inbox_item(1, "CH100000003SI"), None)
    assert parcel["status"] is ParcelStatus.REGISTERED


def test_the_barcode_is_trimmed():
    assert (
        normalize_account_parcel(inbox_item(1, " CH100000003SI "), None)["barcode"]
        == "CH100000003SI"
    )


@pytest.mark.parametrize(
    ("flag", "listed", "expected"),
    [
        (False, "incoming", "incoming"),
        (True, "outgoing", "outgoing"),
        (True, "incoming", "outgoing"),
        (False, "outgoing", "incoming"),
        (None, "outgoing", "outgoing"),
        (None, "incoming", "incoming"),
        ("yes", "outgoing", "incoming"),
        (1, "outgoing", "incoming"),
    ],
)
def test_row_direction(flag, listed, expected):
    assert row_direction({"isOutgoingParcel": flag}, listed) == expected
