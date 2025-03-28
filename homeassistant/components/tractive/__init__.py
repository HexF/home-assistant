"""The tractive integration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import aiotractive

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    CONF_EMAIL,
    CONF_PASSWORD,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_BUZZER,
    ATTR_DAILY_GOAL,
    ATTR_LED,
    ATTR_LIVE_TRACKING,
    ATTR_MINUTES_ACTIVE,
    CLIENT,
    DOMAIN,
    RECONNECT_INTERVAL,
    SERVER_UNAVAILABLE,
    TRACKABLES,
    TRACKER_ACTIVITY_STATUS_UPDATED,
    TRACKER_HARDWARE_STATUS_UPDATED,
    TRACKER_POSITION_UPDATED,
)

PLATFORMS = ["binary_sensor", "device_tracker", "sensor", "switch"]


_LOGGER = logging.getLogger(__name__)


@dataclass
class Trackables:
    """A class that describes trackables."""

    tracker: aiotractive.tracker.Tracker
    trackable: dict
    tracker_details: dict
    hw_info: dict
    pos_report: dict


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up tractive from a config entry."""
    data = entry.data

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})

    client = aiotractive.Tractive(
        data[CONF_EMAIL], data[CONF_PASSWORD], session=async_get_clientsession(hass)
    )
    try:
        creds = await client.authenticate()
    except aiotractive.exceptions.UnauthorizedError as error:
        await client.close()
        raise ConfigEntryAuthFailed from error
    except aiotractive.exceptions.TractiveError as error:
        await client.close()
        raise ConfigEntryNotReady from error

    tractive = TractiveClient(hass, client, creds["user_id"])
    tractive.subscribe()

    try:
        trackable_objects = await client.trackable_objects()
        trackables = await asyncio.gather(
            *(_generate_trackables(client, item) for item in trackable_objects)
        )
    except aiotractive.exceptions.TractiveError as error:
        await tractive.unsubscribe()
        raise ConfigEntryNotReady from error

    # When the pet defined in Tractive has no tracker linked we get None as `trackable`.
    # So we have to remove None values from trackables list.
    trackables = [item for item in trackables if item]

    hass.data[DOMAIN][entry.entry_id][CLIENT] = tractive
    hass.data[DOMAIN][entry.entry_id][TRACKABLES] = trackables

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    async def cancel_listen_task(_):
        await tractive.unsubscribe()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, cancel_listen_task)
    )

    return True


async def _generate_trackables(client, trackable):
    """Generate trackables."""
    trackable = await trackable.details()

    # Check that the pet has tracker linked.
    if not trackable["device_id"]:
        return

    tracker = client.tracker(trackable["device_id"])

    tracker_details, hw_info, pos_report = await asyncio.gather(
        tracker.details(), tracker.hw_info(), tracker.pos_report()
    )

    return Trackables(tracker, trackable, tracker_details, hw_info, pos_report)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        tractive = hass.data[DOMAIN][entry.entry_id].pop(CLIENT)
        await tractive.unsubscribe()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class TractiveClient:
    """A Tractive client."""

    def __init__(self, hass, client, user_id):
        """Initialize the client."""
        self._hass = hass
        self._client = client
        self._user_id = user_id
        self._listen_task = None

    @property
    def user_id(self):
        """Return user id."""
        return self._user_id

    async def trackable_objects(self):
        """Get list of trackable objects."""
        return await self._client.trackable_objects()

    def tracker(self, tracker_id):
        """Get tracker by id."""
        return self._client.tracker(tracker_id)

    def subscribe(self):
        """Start event listener coroutine."""
        self._listen_task = asyncio.create_task(self._listen())

    async def unsubscribe(self):
        """Stop event listener coroutine."""
        if self._listen_task:
            self._listen_task.cancel()
        await self._client.close()

    async def _listen(self):
        server_was_unavailable = False
        while True:
            try:
                async for event in self._client.events():
                    if server_was_unavailable:
                        _LOGGER.debug("Tractive is back online")
                        server_was_unavailable = False

                    if event["message"] == "activity_update":
                        self._send_activity_update(event)
                    else:
                        if "hardware" in event:
                            self._send_hardware_update(event)

                        if "position" in event:
                            self._send_position_update(event)
            except aiotractive.exceptions.TractiveError:
                _LOGGER.debug(
                    "Tractive is not available. Internet connection is down? Sleeping %i seconds and retrying",
                    RECONNECT_INTERVAL.total_seconds(),
                )
                async_dispatcher_send(
                    self._hass, f"{SERVER_UNAVAILABLE}-{self._user_id}"
                )
                await asyncio.sleep(RECONNECT_INTERVAL.total_seconds())
                server_was_unavailable = True
                continue

    def _send_hardware_update(self, event):
        # Sometimes hardware event doesn't contain complete data.
        payload = {
            ATTR_BATTERY_LEVEL: event["hardware"]["battery_level"],
            ATTR_BATTERY_CHARGING: event["charging_state"] == "CHARGING",
            ATTR_LIVE_TRACKING: event.get("live_tracking", {}).get("active"),
            ATTR_BUZZER: event.get("buzzer_control", {}).get("active"),
            ATTR_LED: event.get("led_control", {}).get("active"),
        }
        self._dispatch_tracker_event(
            TRACKER_HARDWARE_STATUS_UPDATED, event["tracker_id"], payload
        )

    def _send_activity_update(self, event):
        payload = {
            ATTR_MINUTES_ACTIVE: event["progress"]["achieved_minutes"],
            ATTR_DAILY_GOAL: event["progress"]["goal_minutes"],
        }
        self._dispatch_tracker_event(
            TRACKER_ACTIVITY_STATUS_UPDATED, event["pet_id"], payload
        )

    def _send_position_update(self, event):
        payload = {
            "latitude": event["position"]["latlong"][0],
            "longitude": event["position"]["latlong"][1],
            "accuracy": event["position"]["accuracy"],
        }
        self._dispatch_tracker_event(
            TRACKER_POSITION_UPDATED, event["tracker_id"], payload
        )

    def _dispatch_tracker_event(self, event_name, tracker_id, payload):
        async_dispatcher_send(
            self._hass,
            f"{event_name}-{tracker_id}",
            payload,
        )
