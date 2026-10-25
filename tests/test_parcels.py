"""Tests for the shared status map and scan-row normalizer."""

from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import MagicMock

import pytest

from custom_components.posta_slovenije import parcels
from custom_components.posta_slovenije.const import (
    CAPABILITIES,
    CAPABILITIES_BY_VARIANT,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    PENDING_CAPABILITIES_BY_VARIANT,
    ParcelStatus,
)
from custom_components.posta_slovenije.parcels import (
    NEW_ISSUE_URL,
    apply_delivered_filter,
    check_keys,
    normalize_scans,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import (
    POST_OFFICE,
    RETURN_STEP,
    delivered_statuses,
    pickup_payload,
    returned_statuses,
    row,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def _normalize(statuses, **kwargs):
    return normalize_scans(
        barcode="CH000000005SI",
        statuses=statuses,
        sender=None,
        receiver=None,
        raw={},
        **kwargs,
    )


def _warnings(caplog, phrase=""):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and phrase in r.getMessage()
    ]


def test_normalize_publishes_exactly_the_canonical_keys():
    assert list(_normalize(delivered_statuses())) == CANONICAL_KEYS


def test_capabilities_are_consistent():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES
    assert not CAPABILITIES & PENDING_CAPABILITIES
    assert set(CAPABILITIES_BY_VARIANT) == set(PENDING_CAPABILITIES_BY_VARIANT)
    for variant, populated in CAPABILITIES_BY_VARIANT.items():
        assert populated <= KNOWN_CAPABILITIES
        assert not populated & PENDING_CAPABILITIES_BY_VARIANT[variant]


def test_delivered_parcel():
    parcel = _normalize(delivered_statuses(), include_history=True)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-09-29T08:15:30+00:00"
    assert parcel["raw_status"] == "VL EN"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert parcel["history"][0]["timestamp"] == "2026-09-28T05:00:00.500000+00:00"


def test_returned_parcel_is_returning_not_delivered():
    # VRP carries statusId 3 / "Dostavljena", but it went back to the sender.
    parcel = _normalize(returned_statuses())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["pickup_point"] is None


def test_returned_history_derives_each_row_in_sequence():
    parcel = _normalize(returned_statuses(), include_history=True)
    assert [e["status"] for e in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.RETURNING,
    ]


def test_returned_parcel_on_the_way_back_is_still_out_for_delivery_by_code():
    parcel = _normalize(returned_statuses()[:8])
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY


def test_post_office_parcel_has_a_pickup_point():
    parcel = _normalize(pickup_payload()["result"]["statuses"])
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Velenje, Example street 1, 3320 Velenje"


def test_failed_attempt_without_return_wording_is_a_problem(caplog):
    statuses = [
        *delivered_statuses()[:3],
        row(
            "V",
            "2026-09-29T09:00:00Z",
            nondeliveryActionEN="Item will be delivered again",
        ),
    ]
    with caplog.at_level(logging.WARNING):
        assert _normalize(statuses)["status"] is ParcelStatus.PROBLEM
        _normalize(statuses)
    warnings = _warnings(caplog, "nondeliveryActionEN")
    assert len(warnings) == 1  # one-shot
    assert "not about a return" in warnings[0].getMessage()


def test_a_failed_attempt_with_no_english_action_warns_rather_than_guessing(caplog):
    statuses = [
        *delivered_statuses()[:3],
        row("V", "2026-09-29T09:00:00Z", nondeliveryActionEN="", nondeliveryId=1),
    ]
    with caplog.at_level(logging.WARNING):
        assert _normalize(statuses)["status"] is ParcelStatus.PROBLEM
    warnings = _warnings(caplog, "nondeliveryActionEN")
    assert len(warnings) == 1
    assert "is empty" in warnings[0].getMessage()


@pytest.mark.parametrize("code", ["EMC", "VLPAKP", "IZR", "POTPKTM"])
def test_a_code_no_real_parcel_has_carried_reports_itself_once(code, caplog):
    statuses = [row(code, "2026-09-29T09:00:00Z")]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(statuses)
        _normalize(statuses)
    assert parcel["status"] is not ParcelStatus.UNKNOWN
    warnings = _warnings(caplog, "never been seen on a real")
    assert len(warnings) == 1  # one-shot
    assert code in warnings[0].getMessage()


def test_a_confirmed_code_says_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        _normalize(delivered_statuses())
    assert _warnings(caplog) == []


def test_return_wording_is_matched_case_insensitively():
    step = {**RETURN_STEP, "nondeliveryActionEN": "RETURNED TO SENDER"}
    statuses = [
        row("N", "2026-09-29T08:00:00Z"),
        row("V", "2026-09-29T09:00:00Z", **step),
    ]
    assert _normalize(statuses)["status"] is ParcelStatus.RETURNING


def test_delivery_after_a_returning_attempt_is_returning():
    statuses = [
        row("N", "2026-09-29T08:00:00Z"),
        row("V", "2026-09-29T09:00:00Z", **RETURN_STEP),
        row("VL", "2026-09-30T09:00:00Z"),
    ]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_delivery_after_a_plain_problem_is_delivered():
    statuses = [
        row("V", "2026-09-29T09:00:00Z", nondeliveryActionEN="Try again tomorrow"),
        row("VL", "2026-09-30T09:00:00Z"),
    ]
    assert _normalize(statuses)["status"] is ParcelStatus.DELIVERED


def test_handover_in_person_is_delivered_and_stamps_delivered_at():
    """``VR`` is a delivery scan, not an unmapped code (live CF369340877SI).

    It follows ``N`` with subCode ``VRPIS`` and the *Dostavljena* bucket, the
    same as ``VL``; reading it as unmapped left the parcel out_for_delivery
    forever and never stamped ``delivered_at``.
    """
    statuses = [
        row("N", "2026-10-12T05:24:07Z"),
        row("VR", "2026-10-12T06:11:00Z"),
    ]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-12T06:11:00+00:00"


def test_handover_in_person_after_a_returning_attempt_is_returning():
    statuses = [
        row("V", "2026-09-29T09:00:00Z", **RETURN_STEP),
        row("VR", "2026-09-30T09:00:00Z"),
    ]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_collected_at_the_post_office_in_person_keeps_the_pickup_point():
    statuses = [
        row("PVI", "2026-09-29T09:00:00Z", **POST_OFFICE),
        row("VR", "2026-09-30T09:00:00Z"),
    ]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["pickup_point"] == "Velenje, Example street 1, 3320 Velenje"


def test_collected_at_the_post_office_keeps_the_pickup_point():
    statuses = [
        row("PVI", "2026-09-29T09:00:00Z", **POST_OFFICE),
        row("VL", "2026-09-30T09:00:00Z"),
    ]
    parcel = _normalize(statuses)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["pickup_point"] == "Velenje, Example street 1, 3320 Velenje"


def test_pickup_point_is_dropped_when_the_pickup_row_is_not_the_last():
    statuses = [
        row("IZR", "2026-09-29T09:00:00Z", **POST_OFFICE),
        row("N", "2026-09-30T08:00:00Z"),
        row("VL", "2026-09-30T09:00:00Z"),
    ]
    assert _normalize(statuses)["pickup_point"] is None


def test_pickup_row_without_an_address_gives_no_pickup_point():
    parcel = _normalize([row("IZR", "2026-09-29T09:00:00Z")])
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup_point"] is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NAJ", ParcelStatus.REGISTERED),
        ("S", ParcelStatus.IN_TRANSIT),
        ("EMC", ParcelStatus.IN_TRANSIT),
        ("N", ParcelStatus.OUT_FOR_DELIVERY),
        ("VLPAKP", ParcelStatus.OUT_FOR_DELIVERY),
        ("IZR", ParcelStatus.AT_PICKUP_POINT),
        ("PVI", ParcelStatus.AT_PICKUP_POINT),
        ("VL", ParcelStatus.DELIVERED),
        ("VR", ParcelStatus.DELIVERED),
        ("VDO", ParcelStatus.PROBLEM),
        ("POTPKTM", ParcelStatus.PROBLEM),
        ("V", ParcelStatus.PROBLEM),
        ("VRP", ParcelStatus.RETURNING),
    ],
)
def test_every_mapped_code(code, expected):
    assert _normalize([row(code, "2026-09-29T09:00:00Z")])["status"] is expected


