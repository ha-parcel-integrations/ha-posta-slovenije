"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. Both sources (``tracking/`` and
``account/``) return the same per-scan rows, so the status map and the scan-row
normalizer live here once and each source only shapes what surrounds them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_SOURCE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_DIRECTION,
    HISTORY_MAX_EVENTS,
    SOURCE_TRACKING,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

CARRIER = "Pošta Slovenije"


def source_of(entry: ConfigEntry) -> str:
    """The entry's source; an entry without one is a tracking hub."""
    return entry.data.get(CONF_SOURCE, SOURCE_TRACKING)


# Where users report something we do not recognise. The ``?template=``
# parameter matters: without it the link opens a blank form, and the report
# comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-posta-slovenije/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the scan ``code``. Never on a row's ``statusId``/``statusName`` (a
# parcel returned to its sender is "Dostavljena" there) and never on the
# parcel-level summary, which lags the history. ``V`` and the delivery scans
# are not here: their status depends on the rows before them (see
# :func:`_row_statuses`).
_STATUS_MAP: dict[str, ParcelStatus] = {
    "NAJ": ParcelStatus.REGISTERED,
    "S": ParcelStatus.IN_TRANSIT,
    "EMC": ParcelStatus.IN_TRANSIT,
    "N": ParcelStatus.OUT_FOR_DELIVERY,
    "VLPAKP": ParcelStatus.OUT_FOR_DELIVERY,
    "IZR": ParcelStatus.AT_PICKUP_POINT,
    "PVI": ParcelStatus.AT_PICKUP_POINT,
    "VDO": ParcelStatus.PROBLEM,
    "POTPKTM": ParcelStatus.PROBLEM,
    "VRP": ParcelStatus.RETURNING,
}

_PICKUP_CODES = frozenset({"IZR", "PVI"})

# In the map on third-party-adapter evidence only: no parcel here has ever
# carried one. They map as well as we can guess, and they also report
# themselves once, so the first real one tells us whether the guess holds.
_UNCONFIRMED_CODES = frozenset({"EMC", "VLPAKP", "IZR", "POTPKTM"})

# The scans that hand the item over for good. ``VL`` drops it in the mailbox,
# ``VR`` hands it to someone in person; both carry subCode ``VRPIS`` and the
# *Dostavljena* bucket, and both mean the same thing to us.
_DELIVERY_CODES = frozenset({"VL", "VR"})

# A failed attempt whose next step sends the item back starts a return.
_RETURN_WORDING = re.compile(r"returned to (the )?sender", re.IGNORECASE)

# Keys seen on the wire. Anything else is reported once, as keys only, so a
# change in the payload reaches us without anyone having to share diagnostics.
KNOWN_TOP_KEYS = frozenset(
    {
        "result",
        "errorCode",
        "errorMessage",
        "invalidField",
        "timeGenerated",
        "traceId",
    }
)
# Confirmed on the wire 2026-10-25, on a real (empty) account inbox.
KNOWN_LIST_KEYS = frozenset(
    {"items", "totalCount", "pageNumber", "pageSize", "totalPages"}
)
KNOWN_RESULT_KEYS = frozenset(
    {
        "parcel",
        "statuses",
        "inMyParcels",
        "isOutgoingParcel",
        "availableDeliveryTypes",
    }
)
KNOWN_PARCEL_KEYS = frozenset(
    {
        "parcelId",
        "trackingNumber",
        "statusId",
        "currentStatusDateTime",
        "parcelType",
        "parcelDestinations",
        "eligibleForRedirect",
        "createdDate",
        "isArchived",
        "codeDescriptionSI",
        "codeDescriptionEN",
        "pickupQRCode",
        "pickupPINCode",
        "source",
        "payByLinkUrl",
        "pickupUnitAddress",
        "inMyParcels",
        "isOutgoingParcel",
        "statuses",
    }
)
KNOWN_ROW_KEYS = frozenset(
    {
        "postalId",
        "postalName",
        "code",
        "subCode",
        "codeDescriptionSI",
        "codeDetailedDescriptionSI",
        "codeDescriptionEN",
        "codeDetailedDescriptionEN",
        "nondeliveryId",
        "nondeliveryReasonSI",
        "nondeliveryActionSI",
        "nondeliveryReasonEN",
        "nondeliveryActionEN",
        "recipient",
        "signatory",
        "note",
        "canceled",
        "statusId",
        "statusName",
        "statusDateTime",
        "pickupAddress",
        "unitType",
        "unitId",
    }
)
_PARTY_PREFIXES = ("sender", "receiver")

