"""Pošta Slovenije parcel tracker custom component for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.auth import PostaSlovenijeTokens
from .account.client import PostaSlovenijeAccountClient
from .account.coordinator import PostaSlovenijeAccountCoordinator
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .parcels import source_of
from .services import async_setup_services, async_unload_services
from .tracking.api import PostaSlovenijeApiClient
from .tracking.coordinator import PostaSlovenijeCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PostaSlovenijeData:
    """Runtime data attached to a Pošta Slovenije config entry."""

    client: PostaSlovenijeApiClient | PostaSlovenijeAccountClient
    coordinator: PostaSlovenijeCoordinator | PostaSlovenijeAccountCoordinator


type PostaSlovenijeConfigEntry = ConfigEntry[PostaSlovenijeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: PostaSlovenijeConfigEntry
) -> bool:
    """Set up Pošta Slovenije from a config entry."""
    session = async_get_clientsession(hass)
    is_account = source_of(entry) == SOURCE_ACCOUNT
    client: PostaSlovenijeApiClient | PostaSlovenijeAccountClient
    coordinator: PostaSlovenijeCoordinator | PostaSlovenijeAccountCoordinator
    if is_account:

        @callback
        def _persist_tokens(tokens: PostaSlovenijeTokens) -> None:
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: tokens.access_token,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_TOKEN_EXPIRES_AT: tokens.expires_at,
                },
            )

        stored = None
        if entry.data.get(CONF_ACCESS_TOKEN):
            stored = PostaSlovenijeTokens(
                entry.data[CONF_ACCESS_TOKEN],
                entry.data.get(CONF_REFRESH_TOKEN) or None,
                float(entry.data.get(CONF_TOKEN_EXPIRES_AT) or 0),
            )
        client = PostaSlovenijeAccountClient(
            session,
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            tokens=stored,
            on_tokens=_persist_tokens,
        )
        coordinator = PostaSlovenijeAccountCoordinator(hass, client, entry)
    else:
        client = PostaSlovenijeApiClient(session)
        coordinator = PostaSlovenijeCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PostaSlovenijeData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not is_account:
        # Apply option changes (added/removed parcels, history) live via a
        # coordinator refresh — no reload — so per-parcel sensors appear and
        # disappear immediately. This is also the resume path after polling
        # fully suspended: adding a parcel back triggers this refresh, which
        # recomputes the tier and re-arms scheduling. The account options flow
        # reloads on submit instead, so it registers no listener.
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
        async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: PostaSlovenijeConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(
    hass: HomeAssistant, entry: PostaSlovenijeConfigEntry
) -> bool:
    """Unload the Pošta Slovenije config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services act on the tracking hub, so they go with the last one.
    if source_of(entry) == SOURCE_TRACKING and not any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and source_of(other) == SOURCE_TRACKING
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        async_unload_services(hass)
    return True
