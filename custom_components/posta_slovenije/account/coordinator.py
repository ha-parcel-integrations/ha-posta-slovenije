"""Coordinator for a MojaPošta account: both lists, then detail where it moved."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import DIRECTION_INCOMING, DIRECTION_OUTGOING
from ..coordinator import PostaSlovenijeBaseCoordinator
from ..transport import PostaSlovenijeApiError
from .client import PostaSlovenijeAccountAuthError, PostaSlovenijeAccountClient
from .parcels import normalize_account_parcel, row_direction, scans_of

_LOGGER = logging.getLogger(__name__)

# The inbox summary can lag the scans, so a parcel still on its way gets its
# detail read again after this long even when the summary has not moved.
DETAIL_MAX_AGE_SECONDS = 3 * 3600


@dataclass
class _ReadDetail:
    """A detail read, with the summary stamp and time it was read at."""

    stamp: Any
    read_at: float
    detail: dict[str, Any]
    delivered: bool


class PostaSlovenijeAccountCoordinator(PostaSlovenijeBaseCoordinator):
    """Polls one MojaPošta account's incoming and outgoing lists.

    The direction comes from the feed, never from the user: each list is its
    own route, and a parcel's ``isOutgoingParcel`` — on its detail where the
    wire carries it, on its inbox row otherwise — is the authority where it is
    present.
    """

    # The mid-tier poll is also how a new parcel is discovered.
    _stops_when_idle = False

    def __init__(
        self,
        hass: HomeAssistant,
        client: PostaSlovenijeAccountClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(hass, entry)
        self.account_client = client
        self.inbox_size = 0
        self._details: dict[str, _ReadDetail] = {}

    def _detail_is_stale(self, parcel_id: str, item: dict[str, Any]) -> bool:
        """Whether the detail has to be read again for this inbox row."""
        known = self._details.get(parcel_id)
        if known is None or known.stamp != item.get("currentStatusDateTime"):
            return True
        # A delivered parcel's scans cannot change again.
        if known.delivered:
            return False
        return time.monotonic() - known.read_at > DETAIL_MAX_AGE_SECONDS

    async def _async_inbox(self) -> list[tuple[dict[str, Any], str]]:
        """Both lists as ``(row, listed direction)``, archived and repeated rows dropped."""
        client = self.account_client
        try:
            await client.async_prepare()
            listed = [
                (row, DIRECTION_INCOMING)
                for row in await client.async_get_inbox(DIRECTION_INCOMING)
            ] + [
                (row, DIRECTION_OUTGOING)
                for row in await client.async_get_inbox(DIRECTION_OUTGOING)
            ]
        except PostaSlovenijeAccountAuthError as err:
            raise ConfigEntryAuthFailed(
                "Pošta Slovenije rejected the password"
            ) from err
        except PostaSlovenijeApiError as err:
            if err.status_code == 429:
                raise self._rate_limited(err.retry_after) from err
            raise UpdateFailed(f"Pošta Slovenije inbox unavailable: {err}") from err

        rows: dict[str, tuple[dict[str, Any], str]] = {}
        for row, source in listed:
            # An archived parcel leaves the default list in the app; it is not
            # tracked. A parcel in both lists is shown once.
            if row.get("isArchived") is True:
                continue
            rows.setdefault(str(row["parcelId"]), (row, source))
        return list(rows.values())

    async def _async_poll(self) -> list[dict]:
        """Read both lists and publish their parcels."""
        client = self.account_client
        rows = await self._async_inbox()
        self.inbox_size = len(rows)
        ids = {str(row["parcelId"]) for row, _ in rows}
        self._details = {k: v for k, v in self._details.items() if k in ids}

        include_history = self._include_history
        incoming: list[dict] = []
        outgoing: list[dict] = []
        for row, listed in rows:
            parcel_id = str(row["parcelId"])
            if self._detail_is_stale(parcel_id, row):
                try:
                    detail = await client.async_get_parcel(row["parcelId"])
                except PostaSlovenijeAccountAuthError as err:
                    raise ConfigEntryAuthFailed(
                        "Pošta Slovenije rejected the password"
                    ) from err
                except PostaSlovenijeApiError as err:
                    if err.status_code == 429:
                        raise self._rate_limited(err.retry_after) from err
                    detail = None
                    _LOGGER.warning("Pošta Slovenije detail fetch failed: %s", err)
                except aiohttp.ClientError as err:
                    detail = None
                    _LOGGER.warning("Pošta Slovenije detail fetch failed: %r", err)
                if detail is not None:
                    self._details[parcel_id] = _ReadDetail(
                        stamp=row.get("currentStatusDateTime"),
                        read_at=time.monotonic(),
                        detail=detail,
                        delivered=normalize_account_parcel(row, detail)["delivered"],
                    )
            known = self._details.get(parcel_id)
            detail = known.detail if known else None
            if detail is None and not scans_of(row, None):
                # Nothing to read scans from yet: show it once the detail answers.
                continue
            parcel = normalize_account_parcel(
                row, detail, include_history=include_history
            )
            direction = row_direction(row, listed, detail)
            (outgoing if direction == DIRECTION_OUTGOING else incoming).append(parcel)

        return self._publish(incoming, outgoing)
