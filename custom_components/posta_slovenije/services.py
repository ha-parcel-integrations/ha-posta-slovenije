"""Services for the Pošta Slovenije parcel tracker integration.

`posta_slovenije.track_parcel` / `posta_slovenije.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
can start tracking a parcel straight from a dashboard.

`track_parcel` takes the same incoming/outgoing choice as the options flow,
defaulting to incoming: the tracking lookup cannot reveal which way a parcel
travels (see `CONF_DIRECTION` in `const.py`).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import normalize_tracking_code, valid_tracking_code
from .const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_DIRECTION,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_TRACKING,
)
from .parcels import source_of, tracked_direction

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_DIRECTION, default=DEFAULT_DIRECTION): vol.In(
            [DIRECTION_INCOMING, DIRECTION_OUTGOING]
        ),
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})


def _resolve_entry(hass: HomeAssistant):
    """Return the tracking hub, or raise when it is not set up.

    An account entry reads its parcels from the inbox and has no code list, so
    the services only ever act on the tracking hub.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if source_of(entry) == SOURCE_TRACKING:
            return entry
    raise ServiceValidationError("Pošta Slovenije tracking is not set up")


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Pošta Slovenije services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid Pošta Slovenije tracking code"
            )
        entry = _resolve_entry(hass)

        direction = call.data[CONF_DIRECTION]
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        existing = next(
            (p for p in parcels if p[CONF_TRACKING_CODE] == tracking_code), None
        )
        if existing is not None:
            # Already tracked. Calling again with the other direction is how a
            # parcel filed the wrong way gets corrected without untracking it
            # first; calling again with the same one stays a no-op.
            if tracked_direction(existing) == direction:
                return
            existing[CONF_DIRECTION] = direction
        else:
            parcels.append(
                {CONF_TRACKING_CODE: tracking_code, CONF_DIRECTION: direction}
            )
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Pošta Slovenije services (they go with the last tracking hub)."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
