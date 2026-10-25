"""Diagnostics support for the Pošta Slovenije parcel tracker integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PostaSlovenijeConfigEntry
from .account.coordinator import PostaSlovenijeAccountCoordinator

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # the parcel and its identifiers
    "trackingNumber",
    "parcelId",
    "traceId",
    "TrackingNumber",
    # scan rows: the signer's name and free-text remarks
    "recipient",
    "signatory",
    "note",
    # a bearer credential for collecting the parcel, and a payment link
    "pickupQRCode",
    "pickupPINCode",
    "payByLinkUrl",
    # parcel-level locations (sender*/receiver* are redacted as a family)
    "pickupUnitAddress",
    "parcelDestinations",
    # sign-in
    "username",
    "password",
    "access_token",
    "refresh_token",
    "Authorization",
}


def _redact(value: Any) -> Any:
    """Redact by name, and every ``sender*`` / ``receiver*`` field as a family."""
    if isinstance(value, dict):
        return {
            key: (
                "**REDACTED**"
                if key in TO_REDACT or str(key).startswith(("sender", "receiver"))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PostaSlovenijeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Pošta Slovenije config entry."""
    coordinator = entry.runtime_data.coordinator

    diagnostics: dict[str, Any] = {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": _redact(coordinator.data or []),
        "delivered": _redact(coordinator.delivered or []),
        "outgoing": _redact(coordinator.outgoing or []),
        "outgoing_delivered": _redact(coordinator.delivered_outgoing or []),
    }
    if isinstance(coordinator, PostaSlovenijeAccountCoordinator):
        diagnostics["counts"]["inbox"] = coordinator.inbox_size
    return diagnostics
