"""Normalise one inbox row and its detail into the canonical parcel shape.

Every field shape here is an assumption: the inbox and detail payloads have not
been seen on the wire, so each lookup tolerates a missing or odd value.
"""

from __future__ import annotations

from typing import Any

from ..const import DIRECTION_INCOMING, DIRECTION_OUTGOING
from ..parcels import normalize_scans, warn_once


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _result_of(detail: dict[str, Any] | None) -> dict[str, Any]:
    result = (detail or {}).get("result")
    return result if isinstance(result, dict) else {}


def scans_of(item: dict[str, Any], detail: dict[str, Any] | None) -> list[Any]:
    """The per-scan rows: the detail's, else the inbox row's own, else none."""
    for source in (_result_of(detail), item):
        statuses = source.get("statuses")
        if isinstance(statuses, list):
            return statuses
    return []


def normalize_account_parcel(
    item: dict[str, Any],
    detail: dict[str, Any] | None,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return a carrier-agnostic parcel dict with the record under ``raw``.

    The inbox row's coarse ``statusId`` and summary are never read: the status
    comes from the scans. Sender and receiver are the names the account sees.
    """
    parcel = _result_of(detail).get("parcel")
    parcel = parcel if isinstance(parcel, dict) else {}
    sender = _text(item.get("senderName")) or _text(parcel.get("senderName"))
    receiver = _text(item.get("receiverName")) or _text(parcel.get("receiverName"))
    if sender is None and receiver is None:
        # Both key names are guesses, and ``check_keys`` accepts the whole
        # ``sender*`` / ``receiver*`` family, so nothing else would notice.
        warn_once(
            "party-names",
            "a parcel reports neither senderName nor receiverName, so both stay empty",
        )
    return normalize_scans(
        barcode=str(item["trackingNumber"]).strip(),
        statuses=scans_of(item, detail),
        sender=sender,
        receiver=receiver,
        raw={"inbox": item, "detail": detail},
        include_history=include_history,
    )


def row_direction(
    row: dict[str, Any], listed: str, detail: dict[str, Any] | None = None
) -> str:
    """The direction of one parcel: the detail's flag, else the row's, else its list.

    The only response seen on the wire carries ``isOutgoingParcel`` on the
    detail's ``result``, beside ``parcel`` rather than inside it, so the detail
    is asked first and the inbox row second. The list a parcel arrived in is a
    third, independent signal, so a flag that disagrees with the list is
    reported once (the field name only) rather than silently resolved. A flag
    that is neither true nor false matches neither side and counts as
    incoming, also with one warning.
    """
    flag = _result_of(detail).get("isOutgoingParcel")
    if flag is None:
        flag = row.get("isOutgoingParcel")
    if flag is None:
        warn_once(
            "direction-missing",
            "neither a parcel's detail nor its inbox row has "
            "isOutgoingParcel; the direction is taken from the list it "
            "arrived in",
        )
        return listed
    if not isinstance(flag, bool):
        warn_once(
            "direction-unrecognised",
            "a parcel's isOutgoingParcel is neither true nor false; it "
            "counts as incoming",
        )
        return DIRECTION_INCOMING
    direction = DIRECTION_OUTGOING if flag else DIRECTION_INCOMING
    if direction != listed:
        warn_once(
            "direction-contradiction",
            "a parcel's isOutgoingParcel contradicts the list it arrived in; "
            "the field is used",
        )
    return direction
