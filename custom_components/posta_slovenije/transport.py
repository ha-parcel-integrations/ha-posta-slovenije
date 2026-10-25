"""HTTP helpers both sources share: capped reads and edge-block detection."""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from .parcels import warn_once

MAX_BODY_BYTES = 512 * 1024
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

_BLOCK_PAGE = re.compile(
    r"Just a moment|Access Denied|Attention Required|cf-turnstile|_cf_chl_opt"
    r"|Azure Front Door|Request blocked",
    re.IGNORECASE,
)


class BodyTooLargeError(Exception):
    """The response grew past :data:`MAX_BODY_BYTES`."""


def parse_retry_after(value: str | None) -> float | None:
    """Return ``Retry-After`` as seconds, or ``None`` for absent or an HTTP-date."""
    try:
        return float(value) if value else None
    except ValueError:
        return None


async def read_capped(response: aiohttp.ClientResponse) -> bytes:
    """Read the body, refusing anything larger than :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(16 * 1024):
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise BodyTooLargeError
    return bytes(body)


def is_block_page(body: bytes) -> bool:
    """Whether a non-JSON body is an edge block page rather than a schema change."""
    return bool(_BLOCK_PAGE.search(body[:4096].decode("utf-8", errors="replace")))


class PostaSlovenijeApiError(Exception):
    """Raised when a Pošta Slovenije API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Pošta Slovenije API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class PostaSlovenijeGuardError(PostaSlovenijeApiError):
    """Raised when the service refuses this client, whatever the number.

    Not a statement about one parcel, so it must never be read as not-found.
    """


def parse_json(body: bytes) -> Any:
    """Decode a JSON body, or ``None`` when it is not JSON."""
    try:
        return json.loads(body)
    except ValueError:
        return None


def error_code(payload: Any) -> str | None:
    """The service's own ``errorCode`` from a JSON error envelope."""
    if isinstance(payload, dict) and isinstance(payload.get("errorCode"), str):
        return payload["errorCode"]
    return None


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """GET ``url`` and return ``(status, parsed JSON or None)``.

    Redirects are never followed. Raises :class:`PostaSlovenijeApiError` for a
    429 (with ``retry_after``), a redirect, an oversized body, a timeout and an
    edge block page, and :class:`PostaSlovenijeGuardError` when the service's
    client guard refuses the request. Anything else is the caller's to
    classify. ``aiohttp.ClientError`` propagates untouched.
    """
    try:
        async with session.get(
            url,
            headers=headers,
            params=params,
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            status = response.status
            if status == 429:
                raise PostaSlovenijeApiError(
                    "HTTP 429",
                    status_code=429,
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                )
            if 300 <= status < 400:
                raise PostaSlovenijeApiError(
                    f"HTTP {status} (redirect)", status_code=status
                )
            body = await read_capped(response)
    except BodyTooLargeError as err:
        raise PostaSlovenijeApiError("response too large") from err
    except TimeoutError as err:
        raise PostaSlovenijeApiError("timeout") from err

    payload = parse_json(body)
    if payload is None and is_block_page(body):
        warn_once(
            "block-page",
            "the service answered with an edge block page (HTTP %s), so "
            "requests are being refused",
            status,
        )
        raise PostaSlovenijeApiError(f"blocked by an edge page (HTTP {status})")
    if status == 403 and error_code(payload) == "error.not.allowed":
        warn_once(
            "guard",
            "the service refused this client (error.not.allowed); parcels keep "
            "their last known status",
        )
        raise PostaSlovenijeGuardError("HTTP 403", status_code=403)
    return status, payload