def test_rows_are_sorted_by_clock_not_by_arrival_order():
    statuses = list(reversed(delivered_statuses()))
    assert _normalize(statuses)["status"] is ParcelStatus.DELIVERED


def test_unmapped_code_falls_back_to_history_and_warns_once(caplog):
    statuses = [
        row("S", "2026-09-29T09:00:00Z"),
        row("ZZZ", "2026-09-29T10:00:00Z", codeDescriptionEN="Something new"),
    ]
    with caplog.at_level(logging.WARNING):
        first = _normalize(statuses, include_history=True)
        _normalize(statuses)
    assert first["status"] is ParcelStatus.IN_TRANSIT
    assert first["raw_status"] == "Something new"
    assert first["history"][-1]["status"] is None
    warnings = _warnings(caplog, "ZZZ")
    assert len(warnings) == 1
    assert NEW_ISSUE_URL in warnings[0].getMessage()
    assert "Something new" in warnings[0].getMessage()


def test_all_unmapped_is_unknown_with_one_warning(caplog):
    statuses = [row("ZZZ", "2026-09-29T10:00:00Z"), row("ZZY", "2026-09-29T11:00:00Z")]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(statuses)
        _normalize(statuses)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert len(_warnings(caplog, "ZZZ")) == 1
    assert len(_warnings(caplog, "ZZY")) == 1


def test_a_row_without_a_code_is_reported_as_unmapped(caplog):
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(
            [
                row(
                    None,
                    "2026-09-29T10:00:00Z",
                    codeDescriptionEN=None,
                    codeDescriptionSI=None,
                )
            ],
            include_history=True,
        )
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] is None
    assert _warnings(caplog, "<none>")


