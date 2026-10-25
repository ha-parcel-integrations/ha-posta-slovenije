"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because each MojaPošta account gets a device named
after it, apart from the tracking hub.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, SOURCE_ACCOUNT
from .parcels import source_of

CONFIGURATION_URL = "https://moja.posta.si"

ATTRIBUTION = "Data provided by Pošta Slovenije"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this entry.

    The account's title is part of its device name, so two configured accounts
    do not produce two indistinguishable devices.
    """
    is_account = source_of(entry) == SOURCE_ACCOUNT
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title if is_account else "Pošta Slovenije",
        manufacturer="Pošta Slovenije",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
