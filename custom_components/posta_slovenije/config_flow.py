"""Config flow for the Pošta Slovenije parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.auth import PostaSlovenijeTokens
from .account.client import PostaSlovenijeAccountAuthError, PostaSlovenijeAccountClient
from .const import (
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
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .parcels import source_of, tracked_direction
from .transport import PostaSlovenijeApiError

_LOGGER = logging.getLogger(__name__)

_CODE_SHAPE = re.compile(r"[A-Z]{2}[0-9]{9}[A-Z]{2}")
_S10_WEIGHTS = (8, 6, 4, 2, 3, 5, 9, 7)


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code trimmed, without spaces, and upper-cased."""
    return "".join((value or "").split()).upper()


def valid_tracking_code(value: str) -> bool:
    """Accept a UPU S10 code: two letters, nine digits, two letters.

    The ninth digit is the S10 check digit, so a mistyped number is refused
    before it ever reaches the network.
    """
    if not _CODE_SHAPE.fullmatch(value or ""):
        return False
    total = sum(int(d) * w for d, w in zip(value[2:10], _S10_WEIGHTS, strict=True))
    check = 11 - total % 11
    check = {10: 0, 11: 5}.get(check, check)
    return check == int(value[10])


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class PostaSlovenijeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Pošta Slovenije integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PostaSlovenijeOptionsFlowHandler:
        """Return the options flow handler."""
        return PostaSlovenijeOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a MojaPošta account or tracking by code."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking hub — one per install, no input needed.

        Tracking is keyed on the tracking code alone (no account, no postal
        code), so there is nothing to ask: the entry is created straight away
        and parcels are added afterwards via the options flow, the
        ``posta_slovenije.track_parcel`` service or a dashboard button. A
        household holds all its codes in this one hub.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Pošta Slovenije",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def _async_check_account(
        self, username: str, password: str
    ) -> tuple[dict[str, Any], str | None]:
        """Sign in and read the inbox once; return the token data or an error key."""
        captured: list[PostaSlovenijeTokens] = []
        client = PostaSlovenijeAccountClient(
            async_get_clientsession(self.hass),
            username=username,
            password=password,
            on_tokens=captured.append,
        )
        try:
            await client.async_sign_in()
            await client.async_get_inbox()
        except PostaSlovenijeAccountAuthError:
            return {}, "invalid_auth"
        except (PostaSlovenijeApiError, aiohttp.ClientError):
            return {}, "cannot_connect"
        tokens = captured[-1]
        return {
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_TOKEN_EXPIRES_AT: tokens.expires_at,
        }, None

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in to MojaPošta; parcels are then discovered from the inbox."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            await self.async_set_unique_id(f"{SOURCE_ACCOUNT}:{username.lower()}")
            self._abort_if_unique_id_configured()
            tokens, error = await self._async_check_account(username, password)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"Pošta Slovenije ({username})",
                    data={
                        CONF_SOURCE: SOURCE_ACCOUNT,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        **tokens,
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): selector.TextSelector(
                    selector.TextSelectorConfig(autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Ask for the password again; the account itself never changes."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the new password once it signs in."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            password = str(user_input[CONF_PASSWORD])
            tokens, error = await self._async_check_account(
                entry.data[CONF_USERNAME], password
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password, **tokens}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={"account": entry.title},
            errors=errors,
        )


class PostaSlovenijeOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings.

        An account entry has no code list to edit (its feed says which parcels
        are incoming and which are outgoing), so it goes straight to its
        settings. The tracking hub's payload cannot tell the two directions
        apart, so it gets a menu entry per direction, both opening the same
        list editor: the user declares the direction by filing a code in one
        list or the other.
        """
        if source_of(self.config_entry) == SOURCE_ACCOUNT:
            return await self.async_step_settings()
        return self.async_show_menu(
            step_id="init",
            menu_options=["incoming_parcels", "outgoing_parcels", "settings"],
        )

    async def async_step_incoming_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being received."""
        return await self._async_step_parcel_list(DIRECTION_INCOMING, user_input)

    async def async_step_outgoing_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being sent."""
        return await self._async_step_parcel_list(DIRECTION_OUTGOING, user_input)

    async def _async_step_parcel_list(
        self, direction: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and handle one direction's complete tracked-code list.

        Both directions share a single ``CONF_PARCELS`` list, so a submission
        replaces this direction's entries and leaves the other direction's
        alone, except for a code submitted here that was filed the other way:
        that moves rather than being rejected, because re-entering a code in
        the other list is how a user corrects a parcel they filed wrongly.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                kept = [
                    parcel
                    for parcel in _current_parcels(self.config_entry)
                    if tracked_direction(parcel) != direction
                    and parcel.get(CONF_TRACKING_CODE) not in codes
                ]
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: kept
                        + [
                            {CONF_TRACKING_CODE: code, CONF_DIRECTION: direction}
                            for code in codes
                        ],
                    },
                )
        current_codes = [
            parcel[CONF_TRACKING_CODE]
            for parcel in _current_parcels(self.config_entry)
            if tracked_direction(parcel) == direction
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=f"{direction}_parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            if source_of(self.config_entry) == SOURCE_ACCOUNT:
                # No update listener on account entries; a reload applies it.
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PARCELS: _current_parcels(self.config_entry),
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