def test_unreadable_clock_skips_the_row_and_warns_once(caplog):
    statuses = [
        row("S", "2026-09-29T09:00:00Z"),
        row("N", "yesterday-ish"),
    ]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(statuses)
        _normalize(statuses)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert len(_warnings(caplog, "unreadable statusDateTime")) == 1


def test_only_unreadable_rows_are_unknown_not_registered():
    assert _normalize([row("S", "nonsense")])["status"] is ParcelStatus.UNKNOWN


def test_empty_statuses_are_registered_without_history():
    for statuses in ([], None):
        parcel = _normalize(statuses, include_history=True)
        assert parcel["status"] is ParcelStatus.REGISTERED
        assert parcel["history"] == []
        assert parcel["raw_status"] is None


def test_non_dict_rows_are_ignored():
    assert _normalize(["junk", row("S", "2026-09-29T09:00:00Z")])["status"] is (
        ParcelStatus.IN_TRANSIT
    )


def test_canceled_row_warns_once(caplog):
    statuses = [row("S", "2026-09-29T09:00:00Z", canceled=1)]
    with caplog.at_level(logging.WARNING):
        _normalize(statuses)
        _normalize(statuses)
    assert len(_warnings(caplog, "non-zero 'canceled'")) == 1


def test_history_is_off_by_default_and_capped_at_twenty():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    statuses = [
        row("S", (start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"))
        for i in range(30)
    ]
    assert _normalize(statuses)["history"] is None
    history = _normalize(statuses, include_history=True)["history"]
    assert len(history) == 20
    assert history[-1]["timestamp"] == "2026-09-02T05:00:00+00:00"


def test_raw_status_falls_back_to_the_slovenian_wording():
    parcel = _normalize([row("S", "2026-09-29T09:00:00Z", codeDescriptionEN="")])
    assert parcel["raw_status"] == "S SI"


def test_raw_status_falls_back_to_the_code_in_history():
    parcel = _normalize(
        [row("S", "2026-09-29T09:00:00Z", codeDescriptionEN="", codeDescriptionSI="")],
        include_history=True,
    )
    assert parcel["history"][0]["raw_status"] == "S"


def test_unknown_keys_are_reported_once_with_names_only(caplog):
    payload = {"result": 1, "brandNewKey": "secret-value"}
    with caplog.at_level(logging.WARNING):
        check_keys("response", payload, parcels.KNOWN_TOP_KEYS)
        check_keys("response", payload, parcels.KNOWN_TOP_KEYS)
    warnings = _warnings(caplog, "brandNewKey")
    assert len(warnings) == 1
    assert "secret-value" not in warnings[0].getMessage()


def test_party_fields_are_a_known_family_on_a_parcel(caplog):
    with caplog.at_level(logging.WARNING):
        check_keys(
            "parcel", {"senderCity": "x", "receiverZip": "y"}, parcels.KNOWN_PARCEL_KEYS
        )
        check_keys("parcel", "not a dict", parcels.KNOWN_PARCEL_KEYS)
    assert not _warnings(caplog)


def test_scan_rows_report_unknown_keys(caplog):
    with caplog.at_level(logging.WARNING):
        _normalize([row("S", "2026-09-29T09:00:00Z", brandNewRowKey=1)])
    assert _warnings(caplog, "brandNewRowKey")


def test_parse_iso_handles_every_fraction_width_and_naive_values():
    for value in (
        "2026-09-29T09:00:00Z",
        "2026-09-29T09:00:00.5Z",
        "2026-09-29T09:00:00.123Z",
    ):
        assert parse_iso(value).tzinfo is not None
    assert parse_iso("2026-09-29T09:00:00").tzinfo is UTC
    assert parse_iso("") is None
    assert parse_iso("nope") is None


def test_tracking_url():
    assert tracking_url("CH000000005SI") == (
        "https://moja.posta.si/tracking?trackingNumber=CH000000005SI"
    )
    assert tracking_url(None) is None


def test_sort_puts_missing_timestamps_last():
    items = [
        {"id": "none"},
        {"id": "b", "delivered_at": "2026-01-02T00:00:00Z"},
        {"id": "a", "delivered_at": "2026-01-01T00:00:00Z"},
    ]
    assert [p["id"] for p in sort_parcels_by_ts(items, "delivered_at")] == [
        "a",
        "b",
        "none",
    ]
    assert [
        p["id"] for p in sort_parcels_by_ts(items, "delivered_at", descending=True)
    ] == [
        "b",
        "a",
        "none",
    ]


def _entry(options):
    entry = MagicMock()
    entry.options = options
    return entry


def test_delivered_filter_by_count_and_days():
    now = datetime.now(UTC)
    items = [
        {"delivered_at": now.isoformat()},
        {"delivered_at": (now - timedelta(days=30)).isoformat()},
        {"delivered_at": None},
    ]
    assert (
        len(
            apply_delivered_filter(
                items,
                _entry(
                    {"delivered_filter_type": "parcels", "delivered_filter_amount": 2}
                ),
            )
        )
        == 2
    )
    kept = apply_delivered_filter(items, _entry({}))
    assert len(kept) == 2
