"""Tests for the Pošta Slovenije refresh button."""

from unittest.mock import AsyncMock, MagicMock

from custom_components.posta_slovenije.button import PostaSlovenijeRefreshButton


async def test_refresh_button_requests_refresh():
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.runtime_data.coordinator.async_request_refresh = AsyncMock()

    button = PostaSlovenijeRefreshButton(entry)
    assert button.unique_id == "e1_refresh"

    await button.async_press()
    entry.runtime_data.coordinator.async_request_refresh.assert_awaited_once()
