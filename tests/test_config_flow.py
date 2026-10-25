"""Tests for the Pošta Slovenije config and options flows."""

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posta_slovenije.account.auth import PostaSlovenijeTokens
from custom_components.posta_slovenije.account.client import (
    PostaSlovenijeAccountAuthError,
)
from custom_components.posta_slovenije.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.posta_slovenije.const import (
    CONF_ACCESS_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TRACKING_CODE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posta_slovenije.transport import PostaSlovenijeApiError

from .payloads import DELIVERED_CODE, PICKUP_CODE, RETURNED_CODE

CLIENT = "custom_components.posta_slovenije.config_flow.PostaSlovenijeAccountClient"


def test_normalize_tracking_code_trims_removes_spaces_and_uppercases():
    assert normalize_tracking_code("  ch 1000 00003 si ") == "CH100000003SI"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


@pytest.mark.parametrize(
    "code", [RETURNED_CODE, DELIVERED_CODE, PICKUP_CODE, "CH300000009SI"]
)
def test_valid_codes(code):
    assert valid_tracking_code(code)


@pytest.mark.parametrize(
    "code",
    [
        "",
        "CH100000004SI",  # wrong check digit
        "CH10000003SI",  # too short
        "1H100000003SI",  # digit where a letter belongs
        "CH100000003S",
        "ch100000003si",  # not normalised
    ],
)
def test_invalid_codes(code):
    assert not valid_tracking_code(code)


def test_s10_check_digit_special_cases():
    # remainder 1 -> check 10 -> 0; remainder 0 -> check 11 -> 5
    assert valid_tracking_code("CH000000005SI")
    ten = next(
        f"CH{n:08d}0SI"
        for n in range(1, 10**4)
        if sum(
            int(d) * w
            for d, w in zip(f"{n:08d}", (8, 6, 4, 2, 3, 5, 9, 7), strict=True)
        )
        % 11
        == 1
    )
    assert valid_tracking_code(ten)


async def test_user_flow_offers_both_sources(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == [SOURCE_ACCOUNT, SOURCE_TRACKING]


async def _choose(hass, step):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_tracking_hub_is_created_without_input(hass):
    result = await _choose(hass, SOURCE_TRACKING)
    assert result["type"] == "create_entry"
    assert result["title"] == "Pošta Slovenije"
    assert result["data"] == {CONF_SOURCE: SOURCE_TRACKING}
    assert result["options"][CONF_PARCELS] == []


async def test_second_tracking_hub_is_refused_but_accounts_are_not(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_SOURCE: SOURCE_TRACKING}
    ).add_to_hass(hass)
    result = await _choose(hass, SOURCE_TRACKING)
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    result = await _choose(hass, SOURCE_ACCOUNT)
    assert result["type"] == "form"  # an account can sit next to the hub


def _client_class(*, sign_in=None, inbox=None, tokens=None):
    tokens = tokens or PostaSlovenijeTokens("access-1", "refresh-1", 1234.0)

    def build(session, *, username, password, on_tokens):
        client = AsyncMock()
        client.async_sign_in = AsyncMock(side_effect=sign_in)
        client.async_get_inbox = AsyncMock(side_effect=inbox, return_value=[])
        if sign_in is None:
            client.async_sign_in.side_effect = lambda: on_tokens(tokens)
        return client

    return build


async def _submit_account(hass, user="Someone", password="hunter2"):  # noqa: S107
    result = await _choose(hass, SOURCE_ACCOUNT)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: user, CONF_PASSWORD: password}
    )


async def test_account_is_created_with_its_tokens(hass):
    with patch(CLIENT, new=_client_class()):
        result = await _submit_account(hass)
    assert result["type"] == "create_entry"
    assert result["title"] == "Pošta Slovenije (Someone)"
    assert result["data"] == {
        CONF_SOURCE: SOURCE_ACCOUNT,
        CONF_USERNAME: "Someone",
        CONF_PASSWORD: "hunter2",
        CONF_ACCESS_TOKEN: "access-1",
        CONF_REFRESH_TOKEN: "refresh-1",
        CONF_TOKEN_EXPIRES_AT: 1234.0,
    }
    assert CONF_PARCELS not in result["options"]


async def test_same_account_cannot_be_added_twice_in_another_casing(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id="account:someone", data={CONF_SOURCE: SOURCE_ACCOUNT}
    ).add_to_hass(hass)
    with patch(CLIENT, new=_client_class()):
        result = await _submit_account(hass, user="SOMEONE")
    assert result["type"] == "abort"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (PostaSlovenijeAccountAuthError("rejected"), "invalid_auth"),
        (PostaSlovenijeApiError("HTTP 500"), "cannot_connect"),
        (aiohttp.ClientError(), "cannot_connect"),
    ],
)
async def test_account_errors(hass, error, key):
    with patch(CLIENT, new=_client_class(sign_in=error)):
        result = await _submit_account(hass)
    assert result["type"] == "form"
    assert result["errors"] == {"base": key}


