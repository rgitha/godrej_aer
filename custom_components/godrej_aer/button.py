from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_device_device_info_fn, async_send_command
from .godrej import SmartMatic


_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticSprayNowButton(instance, config_entry),
        SmartMaticResetRefillButton(instance, config_entry),
        SmartMaticUpdateStatusButton(instance, config_entry),
        SmartMaticIdentifyButton(instance, config_entry),
    ])


class SmartMaticButton(ButtonEntity):
    """Base for Smart Matic buttons."""

    _key: str
    _attr_has_entity_name = True

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry: ConfigEntry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{self._key}"

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])


class SmartMaticSprayNowButton(SmartMaticButton):
    """Spray once, now."""

    _key = "trigger"
    _attr_name = "Spray now"
    _attr_icon = "mdi:spray"

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Spray now pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.spray_now())


class SmartMaticResetRefillButton(SmartMaticButton):
    """Tell the device its cartridge has been refilled."""

    _key = "reset_refill"
    _attr_name = "Reset refill level"
    _attr_icon = "mdi:cup-water"
    # Its own card on the device page, away from the everyday controls.
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Reset refill level pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.reset_refill())


class SmartMaticUpdateStatusButton(SmartMaticButton):
    """Read the status and schedules now, like the Update status action."""

    _key = "update_status"
    _attr_name = "Update status"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Update status pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._update())

    async def _update(self) -> None:
        await self._instance.connect(purpose="update status")
        await self._instance.read_schedules()


class SmartMaticIdentifyButton(SmartMaticButton):
    """Light the device's three interval lights for a few seconds (HA's
    standard identify button: the type only sets its icon and name)."""

    _key = "identify"
    _attr_name = "Identify"
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Identify pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.identify())
