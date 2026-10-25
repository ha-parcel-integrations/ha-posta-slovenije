"""Tests for Pošta Slovenije diagnostics: nothing identifying may survive."""

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from custom_components.posta_slovenije.account.coordinator import (
    PostaSlovenijeAccountCoordinator,
)
from custom_components.posta_slovenije.account.parcels import normalize_account_parcel
from custom_components.posta_slovenije.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
)
from custom_components.posta_slovenije.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.posta_slovenije.tracking.parcels import normalize_parcel

from .payloads import (
    DELIVERED_CODE,
    RETURNED_CODE,
    delivered_payload,
    inbox_item,
    returned_payload,
)

R = "**REDACTED**"


def _entry(coordinator, data=None, options=None):
    entry = MagicMock()
    entry.data = data or {}
    entry.options = options or {}
    entry.runtime_data.coordinator = coordinator
    return entry


def _coordinator(cls=MagicMock, **attrs):
    coordinator = MagicMock(spec=cls) if cls is not MagicMock else MagicMock()
    coordinator.current_tier_minutes = 15
    coordinator.update_interval = timedelta(minutes=15)
    coordinator.data = []
    coordinator.delivered = []
    coordinator.outgoing = []
    coordinator.delivered_outgoing = []
    coordinator.delivered_codes = set()
    for key, value in attrs.items():
        setattr(coordinator, key, value)
    return coordinator


async def test_tracking_diagnostics_redact_numbers_names_and_notes(hass):
    parcel = normalize_parcel(DELIVERED_CODE, delivered_payload())
    returned = normalize_parcel(RETURNED_CODE, returned_payload())
    coordinator = _coordinator(data=[returned], delivered=[parcel])
    entry = _entry(
        coordinator, options={"parcels": [{"tracking_code": DELIVERED_CODE}]}
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 1,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    assert result["entry_options"]["parcels"][0]["tracking_code"] == R
    delivered = result["delivered"][0]
    assert delivered["barcode"] == R
    assert delivered["url"] == R
    assert delivered["raw"]["result"]["parcel"]["trackingNumber"] == R
    assert delivered["raw"]["result"]["parcel"]["parcelId"] == R
    assert delivered["raw"]["traceId"] == R
    rows = delivered["raw"]["result"]["statuses"]
    assert {r["recipient"] for r in rows} == {R}
    assert {r["signatory"] for r in rows} == {R}
    assert {r["note"] for r in rows} == {R}
    # what a report needs survives
    assert delivered["status"] == "delivered"
    assert rows[-1]["code"] == "VL"
    assert "Signer Name" not in str(result)
    assert "Left at the door" not in str(result)
    assert DELIVERED_CODE not in str(result)


async def test_account_diagnostics_redact_credentials_parties_and_pickup_codes(hass):
    item = inbox_item(
        7, DELIVERED_CODE, senderEmail="a@example.test", receiverPhone="123"
    )
    parcel = normalize_account_parcel(item, delivered_payload())
    coordinator = _coordinator(
        PostaSlovenijeAccountCoordinator, data=[parcel], inbox_size=1
    )
    entry = _entry(
        coordinator,
        data={
            CONF_USERNAME: "someone",
            CONF_PASSWORD: "hunter2",
            CONF_ACCESS_TOKEN: "access-token",
            CONF_REFRESH_TOKEN: "refresh-token",
            "source": "account",
        },
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"]["inbox"] == 1
    assert result["entry_data"][CONF_USERNAME] == R
    assert result["entry_data"][CONF_PASSWORD] == R
    assert result["entry_data"][CONF_ACCESS_TOKEN] == R
    assert result["entry_data"][CONF_REFRESH_TOKEN] == R
    assert result["entry_data"]["source"] == "account"
    inbox = result["incoming"][0]["raw"]["inbox"]
    for key in (
        "senderName",
        "receiverName",
        "senderEmail",
        "receiverPhone",
        "pickupQRCode",
        "pickupPINCode",
        "parcelId",
        "trackingNumber",
    ):
        assert inbox[key] == R
    text = str(result)
    for secret in (
        "Example Shop",
        "Example Receiver",
        "QR-EXAMPLE",
        "4817",
        "a@example.test",
        "hunter2",
    ):
        assert secret not in text


async def test_diagnostics_report_suspended_polling(hass):
    coordinator = _coordinator(current_tier_minutes=None, update_interval=None)
    result = await async_get_config_entry_diagnostics(hass, _entry(coordinator))
    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


async def test_outgoing_lists_are_in_diagnostics_and_redacted(hass):
    sent = normalize_account_parcel(
        inbox_item(8, RETURNED_CODE), returned_payload(RETURNED_CODE)
    )
    sent_delivered = normalize_account_parcel(
        inbox_item(9, DELIVERED_CODE), delivered_payload()
    )
    coordinator = _coordinator(outgoing=[sent], delivered_outgoing=[sent_delivered])
    result = await async_get_config_entry_diagnostics(hass, _entry(coordinator))

    assert result["counts"]["outgoing_active"] == 1
    assert result["counts"]["outgoing_delivered"] == 1
    assert result["outgoing"][0]["barcode"] == R
    assert result["outgoing"][0]["raw"]["inbox"]["receiverName"] == R
    assert result["outgoing_delivered"][0]["raw"]["inbox"]["pickupPINCode"] == R
    text = str(result)
    for secret in ("Example Receiver", "Example Shop", "4817", RETURNED_CODE):
        assert secret not in text
