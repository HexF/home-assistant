"""DataUpdateCoordinator for WLED."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from wled import WLED, Device as WLEDDevice, WLEDConnectionClosed, WLEDError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_KEEP_MASTER_LIGHT,
    DEFAULT_KEEP_MASTER_LIGHT,
    DOMAIN,
    LOGGER,
    SCAN_INTERVAL,
)


class WLEDDataUpdateCoordinator(DataUpdateCoordinator[WLEDDevice]):
    """Class to manage fetching WLED data from single endpoint."""

    keep_master_light: bool

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry: ConfigEntry,
    ) -> None:
        """Initialize global WLED data updater."""
        self.keep_master_light = entry.options.get(
            CONF_KEEP_MASTER_LIGHT, DEFAULT_KEEP_MASTER_LIGHT
        )
        self.wled = WLED(entry.data[CONF_HOST], session=async_get_clientsession(hass))
        self.unsub: Callable | None = None

        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    @property
    def has_master_light(self) -> bool:
        """Return if the coordinated device has an master light."""
        return self.keep_master_light or (
            self.data is not None and len(self.data.state.segments) > 1
        )

    def update_listeners(self) -> None:
        """Call update on all listeners."""
        for update_callback in self._listeners:
            update_callback()

    @callback
    def _use_websocket(self) -> None:
        """Use WebSocket for updates, instead of polling."""

        async def listen() -> None:
            """Listen for state changes via WebSocket."""
            try:
                await self.wled.connect()
            except WLEDError as err:
                self.logger.info(err)
                if self.unsub:
                    self.unsub()
                    self.unsub = None
                return

            try:
                await self.wled.listen(callback=self.async_set_updated_data)
            except WLEDConnectionClosed as err:
                self.last_update_success = False
                self.logger.info(err)
            except WLEDError as err:
                self.last_update_success = False
                self.update_listeners()
                self.logger.error(err)

            # Ensure we are disconnected
            await self.wled.disconnect()
            if self.unsub:
                self.unsub()
                self.unsub = None

        async def close_websocket(_) -> None:
            """Close WebSocket connection."""
            await self.wled.disconnect()

        # Clean disconnect WebSocket on Home Assistant shutdown
        self.unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, close_websocket
        )

        # Start listening
        asyncio.create_task(listen())

    async def _async_update_data(self) -> WLEDDevice:
        """Fetch data from WLED."""
        try:
            device = await self.wled.update(full_update=not self.last_update_success)
        except WLEDError as error:
            raise UpdateFailed(f"Invalid response from API: {error}") from error

        # If the device supports a WebSocket, try activating it.
        if (
            device.info.websocket is not None
            and not self.wled.connected
            and not self.unsub
        ):
            self._use_websocket()

        return device
