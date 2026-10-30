from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import AcceptedButFlat, async_device_device_info_fn, async_send_command
from .godrej import SmartMatic, DeviceStatus
from .godrej.const import SPRAY_INTERVALS
from .godrej.events import DEVICE_STATUS_UPDATE

OFF = "Off"
_INTERVAL_BY_OPTION = {f"Every {minutes} min": minutes for minutes in SPRAY_INTERVALS}
_OPTION_BY_INTERVAL = {minutes: option for option, minutes in _INTERVAL_BY_OPTION.items()}


_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([SmartMaticAutoSpraySelect(instance, config_entry)])


class SmartMaticAutoSpraySelect(SelectEntity):
    """Auto-spray mode: Off, or every 10, 20 or 40 minutes.

    One setting, as on the device. The state is what the device reports,
    and "started_by" says what started auto-spraying. Choosing the
    interval already running sends nothing: the device would restart the
    cycle with a spray. Off is always sent (it also clears the lights).
    """

    _attr_icon = "mdi:spray"
    _attr_options = [OFF, *_INTERVAL_BY_OPTION]

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance = instance
        self._config_entry = config_entry
        self._attr_has_entity_name = True
        self._attr_name = "Spray: auto"
        self._attr_unique_id = f"{config_entry.entry_id}_spray_mode"
        self._apply_status(instance.device_status)

    def _apply_status(self, status: DeviceStatus | None) -> None:
        if status is None or status.current_status is None:
            self._attr_current_option = None
            self._attr_extra_state_attributes = {}
            return
        if status.is_spraying:
            self._attr_current_option = _OPTION_BY_INTERVAL.get(status.spray_interval)
        else:
            self._attr_current_option = OFF
        self._attr_extra_state_attributes = {"started_by": status.spray_source}

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

    async def async_select_option(self, option: str) -> None:
        _LOGGER.debug("%s: Spray: auto set to %s", self._config_entry.data[CONF_NAME], option)
        status = self._instance.device_status
        if (
            option != OFF
            and status is not None
            and status.is_spraying
            and _OPTION_BY_INTERVAL.get(status.spray_interval) == option
        ):
            _LOGGER.debug("Already spraying every %s min; nothing sent", status.spray_interval)
            return
        if option == OFF:
            command = self._instance.stop_spray()
        else:
            command = self._instance.set_spray_interval(_INTERVAL_BY_OPTION[option])
        # Show the option once the device acknowledges it; otherwise
        # re-write the current one (the dropdown reverts) and raise. The
        # status read-back that follows shows what the device really does
        # (e.g. it ignores a different interval during a schedule run).
        try:
            await async_send_command(command)
        except AcceptedButFlat:
            # The device did switch modes; it just can't spray.
            self._attr_current_option = option
            self.async_write_ha_state()
            raise
        except Exception:
            self.async_write_ha_state()
            raise
        self._attr_current_option = option
        self.async_write_ha_state()

    async def on_status_update(self, device_status: DeviceStatus):
        self._apply_status(device_status)
        self.async_write_ha_state()
