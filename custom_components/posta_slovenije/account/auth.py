"""MojaPošta sign-in: Keycloak's direct access grant and the token refresh.

A username and password exchange for a token pair with no browser redirect.
Nothing here logs a username, password or token, at any level.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, NoReturn

import aiohttp

from ..const import OAUTH_CLIENT_ID, OAUTH_SCOPE, TOKEN_URL
from ..parcels import warn_once
from ..transport import REQUEST_TIMEOUT, BodyTooLargeError, parse_json, read_capped

# The lifetime to assume when the token response does not say.
DEFAULT_EXPIRES_IN = 300


class PostaSlovenijeAuthError(Exception):
    """The token endpoint failed for a reason other than the user's credentials."""


class PostaSlovenijeInvalidCredentials(PostaSlovenijeAuthError):
    """MojaPošta rejected the username or password."""


class PostaSlovenijeRefreshRejected(PostaSlovenijeAuthError):
    """The refresh token no longer works; sign in again with the password."""


@dataclass
class PostaSlovenijeTokens:
    """An access token, its optional refresh token and when it expires."""

    access_token: str
    refresh_token: str | None
    expires_at: float


async def _post_token(
    session: aiohttp.ClientSession, form: dict[str, str]
) -> tuple[int, Any]:
    """POST a form to the token endpoint and return ``(status, parsed JSON)``."""
    try:
        async with session.post(
            TOKEN_URL,
            data=form,
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        ) as response:
            body = await read_capped(response)
            status = response.status
    except BodyTooLargeError as err:
        raise PostaSlovenijeAuthError("response too large") from err
    except TimeoutError as err:
        raise PostaSlovenijeAuthError("timeout") from err
    return status, parse_json(body)


def _tokens_from(payload: Any) -> PostaSlovenijeTokens:
    """Read a successful token response."""
    if not isinstance(payload, dict) or not isinstance(
        payload.get("access_token"), str
    ):
        raise PostaSlovenijeAuthError("token response without an access token")
    refresh = payload.get("refresh_token")
    try:
        expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN)
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRES_IN
    return PostaSlovenijeTokens(
        access_token=payload["access_token"],
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=time.time() + expires_in,
    )


def _is_credential_failure(status: int, payload: Any) -> bool:
    """Whether an error response says the username or password is wrong."""
    if status not in (400, 401) or not isinstance(payload, dict):
        return False
    description = str(payload.get("error_description") or "").lower()
    return payload.get("error") == "invalid_grant" and "credentials" in description


def _raise_for_error(status: int, payload: Any) -> NoReturn:
    """Classify a token-endpoint failure."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if error == "unauthorized_client":
        warn_once(
            "unauthorized_client",
            "the sign-in service no longer accepts this app's login (%s); "
            "parcels keep their last known status",
            error,
        )
    else:
        warn_once(
            f"token-error:{error}:{status}",
            "unexpected answer from the sign-in service (HTTP %s, error=%s)",
            status,
            error if isinstance(error, str) else "none",
        )
    raise PostaSlovenijeAuthError(f"HTTP {status}")


async def async_password_grant(
    session: aiohttp.ClientSession, username: str, password: str
) -> PostaSlovenijeTokens:
    """Sign in with the user's own username and password."""
    scope = OAUTH_SCOPE
    while True:
        status, payload = await _post_token(
            session,
            {
                "grant_type": "password",
                "client_id": OAUTH_CLIENT_ID,
                "username": username,
                "password": password,
                "scope": scope,
            },
        )
        if status == 200:
            tokens = _tokens_from(payload)
            if tokens.refresh_token is None and "offline_access" in scope:
                warn_once(
                    "no-refresh-token",
                    "the sign-in service issued no refresh token; the password "
                    "is used to sign in again each time the session ends",
                )
            return tokens
        if _is_credential_failure(status, payload):
            raise PostaSlovenijeInvalidCredentials("credentials rejected")
        if (
            isinstance(payload, dict)
            and payload.get("error") == "invalid_scope"
            and "offline_access" in scope
        ):
            # offline_access is not granted; the access token is then the whole session.
            scope = " ".join(part for part in scope.split() if part != "offline_access")
            continue
        _raise_for_error(status, payload)


async def async_refresh_grant(
    session: aiohttp.ClientSession, refresh_token: str
) -> PostaSlovenijeTokens:
    """Exchange a refresh token for a new token pair."""
    status, payload = await _post_token(
        session,
        {
            "grant_type": "refresh_token",
            "client_id": OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if status == 200:
        tokens = _tokens_from(payload)
        # A refresh may omit a rotated token; keep the one we used.
        if tokens.refresh_token is None:
            tokens.refresh_token = refresh_token
        return tokens
    if (
        status in (400, 401)
        and isinstance(payload, dict)
        and payload.get("error") == "invalid_grant"
    ):
        raise PostaSlovenijeRefreshRejected("refresh token rejected")
    _raise_for_error(status, payload)
