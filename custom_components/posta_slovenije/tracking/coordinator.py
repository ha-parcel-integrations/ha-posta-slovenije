"""Coordinator for the tracking hub: one guest lookup per tracking code."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_PARCELS, CONF_TRACKING_CODE, DIRECTION_OUTGOING
from ..coordinator import PostaSlovenijeBaseCoordinator
from ..parcels import tracked_direction
from .api import PostaSlovenijeApiClient, PostaSlovenijeApiError
from .parcels import normalize_parcel, scans_of

_LOGGER = logging.getLogger(__name__)

# Rate limits are unknown, so lookups for a household's codes run a few at a time.
MAX_PARALLEL_LOOKUPS = 4


class PostaSlovenijeCoordinator(PostaSlovenijeBaseCoordinator):
    """Polls each tracked code and publishes the canonical parcel lists.

    The tracked parcels are the tracking codes the user entered (stored in the
    entry options). Each is fetched individually and merged into one list. The
    direction is the one the user filed the code under: a guest lookup cannot
    say who is sending.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PostaSlovenijeApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(hass, entry)
        self._client = client
        # tracking_code -> last successful reply, so a transient fetch failure,
        # an aged-out record or a reply without scans keeps the parcel visible
        # instead of dropping its sensor. Resets on restart.
        self._raw_cache: dict[str, dict[str, Any]] = {}
        # Codes confirmed delivered on a prior refresh — excluded from the
        # fetch since a delivered parcel's history can never change again.
        self._delivered_codes: set[str] = set()

    @property
    def delivered_codes(self) -> set[str]:
        """Tracking codes currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

    def _tracked(self) -> dict[str, str]:
        """Return the configured tracking codes mapped to their direction."""
        return {
            item[CONF_TRACKING_CODE]: tracked_direction(item)
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        }

    async def _async_poll(self) -> list[dict]:
        """Fetch every tracked code and split into active vs delivered."""
        directions = self._tracked()
        codes = list(directions)

        # Drop cache entries for parcels the user no longer follows.
        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes
        codes_to_fetch = [code for code in codes if code not in self._delivered_codes]

        semaphore = asyncio.Semaphore(MAX_PARALLEL_LOOKUPS)

        async def lookup(code: str) -> dict[str, Any] | None:
            async with semaphore:
                return await self._client.async_get_parcel(code)

        results = await asyncio.gather(
            *(lookup(code) for code in codes_to_fetch), return_exceptions=True
        )

        payloads: dict[str, dict[str, Any] | None] = {}
        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for code, result in zip(codes_to_fetch, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(
                    result, (PostaSlovenijeApiError, aiohttp.ClientError)
                ):
                    raise result
                errors += 1
                if (
                    isinstance(result, PostaSlovenijeApiError)
                    and result.status_code == 429
                ):
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("Pošta Slovenije fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    payloads[code] = cached
                continue

            if result is None:
                # Unknown number: aged out of the service, or not there yet.
                # Keep prior data if we have it, otherwise show a placeholder.
                payloads[code] = self._raw_cache.get(code)
                continue

            cached = self._raw_cache.get(code)
            if not scans_of(result) and cached is not None and scans_of(cached):
                # A later reply with no scans must not wipe a populated history.
                payloads[code] = cached
                continue
            self._raw_cache[code] = result
            payloads[code] = result

        # Codes skipped from the fetch (already confirmed delivered) keep their
        # cached reply so the delivered sensor holds its data.
        for code in self._delivered_codes:
            if code in self._raw_cache:
                payloads[code] = self._raw_cache[code]

        if saw_429:
            # A 429 anywhere in this batch means the whole poll backs off.
            raise self._rate_limited(max(retry_afters) if retry_afters else None)

        if codes_to_fetch and errors == len(codes_to_fetch) and not payloads:
            raise UpdateFailed("Pošta Slovenije unreachable for all tracked parcels")

        include_history = self._include_history
        entries = [
            (code, normalize_parcel(code, payload, include_history=include_history))
            for code, payload in payloads.items()
        ]
        self._delivered_codes = {
            code for code, parcel in entries if parcel["delivered"]
        }
        # Only stamp the diagnostic timestamp when at least one fetch actually
        # succeeded (or nothing needed fetching) — a poll served entirely from
        # cache must not present itself as a successful update.
        return self._publish(
            [p for code, p in entries if directions[code] != DIRECTION_OUTGOING],
            [p for code, p in entries if directions[code] == DIRECTION_OUTGOING],
            stamp_success=not codes_to_fetch or errors < len(codes_to_fetch),
        )