# Warnings already logged this session, keyed on what they are about, so each
# unknown is reported once instead of on every poll.
_warned: set[str] = set()


def warn_once(kind: str, message: str, *args: object) -> None:
    """Log ``message`` at WARNING the first time ``kind`` is seen, with the issue link."""
    if kind in _warned:
        return
    _warned.add(kind)
    _LOGGER.warning(
        "Pošta Slovenije: %s. Open an issue and paste this line: %s",
        message % args if args else message,
        NEW_ISSUE_URL,
    )


def check_keys(level: str, payload: Any, known: frozenset[str]) -> None:
    """Warn once about each key of ``payload`` we have not seen before.

    Names the keys only, never their values. ``sender*`` / ``receiver*`` keys
    are accepted on a parcel as a family.
    """
    if not isinstance(payload, dict):
        return
    for key in payload:
        if key in known:
            continue
        if level == "parcel" and str(key).startswith(_PARTY_PREFIXES):
            continue
        warn_once(f"key:{level}:{key}", "unrecognised %s field '%s'", level, key)


def parse_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _text(value: Any) -> str | None:
    """A non-empty stripped string, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _timed_rows(statuses: Any) -> list[tuple[datetime, dict[str, Any]]]:
    """The scan rows with a readable clock, oldest first (stable)."""
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in statuses if isinstance(statuses, list) else []:
        if not isinstance(row, dict):
            continue
        check_keys("scan", row, KNOWN_ROW_KEYS)
        parsed = parse_iso(row.get("statusDateTime"))
        if parsed is None:
            warn_once(
                f"clock:{row.get('code')}",
                "a '%s' scan has an unreadable statusDateTime and was skipped",
                row.get("code"),
            )
            continue
        canceled = row.get("canceled")
        if canceled not in (None, 0, False):
            warn_once(
                f"canceled:{row.get('code')}",
                "a '%s' scan has a non-zero 'canceled' (%s)",
                row.get("code"),
                canceled,
            )
        rows.append((parsed, row))
    rows.sort(key=lambda item: item[0])
    return rows


def _row_statuses(
    rows: list[tuple[datetime, dict[str, Any]]],
) -> list[ParcelStatus | None]:
    """Derive each row's status, oldest to newest.

    Two rules depend on sequence: a ``V`` whose next step returns the item to
    the sender is ``returning`` (any other ``V`` is a ``problem``), and every
    a delivery scan after a returning ``V`` is ``returning`` rather than
    ``delivered``.
    A row whose code is not mapped yields ``None``.
    """
    statuses: list[ParcelStatus | None] = []
    returning = False
    for _, row in rows:
        code = str(row.get("code") or "")
        status: ParcelStatus | None
        if code == "V":
            action = _text(row.get("nondeliveryActionEN"))
            if action and _RETURN_WORDING.search(action):
                returning = True
                status = ParcelStatus.RETURNING
            else:
                # The carrier fills its English fields unevenly, so an empty
                # one would otherwise turn every return into a problem in
                # silence.
                warn_once(
                    f"nondelivery:{'empty' if action is None else 'unmatched'}",
                    "a 'V' scan's nondeliveryActionEN is %s (nondeliveryId "
                    "%s), so the parcel counts as a problem rather than a "
                    "return",
                    "empty" if action is None else "not about a return",
                    row.get("nondeliveryId"),
                )
                status = ParcelStatus.PROBLEM
        elif code in _DELIVERY_CODES:
            status = ParcelStatus.RETURNING if returning else ParcelStatus.DELIVERED
        else:
            status = _STATUS_MAP.get(code)
            if status is not None and code in _UNCONFIRMED_CODES:
                warn_once(
                    f"unconfirmed:{code}",
                    "status code '%s' (%s) has never been seen on a real "
                    "parcel here and was read as '%s'",
                    code,
                    _text(row.get("codeDescriptionEN")) or "no description",
                    status.value,
                )
        if status is None:
            warn_once(
                f"status:{code}",
                "unrecognised status code '%s' (%s) reported as unknown or "
                "taken from an earlier scan",
                code or "<none>",
                _text(row.get("codeDescriptionEN")) or "no description",
            )
        statuses.append(status)
    return statuses


def _raw_status(row: dict[str, Any]) -> str | None:
    return _text(row.get("codeDescriptionEN")) or _text(row.get("codeDescriptionSI"))


def _pickup_point(row: dict[str, Any]) -> str | None:
    """The post office and its address, from a pickup scan."""
    parts = [_text(row.get("postalName")), _text(row.get("pickupAddress"))]
    return ", ".join(part for part in parts if part) or None


def normalize_scans(
    *,
    barcode: str,
    statuses: Any,
    sender: str | None,
    receiver: str | None,
    raw: Any,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return a carrier-agnostic parcel dict built from the per-scan rows.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these. The parcel-level summary is never read: it
    lags the history. Neither source reports an ETA, weight or dimensions.
    """
    rows = _timed_rows(statuses)
    derived = _row_statuses(rows)

    # No scans yet is a parcel the carrier knows about but has not handled;
    # scans we could not read or map are a different matter.
    status = (
        ParcelStatus.UNKNOWN
        if isinstance(statuses, list) and statuses
        else ParcelStatus.REGISTERED
    )
    for value in reversed(derived):
        if value is not None:
            status = value
            break

    delivered = status is ParcelStatus.DELIVERED
    delivered_at = None
    if delivered:
        delivered_at = next(
            (
                moment.astimezone(UTC).isoformat()
                for (moment, row) in reversed(rows)
                if row.get("code") in _DELIVERY_CODES
            ),
            None,
        )

    pickup_point = None
    if status is ParcelStatus.AT_PICKUP_POINT:
        pickup_row = next(
            (row for _, row in reversed(rows) if row.get("code") in _PICKUP_CODES),
            None,
        )
        pickup_point = _pickup_point(pickup_row) if pickup_row else None
    elif delivered:
        # Collected at the post office: the pickup scan is the last one before
        # the delivery scans.
        last_other = next(
            (
                row
                for _, row in reversed(rows)
                if row.get("code") not in _DELIVERY_CODES
            ),
            None,
        )
        if last_other is not None and last_other.get("code") in _PICKUP_CODES:
            pickup_point = _pickup_point(last_other)

    history = None
    if include_history:
        history = [
            {
                "timestamp": moment.astimezone(UTC).isoformat(),
                "status": derived[index],
                "raw_status": _raw_status(row) or _text(row.get("code")),
            }
            for index, (moment, row) in enumerate(rows)
        ][-HISTORY_MAX_EVENTS:]

    return {
        "carrier": CARRIER,
        "barcode": barcode,
        "sender": sender,
        "receiver": receiver,
        "status": status,
        "raw_status": _raw_status(rows[-1][1]) if rows else None,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]


def tracked_direction(item: dict) -> str:
    """Return the declared direction of one tracked-parcel options entry.

    Entries stored before the option existed carry no ``direction`` key, so a
    missing or empty value reads as the default (incoming) rather than needing
    a migration.
    """
    return item.get(CONF_DIRECTION) or DEFAULT_DIRECTION
