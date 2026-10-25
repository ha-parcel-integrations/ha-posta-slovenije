"""Normalise one guest-lookup reply into the canonical parcel shape."""

from __future__ import annotations

from typing import Any

from ..const import ParcelStatus
from ..parcels import normalize_scans


def scans_of(payload: dict[str, Any] | None) -> list[Any]:
    """The reply's per-scan rows; empty when there are none."""
    result = (payload or {}).get("result")
    statuses = result.get("statuses") if isinstance(result, dict) else None
    return statuses if isinstance(statuses, list) else []


def normalize_parcel(
    tracking_code: str,
    payload: dict[str, Any] | None,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return a carrier-agnostic parcel dict with the full reply under ``raw``.

    ``payload`` is ``None`` for a number the service does not know: the user
    asked us to track it, so it shows as ``unknown`` until a scan arrives.
    The sender and receiver are always ``None`` — the service withholds them
    from a guest.
    """
    parcel = normalize_scans(
        barcode=tracking_code,
        statuses=scans_of(payload),
        sender=None,
        receiver=None,
        raw=payload if payload is not None else {"trackingNumber": tracking_code},
        include_history=include_history,
    )
    if payload is None:
        parcel["status"] = ParcelStatus.UNKNOWN
    return parcel
