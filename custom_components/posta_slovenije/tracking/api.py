"""Pošta Slovenije guest lookup client.

The contract the coordinator relies on:

* ``async_get_parcel`` returns the full response dict for a found parcel,
* returns ``None`` when the carrier says the number is unknown (an old record
  ages out, and a new one is not there yet) — a normal state, never an error,
* raises :class:`PostaSlovenijeGuardError` when the client guard refuses the
  request, which says nothing about the parcel,
* raises :class:`PostaSlovenijeApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when a 429
  carries seconds,
* lets ``aiohttp.ClientError`` propagate untouched.

No ``Content-Type`` is sent: the service then reads an empty JSON body and
answers 400. Redirects are never followed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import aiohttp

from ..const import TRACKING_API_URL, USER_AGENT
from ..parcels import (
    KNOWN_PARCEL_KEYS,
    KNOWN_RESULT_KEYS,
    KNOWN_TOP_KEYS,
    check_keys,
    warn_once,
)
from ..transport import (
    PostaSlovenijeApiError,
    PostaSlovenijeGuardError,
    error_code,
    get_json,
)

HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}

__all__ = [
    "PostaSlovenijeApiClient",
    "PostaSlovenijeApiError",
    "PostaSlovenijeGuardError",
]


class PostaSlovenijeApiClient:
    """Client for the keyless Pošta Slovenije guest lookup."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's scans, or ``None`` when the number is not known."""
        url = f"{TRACKING_API_URL}?TrackingNumber={quote(tracking_code, safe='')}"
        status, payload = await get_json(self._session, url, headers=HEADERS)

        if (
            status == 404
            and error_code(payload) == "error.not.found"
            and tracking_code in str(payload.get("errorMessage") or "")
        ):
            return None
        if status != 200:
            raise PostaSlovenijeApiError(f"HTTP {status}", status_code=status)
        if not isinstance(payload, dict):
            raise PostaSlovenijeApiError("unparseable body")
        return _checked_result(payload, tracking_code)


def _checked_result(payload: dict[str, Any], tracking_code: str) -> dict[str, Any]:
    """Validate a 200 envelope and return it whole."""
    check_keys("response", payload, KNOWN_TOP_KEYS)
    result = payload.get("result")
    if payload.get("errorCode") or not isinstance(result, dict):
        raise PostaSlovenijeApiError("unexpected envelope")
    check_keys("result", result, KNOWN_RESULT_KEYS)
    parcel = result.get("parcel")
    if not isinstance(parcel, dict):
        raise PostaSlovenijeApiError("unexpected envelope (no parcel)")
    check_keys("parcel", parcel, KNOWN_PARCEL_KEYS)
    answered = str(parcel.get("trackingNumber") or "").strip().upper()
    if answered != tracking_code:
        warn_once(
            "mismatch",
            "the service answered a lookup with a different tracking number, "
            "so the answer was ignored",
        )
        raise PostaSlovenijeApiError("reply is for a different parcel")
    return payload
