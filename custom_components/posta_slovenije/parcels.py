"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. Both sources (``tracking/`` and
``account/``) return the same per-scan rows, so the status map and the scan-row
normalizer live here once and each source only shapes what surrounds them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

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

_ZONE = ZoneInfo("Europe/Ljubljana")


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
# These collect or pay for a parcel, so they never reach ``raw``.
_PASS_FIELDS = frozenset({"pickupQRCode", "pickupPINCode", "payByLinkUrl"})

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
    the sender is ``returning`` (any other ``V`` is a ``problem``), and once a
    return has started (that ``V``, or a ``VRP``) a delivery or pickup-point
    scan belongs to the return leg and is ``returning``, not ``delivered`` or
    ``at_pickup_point``.
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
        elif returning and code in _PICKUP_CODES:
            status = ParcelStatus.RETURNING
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
        if status is ParcelStatus.RETURNING:
            returning = True
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


def _without_pass_fields(value: Any) -> Any:
    """Copy a payload without its pass fields, at any depth."""
    if isinstance(value, dict):
        return {
            key: _without_pass_fields(item)
            for key, item in value.items()
            if key not in _PASS_FIELDS
        }
    if isinstance(value, list):
        return [_without_pass_fields(item) for item in value]
    return value


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

    pickup = is_pickup(None, status, derived)
    pickup_point = None
    if pickup:
        pickup_row = next(
            (
                row
                for (_, row), value in zip(
                    reversed(rows), reversed(derived), strict=True
                )
                if row.get("code") in _PICKUP_CODES
                and value is ParcelStatus.AT_PICKUP_POINT
            ),
            None,
        )
        pickup_point = _text(pickup_row.get("postalName")) if pickup_row else None
    planned_from, planned_to = planned_for_status(
        status, *planned_window(None, None, zone=_ZONE)
    )

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
        "contents": None,
        "status": status,
        "raw_status": _raw_status(rows[-1][1]) if rows else None,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": _without_pass_fields(raw),
    }


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


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
