"""MojaPošta account client: the signed-in inbox and per-parcel detail.

The contract the coordinator relies on:

* ``async_prepare`` makes sure a usable access token exists before a poll, so
  one poll uses one token,
* ``async_get_inbox`` returns every row of one list (incoming or outgoing),
  paged until the service's ``totalCount`` is reached,
* ``async_get_parcel`` returns one parcel's detail, or ``None`` when its
  ``parcelId`` no longer resolves,
* a ``401`` is answered with one refresh, then one fresh sign-in from the
  stored password; if that still fails, :class:`PostaSlovenijeAccountAuthError`
  says the user has to reauthenticate,
* every other failure is a :class:`PostaSlovenijeApiError` (a 429 carries
  ``retry_after``) and ``aiohttp.ClientError`` propagates untouched.

Nothing here logs a username, password or token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from ..const import (
    DIRECTION_INCOMING,
    INBOX_PAGE_SIZE,
    INBOX_PARAM_BY_DIRECTION,
    PARCELS_URL,
    USER_AGENT,
)
from ..parcels import (
    KNOWN_LIST_KEYS,
    KNOWN_PARCEL_KEYS,
    KNOWN_RESULT_KEYS,
    KNOWN_TOP_KEYS,
    check_keys,
    warn_once,
)
from ..transport import PostaSlovenijeApiError, get_json
from .auth import (
    PostaSlovenijeAuthError,
    PostaSlovenijeInvalidCredentials,
    PostaSlovenijeRefreshRejected,
    PostaSlovenijeTokens,
    async_password_grant,
    async_refresh_grant,
)

# Renew a token this long before it expires.
REFRESH_MARGIN_SECONDS = 60
# An inbox longer than this many pages is treated as a fault, not a mailbox.
MAX_INBOX_PAGES = 50


class PostaSlovenijeAccountAuthError(PostaSlovenijeApiError):
    """The stored password no longer signs in: the user has to reauthenticate."""


class PostaSlovenijeAccountClient:
    """Signed-in calls to MojaPošta on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        username: str,
        password: str,
        tokens: PostaSlovenijeTokens | None = None,
        on_tokens: Callable[[PostaSlovenijeTokens], None] | None = None,
    ) -> None:
        """Store the credentials and any token pair from a previous run."""
        self._session = session
        self._username = username
        self._password = password
        self._tokens = tokens
        self._on_tokens = on_tokens
        self._lock = asyncio.Lock()

    # -- tokens ---------------------------------------------------------

    def _store(self, tokens: PostaSlovenijeTokens) -> str:
        self._tokens = tokens
        if self._on_tokens is not None:
            self._on_tokens(tokens)
        return tokens.access_token

    async def _async_login(self) -> str:
        """Sign in with the stored password."""
        try:
            return self._store(
                await async_password_grant(
                    self._session, self._username, self._password
                )
            )
        except PostaSlovenijeInvalidCredentials as err:
            raise PostaSlovenijeAccountAuthError("password rejected") from err
        except PostaSlovenijeAuthError as err:
            raise PostaSlovenijeApiError(f"sign-in failed ({err})") from err

    async def _async_renew(self, *, refresh: bool) -> tuple[str, bool]:
        """Get a new access token; return it and whether the password was used.

        A refresh is tried first when asked for and possible; a rejected one
        falls back to the password.
        """
        async with self._lock:
            tokens = self._tokens
            if refresh and tokens is not None and tokens.refresh_token:
                try:
                    return (
                        self._store(
                            await async_refresh_grant(
                                self._session, tokens.refresh_token
                            )
                        ),
                        False,
                    )
                except PostaSlovenijeRefreshRejected:
                    pass
                except PostaSlovenijeAuthError as err:
                    raise PostaSlovenijeApiError(
                        f"token refresh failed ({err})"
                    ) from err
            return await self._async_login(), True

    async def async_sign_in(self) -> None:
        """Sign in from scratch; used by the config flow to check the input."""
        await self._async_login()

    async def async_prepare(self) -> None:
        """Make sure a usable access token exists, renewing it near expiry."""
        tokens = self._tokens
        if (
            tokens is not None
            and tokens.expires_at - REFRESH_MARGIN_SECONDS > time.time()
        ):
            return
        await self._async_renew(refresh=True)

    # -- requests -------------------------------------------------------

    async def _async_get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """GET a signed-in route, renewing the token on a ``401``.

        The first ``401`` refreshes (falling back to the password); a second
        signs in again with the password; a third means the account no longer
        works and the user has to reauthenticate.
        """
        if self._tokens is None:
            await self._async_renew(refresh=False)
        assert self._tokens is not None
        token = self._tokens.access_token
        stage = 0
        while True:
            status, payload = await get_json(
                self._session,
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Bearer {token}",
                },
            )
            if status != 401:
                return status, payload
            if stage >= 2:
                raise PostaSlovenijeAccountAuthError("token rejected after signing in")
            token, used_password = await self._async_renew(refresh=stage == 0)
            stage = 2 if used_password else stage + 1

    async def async_get_inbox(
        self, direction: str = DIRECTION_INCOMING
    ) -> list[dict[str, Any]]:
        """Every row of the ``incoming`` or ``outgoing`` list, paged to ``totalCount``."""
        param = INBOX_PARAM_BY_DIRECTION[direction]
        rows: dict[str, dict[str, Any]] = {}
        skipped = 0
        page = 1
        last_page: int | None = None
        while True:
            status, payload = await self._async_get(
                PARCELS_URL,
                {"page": page, "pageSize": INBOX_PAGE_SIZE, param: "true"},
            )
            result = _checked_result(status, payload)
            check_keys("inbox", result, KNOWN_LIST_KEYS)
            items = result.get("items")
            total = result.get("totalCount")
            if not isinstance(items, list) or not isinstance(total, int):
                raise PostaSlovenijeApiError("unexpected inbox shape")
            echoed = result.get("pageNumber")
            if isinstance(echoed, int) and echoed != page:
                warn_once(
                    "inbox-page-echo",
                    "the inbox answered page %s for a request for page %s",
                    echoed,
                    page,
                )
            reported = result.get("totalPages")
            if isinstance(reported, int) and reported > 0:
                last_page = reported
            for item in items:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                check_keys("parcel", item, KNOWN_PARCEL_KEYS)
                parcel_id = item.get("parcelId")
                if parcel_id is None or not item.get("trackingNumber"):
                    skipped += 1
                    warn_once(
                        "inbox-row-identity",
                        "an inbox row has no parcelId or trackingNumber and was skipped",
                    )
                    continue
                rows.setdefault(str(parcel_id), item)
            # Counted on what we actually hold, not on rows fetched: repeated
            # or overlapping pages would otherwise satisfy this with a short
            # list.
            if len(rows) + skipped >= total:
                return list(rows.values())
            if not items or (last_page is not None and page >= last_page):
                # A partial inbox would drop parcels and announce them as new
                # once the rest is back.
                raise PostaSlovenijeApiError("inbox ended before totalCount")
            page += 1
            if page > MAX_INBOX_PAGES:
                raise PostaSlovenijeApiError("inbox has too many pages")

    async def async_get_parcel(self, parcel_id: Any) -> dict[str, Any] | None:
        """One parcel's detail, or ``None`` when its ``parcelId`` is gone."""
        status, payload = await self._async_get(
            f"{PARCELS_URL}/{quote(str(parcel_id), safe='')}"
        )
        if status == 404:
            return None
        result = _checked_result(status, payload)
        check_keys("result", result, KNOWN_RESULT_KEYS)
        parcel = result.get("parcel")
        if parcel is not None:
            check_keys("parcel", parcel, KNOWN_PARCEL_KEYS)
        return payload


def _checked_result(status: int, payload: Any) -> dict[str, Any]:
    """Validate a 200 envelope and return its ``result``."""
    if status != 200:
        raise PostaSlovenijeApiError(f"HTTP {status}", status_code=status)
    if not isinstance(payload, dict):
        raise PostaSlovenijeApiError("unparseable body")
    check_keys("response", payload, KNOWN_TOP_KEYS)
    result = payload.get("result")
    if payload.get("errorCode") or not isinstance(result, dict):
        raise PostaSlovenijeApiError("unexpected envelope")
    return result