async def test_a_token_the_api_refuses_is_reported(hass):
    with patch(
        CLIENT, new=_client_class(inbox=PostaSlovenijeAccountAuthError("rejected"))
    ):
        result = await _submit_account(hass)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_failed_account_form_does_not_echo_the_password(hass):
    with patch(CLIENT, new=_client_class(sign_in=PostaSlovenijeAccountAuthError("x"))):
        result = await _submit_account(hass)
    suggested = [
        key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    ]
    assert "hunter2" not in suggested
    assert "Someone" in suggested


def _account_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:someone",
        title="Pošta Slovenije (someone)",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: "someone",
            CONF_PASSWORD: "old",
            CONF_ACCESS_TOKEN: "a",
            CONF_REFRESH_TOKEN: "r",
            CONF_TOKEN_EXPIRES_AT: 1.0,
        },
        options={},
    )


async def test_reauth_asks_only_for_the_password_and_stores_new_tokens(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert list(result["data_schema"].schema) == [CONF_PASSWORD]

    tokens = PostaSlovenijeTokens("access-9", "refresh-9", 99.0)
    with (
        patch(CLIENT, new=_client_class(tokens=tokens)),
        patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
    assert entry.data[CONF_ACCESS_TOKEN] == "access-9"
    assert entry.data[CONF_USERNAME] == "someone"


async def test_reauth_with_a_wrong_password_shows_the_error(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(CLIENT, new=_client_class(sign_in=PostaSlovenijeAccountAuthError("x"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "bad"}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


# -- options ----------------------------------------------------------------


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels},
    )


async def _open_options_step(hass, entry, step_id):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == [
        "incoming_parcels",
        "outgoing_parcels",
        "settings",
    ]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_add_parcel_normalises_the_code(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "incoming_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [" ch200000006 si"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_options_reject_a_malformed_code(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "incoming_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["CH200000007SI"]}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_tracking_code"}


async def test_options_de_duplicate_and_remove(hass):
    entry = _hub(
        [{CONF_TRACKING_CODE: PICKUP_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "incoming_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [PICKUP_CODE, PICKUP_CODE.lower(), ""]}
    )
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_options_can_clear_the_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: PICKUP_CODE}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "incoming_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["data"][CONF_PARCELS] == []


async def test_hub_settings_keep_the_code_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: PICKUP_CODE}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
            CONF_INCLUDE_HISTORY: True,
        },
    )
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: PICKUP_CODE}]
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5


async def test_account_options_go_straight_to_settings_and_reload(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "settings"
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_schedule_reload"
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DELIVERED_FILTER_TYPE: "days",
                CONF_DELIVERED_FILTER_AMOUNT: 14,
                CONF_INCLUDE_HISTORY: False,
            },
        )
    reload.assert_called_once_with(entry.entry_id)
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 14


async def test_outgoing_list_files_codes_as_outgoing(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "outgoing_parcels")
    assert result["step_id"] == "outgoing_parcels"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [PICKUP_CODE]}
    )
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_one_direction_leaves_the_other_alone(hass):
    entry = _hub(
        [
            {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
            {CONF_TRACKING_CODE: DELIVERED_CODE},  # no direction: incoming
        ]
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "incoming_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [DELIVERED_CODE, RETURNED_CODE]}
    )
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
        {CONF_TRACKING_CODE: DELIVERED_CODE, CONF_DIRECTION: DIRECTION_INCOMING},
        {CONF_TRACKING_CODE: RETURNED_CODE, CONF_DIRECTION: DIRECTION_INCOMING},
    ]


async def test_refiling_a_code_under_the_other_direction_moves_it(hass):
    entry = _hub([{CONF_TRACKING_CODE: PICKUP_CODE}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "outgoing_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [PICKUP_CODE]}
    )
    assert result["type"] == "create_entry"  # no duplicate error
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_each_list_shows_only_its_own_codes(hass):
    entry = _hub(
        [
            {CONF_TRACKING_CODE: PICKUP_CODE, CONF_DIRECTION: DIRECTION_OUTGOING},
            {CONF_TRACKING_CODE: DELIVERED_CODE},
        ]
    )
    entry.add_to_hass(hass)
    out = await _open_options_step(hass, entry, "outgoing_parcels")
    inc = await _open_options_step(hass, entry, "incoming_parcels")

    def suggested(result):
        return next(
            k.description["suggested_value"]
            for k in result["data_schema"].schema
            if k == "tracking_codes"
        )

    assert suggested(out) == [PICKUP_CODE]
    assert suggested(inc) == [DELIVERED_CODE]
