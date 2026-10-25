"""Tests for the Keycloak direct access grant and the refresh."""

import logging
import time

import pytest

from custom_components.posta_slovenije.account import auth
from custom_components.posta_slovenije.account.auth import (
    PostaSlovenijeAuthError,
    PostaSlovenijeInvalidCredentials,
    PostaSlovenijeRefreshRejected,
    async_password_grant,
    async_refresh_grant,
)
from custom_components.posta_slovenije.const import OAUTH_CLIENT_ID, TOKEN_URL
from tests.fakes import FakeResponse, FakeSession
from tests.payloads import token_response

BAD_PASSWORD = {
    "error": "invalid_grant",
    "error_description": "Invalid user credentials",
}


async def test_password_grant_sends_the_documented_form():
    session = FakeSession(FakeResponse(200, token_response()))
    tokens = await async_password_grant(session, "someone", "hunter2")
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", TOKEN_URL)
    assert kwargs["data"] == {
        "grant_type": "password",
        "client_id": OAUTH_CLIENT_ID,
        "username": "someone",
        "password": "hunter2",
        "scope": "openid profile email offline_access",
    }
    assert kwargs["allow_redirects"] is False
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_at == pytest.approx(time.time() + 300, abs=5)


@pytest.mark.parametrize("status", [400, 401])
async def test_bad_credentials(status):
    session = FakeSession(FakeResponse(status, BAD_PASSWORD))
    with pytest.raises(PostaSlovenijeInvalidCredentials):
        await async_password_grant(session, "someone", "wrong")


async def test_other_invalid_grant_is_not_a_credential_failure(caplog):
    body = {"error": "invalid_grant", "error_description": "Account disabled"}
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(PostaSlovenijeAuthError) as err,
    ):
        await async_password_grant(
            FakeSession(FakeResponse(400, body)), "someone", "pw"
        )
    assert not isinstance(err.value, PostaSlovenijeInvalidCredentials)
    assert "error=invalid_grant" in caplog.text


async def test_unauthorized_client_is_a_transport_error_with_one_warning(caplog):
    body = {"error": "unauthorized_client", "error_description": "Client not allowed"}
    session = FakeSession(FakeResponse(400, body), FakeResponse(400, body))
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(PostaSlovenijeAuthError) as err:
                await async_password_grant(session, "someone", "pw")
            assert not isinstance(err.value, PostaSlovenijeInvalidCredentials)
    assert sum("no longer accepts" in r.getMessage() for r in caplog.records) == 1


async def test_unexpected_error_warns_once_with_the_code_only(caplog):
    body = {"error": "server_error", "error_description": "secret detail"}
    with caplog.at_level(logging.WARNING), pytest.raises(PostaSlovenijeAuthError):
        await async_password_grant(
            FakeSession(FakeResponse(500, body)), "someone", "pw"
        )
    assert "server_error" in caplog.text
    assert "secret detail" not in caplog.text


async def test_non_json_error_is_a_transport_error():
    with pytest.raises(PostaSlovenijeAuthError):
        await async_password_grant(FakeSession(FakeResponse(502, b"<html>")), "u", "p")


async def test_invalid_scope_retries_without_offline_access():
    session = FakeSession(
        FakeResponse(400, {"error": "invalid_scope"}),
        FakeResponse(200, token_response(refresh=None)),
    )
    tokens = await async_password_grant(session, "someone", "pw")
    assert session.calls[1][2]["data"]["scope"] == "openid profile email"
    assert tokens.refresh_token is None


async def test_missing_refresh_token_warns_once(caplog):
    session = FakeSession(
        FakeResponse(200, token_response(refresh=None)),
        FakeResponse(200, token_response(refresh=None)),
    )
    with caplog.at_level(logging.WARNING):
        await async_password_grant(session, "u", "p")
        await async_password_grant(session, "u", "p")
    assert sum("no refresh token" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    "body",
    [{"expires_in": 5}, [], {"access_token": 5}],
)
async def test_success_without_an_access_token_is_an_error(body):
    with pytest.raises(PostaSlovenijeAuthError, match="access token"):
        await async_password_grant(FakeSession(FakeResponse(200, body)), "u", "p")


async def test_unreadable_expiry_falls_back_to_the_default():
    body = {**token_response(), "expires_in": "soon"}
    tokens = await async_password_grant(FakeSession(FakeResponse(200, body)), "u", "p")
    assert tokens.expires_at == pytest.approx(
        time.time() + auth.DEFAULT_EXPIRES_IN, abs=5
    )


async def test_refresh_grant_form_and_rotation():
    session = FakeSession(FakeResponse(200, token_response("access-2", "refresh-2")))
    tokens = await async_refresh_grant(session, "refresh-1")
    assert session.calls[0][2]["data"] == {
        "grant_type": "refresh_token",
        "client_id": OAUTH_CLIENT_ID,
        "refresh_token": "refresh-1",
    }
    assert (tokens.access_token, tokens.refresh_token) == ("access-2", "refresh-2")


async def test_refresh_keeps_the_old_token_when_none_is_returned():
    session = FakeSession(FakeResponse(200, token_response("access-2", None)))
    assert (
        await async_refresh_grant(session, "refresh-1")
    ).refresh_token == "refresh-1"


async def test_rejected_refresh_token():
    body = {"error": "invalid_grant", "error_description": "Token is not active"}
    with pytest.raises(PostaSlovenijeRefreshRejected):
        await async_refresh_grant(FakeSession(FakeResponse(400, body)), "refresh-1")


async def test_refresh_outage_is_a_transport_error():
    with pytest.raises(PostaSlovenijeAuthError) as err:
        await async_refresh_grant(FakeSession(FakeResponse(503, b"")), "refresh-1")
    assert not isinstance(err.value, PostaSlovenijeRefreshRejected)


async def test_timeout_and_oversize_are_transport_errors(monkeypatch):
    with pytest.raises(PostaSlovenijeAuthError, match="timeout"):
        await async_password_grant(FakeSession(TimeoutError()), "u", "p")
    monkeypatch.setattr("custom_components.posta_slovenije.transport.MAX_BODY_BYTES", 5)
    with pytest.raises(PostaSlovenijeAuthError, match="too large"):
        await async_password_grant(FakeSession(FakeResponse(200, b"x" * 50)), "u", "p")


async def test_secrets_are_never_logged(caplog):
    session = FakeSession(FakeResponse(200, token_response()))
    with caplog.at_level(logging.DEBUG):
        await async_password_grant(session, "someone", "hunter2")
    for secret in ("someone", "hunter2", "access-1", "refresh-1"):
        assert secret not in caplog.text
