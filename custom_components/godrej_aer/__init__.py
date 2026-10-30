"""The Godrej Aer Smart Matic integration."""
from __future__ import annotations
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_MAC
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import CONF_COMMAND_CHAR, CONF_REPLY_CHAR
from .const import DOMAIN
from .godrej import SmartMatic
from .godrej.events import DEVICE_STATUS_UPDATE
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)
# Replaced by the "Spray: auto" select (Off / every 10, 20 or 40 min).
_REPLACED_ENTITIES = (("switch", "auto_spray"), ("select", "spray_interval"))

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """Set up Godrej Aer Smart Matic from a config entry."""

    mac = entry.options.get(CONF_MAC, None) or entry.data.get(CONF_MAC, None)

    instance = SmartMatic(
        hass,
        mac,
        command_char=entry.data.get(CONF_COMMAND_CHAR),
        reply_char=entry.data.get(CONF_REPLY_CHAR),
    )
    entry.runtime_data = instance

    async_setup_services(hass)

    retry_timer = None

    @callback
    def _cancel_retry_timer():
        nonlocal retry_timer
        if retry_timer is not None:
            retry_timer()
            retry_timer = None

    entry.async_on_unload(_cancel_retry_timer)

    async def _read_schedules():
        _LOGGER.debug("Reading the schedules of %s after startup", mac)
        try:
            await instance.read_schedules()
        except Exception:
            _LOGGER.debug("Could not read schedules from %s", mac, exc_info=True)

    async def _on_first_status(_status) -> None:
        # Read the device's own schedules once, after the first status
        # shows it is reachable; later changes read them back themselves.
        instance.eventbus.remove_listener(DEVICE_STATUS_UPDATE, _on_first_status)
        if instance.schedules is None:
            entry.async_create_background_task(
                hass, _read_schedules(), f"godrej_aer schedules {mac}"
            )

    instance.eventbus.add_listener(DEVICE_STATUS_UPDATE, _on_first_status)
    entry.async_on_unload(
        lambda: instance.eventbus.remove_listener(DEVICE_STATUS_UPDATE, _on_first_status)
    )

    async def _connect_if_needed():
        nonlocal retry_timer
        if bluetooth.async_address_present(hass, mac, connectable=True):
            try:
                await instance.connect_if_needed()
            except Exception as e:
                _LOGGER.debug("Background status read for %s failed: %s", mac, e)
                # Retry exactly when the backoff ends, not at the next
                # periodic tick (up to a minute later).
                delay = instance.retry_delay
                if delay is not None:
                    _cancel_retry_timer()
                    retry_timer = async_call_later(hass, delay + 0.5, _retry_now)

    @callback
    def _retry_now(_now) -> None:
        nonlocal retry_timer
        retry_timer = None
        _LOGGER.debug("Retrying %s after the backoff", mac)
        _schedule_connect()

    @callback
    def _schedule_connect():
        # Don't delay startup; cancelled on unload.
        entry.async_create_background_task(
            hass, _connect_if_needed(), f"godrej_aer connect {mac}"
        )

    @callback
    def _async_discovered_device(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange
    ):
        """Subscribe to bluetooth changes."""
        instance.set_advertising(True)
        _schedule_connect()

    @callback
    def _async_device_unavailable(
        service_info: bluetooth.BluetoothServiceInfoBleak,
    ) -> None:
        # No advertisements for a while: out of range or out of battery.
        instance.set_advertising(False)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_discovered_device,
            BluetoothCallbackMatcher({ADDRESS: mac}),
            bluetooth.BluetoothScanningMode.PASSIVE
        )
    )
    entry.async_on_unload(
        bluetooth.async_track_unavailable(
            hass, _async_device_unavailable, mac, connectable=True
        )
    )

    registry = er.async_get(hass)
    for platform, key in _REPLACED_ENTITIES:
        old = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        if old:
            registry.async_remove(old)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    @callback
    def _async_periodic_connect(_now):
        _schedule_connect()

    entry.async_on_unload(
        async_track_time_interval(hass, _async_periodic_connect, timedelta(seconds=60))
    )

    _schedule_connect()

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        instance: SmartMatic = entry.runtime_data
        await instance.disconnect()
        async_unload_services(hass, entry)
    return unload_ok

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Config entry %s updated", entry.entry_id)