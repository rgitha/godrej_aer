from __future__ import annotations

import time
from collections.abc import Callable

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    CONF_NAME,
    EntityCategory,
)
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .entity import async_device_device_info_fn
from .godrej import SmartMatic, DeviceStatus
from .godrej.events import DEVICE_REACHABILITY, DEVICE_STATUS_UPDATE


async def async_setup_entry(hass, config_entry, async_add_entities):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticStatusSensor(instance, config_entry),
        SmartMaticBatterySensor(instance, config_entry),
        SmartMaticBatteryVoltageSensor(instance, config_entry),
        SmartMaticRefillLevelSensor(instance, config_entry),
        SmartMaticSprayCountSensor(instance, config_entry),
        SmartMaticChannelSensor(
            instance, config_entry, "command_channel",
            "Channel: command", instance.command_char,
        ),
        SmartMaticChannelSensor(
            instance, config_entry, "reply_channel",
            "Channel: reply", instance.reply_char,
        ),
        SmartMaticSignalStrengthSensor(instance, config_entry),
        SmartMaticUpdatedLastSensor(instance, config_entry),
    ])


class SmartMaticSensor(SensorEntity):
    """Base for sensors driven by device status updates."""

    _key: str
    _value_fn: Callable[[DeviceStatus], float | int | None]
    _attr_has_entity_name = True

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{self._key}"
        if instance.device_status is not None:
            self._attr_native_value = self._value_fn(instance.device_status)

    async def async_added_to_hass(self) -> None:
        self._instance.eventbus.add_listener(
            DEVICE_STATUS_UPDATE, self.on_status_update
        )

    async def async_will_remove_from_hass(self) -> None:
        self._instance.eventbus.remove_listener(
            DEVICE_STATUS_UPDATE, self.on_status_update
        )

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )

    async def on_status_update(self, device_status: DeviceStatus):
        self._attr_native_value = self._value_fn(device_status)
        self.async_write_ha_state()


class SmartMaticStatusSensor(SensorEntity):
    """One status: Idle, Auto, Schedule or Disconnected.

    Disconnected when Connection status is Unreachable (not heard, or the
    last exchange failed); otherwise the mode from the last status read
    (see Updated last). Unknown until something is heard or read.
    """

    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_translation_key = "device_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "auto", "schedule", "disconnected"]

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_device_status"

    @property
    def native_value(self) -> str | None:
        if self._instance.reachable is False:
            return "disconnected"
        status = self._instance.device_status
        if status is None or status.current_status is None:
            return None
        if not status.is_spraying:
            return "idle"
        return "schedule" if status.spray_source == "schedule" else "auto"

    @property
    def icon(self) -> str:
        return {
            "idle": "mdi:spray",
            "auto": "mdi:timer-sync-outline",
            "schedule": "mdi:calendar-clock",
            "disconnected": "mdi:bluetooth-off",
        }.get(self.native_value, "mdi:help-circle-outline")

    async def async_added_to_hass(self) -> None:
        for event in (DEVICE_STATUS_UPDATE, DEVICE_REACHABILITY):
            self._instance.eventbus.add_listener(event, self._on_event)

    async def async_will_remove_from_hass(self) -> None:
        for event in (DEVICE_STATUS_UPDATE, DEVICE_REACHABILITY):
            self._instance.eventbus.remove_listener(event, self._on_event)

    async def _on_event(self, _data) -> None:
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )


class SmartMaticBatterySensor(SmartMaticSensor):
    """Estimated battery level (linear in the voltage; see const.py)."""

    _key = "battery_level"
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _value_fn = staticmethod(lambda status: status.battery_percent)


class SmartMaticBatteryVoltageSensor(SmartMaticSensor):
    """The measured voltage the battery level is estimated from."""

    _key = "battery_voltage"
    _attr_name = "Battery voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_suggested_display_precision = 0
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The Battery sensor shows the level; enable this for the raw voltage.
    _attr_entity_registry_enabled_default = False
    _value_fn = staticmethod(lambda status: status.battery_mv)


class SmartMaticRefillLevelSensor(SmartMaticSensor):
    _key = "refill_level"
    _attr_name = "Refill level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cup-water"
    _value_fn = staticmethod(lambda status: status.refill_percent)


class SmartMaticSprayCountSensor(SmartMaticSensor):
    _key = "spray_count"
    _attr_name = "Spray count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"
    _value_fn = staticmethod(lambda status: status.total_spray_count)


class SmartMaticChannelSensor(SensorEntity):
    """A detected characteristic, fixed until the device is reconfigured."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-settings"
    _attr_should_poll = False
    # Only needed for troubleshooting: enable it on the device page.
    _attr_entity_registry_enabled_default = False

    def __init__(self, instance, config_entry, key, name, uuid):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_value = uuid[:8] if uuid else "not detected"
        self._attr_extra_state_attributes = {"uuid": uuid}

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )


class SmartMaticSignalStrengthSensor(SensorEntity):
    """RSSI of the device's advertisements, and which adapter or proxy heard them."""

    _attr_has_entity_name = True
    _attr_name = "Signal strength"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    # Advertisements arrive several times a second; record changes only.
    _MIN_INTERVAL = 60
    _MIN_CHANGE = 5

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_signal_strength"
        self._last_write = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        mac = self._instance.mac
        info = bluetooth.async_last_service_info(self.hass, mac, connectable=True)
        if info is not None:
            self._update(info)
            self.async_write_ha_state()
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                BluetoothCallbackMatcher({ADDRESS: mac}),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    @callback
    def _on_advertisement(self, info, change) -> None:
        previous = self._attr_native_value
        if (
            previous is not None
            and info.source == (self._attr_extra_state_attributes or {}).get("source")
            and abs(info.rssi - previous) < self._MIN_CHANGE
            and time.monotonic() - self._last_write < self._MIN_INTERVAL
        ):
            return
        self._update(info)
        self.async_write_ha_state()

    def _update(self, info) -> None:
        scanner = bluetooth.async_scanner_by_source(self.hass, info.source)
        self._attr_native_value = info.rssi
        self._attr_extra_state_attributes = {
            "source": info.source,
            "source_name": getattr(scanner, "name", None),
        }
        self._last_write = time.monotonic()



class SmartMaticUpdatedLastSensor(SensorEntity):
    """When the last status read succeeded.

    Named to sort next to the Update status button on the device page.
    """

    _attr_has_entity_name = True
    _attr_name = "Updated last"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        # Not "last_update": that was an older entity, removed on upgrade.
        self._attr_unique_id = f"{config_entry.entry_id}_updated_last"
        self._attr_native_value = instance.last_update

    async def async_added_to_hass(self) -> None:
        self._instance.eventbus.add_listener(DEVICE_STATUS_UPDATE, self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        self._instance.eventbus.remove_listener(DEVICE_STATUS_UPDATE, self._on_update)

    async def _on_update(self, _status) -> None:
        self._attr_native_value = self._instance.last_update
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(
            self._instance, self._config_entry.data[CONF_NAME]
        )
