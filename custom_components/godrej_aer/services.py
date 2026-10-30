"""Services for the Godrej Aer Smart Matic integration."""
from __future__ import annotations

import asyncio
import inspect
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import DOMAIN
from .entity import async_send_command
from .godrej import SmartMatic
from .godrej.const import SPRAY_INTERVALS
from .godrej.schedule import Schedule, ScheduleError

_LOGGER = logging.getLogger(__name__)

SERVICE_UPDATE_STATUS = "update_status"
SERVICE_SET_SCHEDULES = "set_schedules"
SERVICE_ADD_SCHEDULE = "add_schedule"
SERVICE_REMOVE_SCHEDULE = "remove_schedule"
SERVICE_CLEAR_SCHEDULES = "clear_schedules"
SERVICE_STOP_SCHEDULE = "stop_schedule"
SCHEDULE_SERVICES = (
    SERVICE_SET_SCHEDULES, SERVICE_ADD_SCHEDULE,
    SERVICE_REMOVE_SCHEDULE, SERVICE_CLEAR_SCHEDULES,
    SERVICE_STOP_SCHEDULE,
)

_SCHEDULE_FIELDS = {
    vol.Required("days"): vol.All(cv.ensure_list, [cv.string]),
    vol.Required("start"): cv.time,
    vol.Required("end"): cv.time,
    vol.Required("interval"): vol.All(vol.Coerce(int), vol.In(SPRAY_INTERVALS)),
}
_TARGET_KEYS = {
    vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
    vol.Optional("entity_id"): cv.comp_entity_ids,
    vol.Optional("area_id"): vol.Any(cv.string, [cv.string]),
}
SET_SCHEDULES_SCHEMA = vol.Schema({
    **_TARGET_KEYS,
    vol.Required("schedules"): vol.All(cv.ensure_list, [vol.Schema(_SCHEDULE_FIELDS)]),
})
ADD_SCHEDULE_SCHEMA = vol.Schema({**_TARGET_KEYS, **_SCHEDULE_FIELDS})
REMOVE_SCHEDULE_SCHEMA = vol.Schema({
    **_TARGET_KEYS,
    vol.Required("number"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
})
CLEAR_SCHEDULES_SCHEMA = vol.Schema(_TARGET_KEYS)
SCHEDULE_TARGET_SCHEMA = vol.Schema({
    **_TARGET_KEYS,
    vol.Required("schedule"): vol.Any(
        vol.In(["current", "all"]),
        vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
    ),
})


def _schedule_from(data: dict) -> Schedule:
    try:
        return Schedule.create(data["days"], data["start"], data["end"], data["interval"])
    except ScheduleError as e:
        raise ServiceValidationError(str(e)) from e


async def _extract_config_entry_ids(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Call the helper with the signature this Home Assistant expects.

    It took ``(hass, call)`` until HA 2025.x, then ``(call)``; the old form
    was removed in 2026.10.
    """
    params = list(inspect.signature(async_extract_config_entry_ids).parameters)
    if params and params[0] == "hass":
        return await async_extract_config_entry_ids(hass, call)
    return await async_extract_config_entry_ids(call)


async def _async_matched_instances(
    hass: HomeAssistant, call: ServiceCall
) -> list[SmartMatic]:
    """Return the SmartMatic instances targeted by a service call.

    Resolves the call's area/device/entity target to loaded config entries.
    With no target, every loaded Godrej Aer entry is used.
    """
    entry_ids = await _extract_config_entry_ids(hass, call)

    instances: list[SmartMatic] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_ids and entry.entry_id not in entry_ids:
            continue
        instance: SmartMatic | None = getattr(entry, "runtime_data", None)
        if instance is not None:
            instances.append(instance)

    return instances


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration-level services (once per HA start)."""

    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_STATUS):
        return

    async def _async_update_status(call: ServiceCall) -> None:
        instances = await _async_matched_instances(hass, call)
        _LOGGER.debug("Update status action run for %s", [i.mac for i in instances])
        if not instances:
            raise HomeAssistantError(
                "No loaded Godrej Aer Smart Matic devices matched the service call"
            )

        async def _update(instance: SmartMatic) -> None:
            await instance.connect(purpose="update status")
            await instance.read_schedules()

        results = await asyncio.gather(
            *(_update(instance) for instance in instances),
            return_exceptions=True,
        )

        errors = [
            (instance, result)
            for instance, result in zip(instances, results)
            if isinstance(result, Exception)
        ]
        for instance, error in errors:
            _LOGGER.warning(
                "Manual status update failed for %s: %s", instance.mac, error
            )
        if errors:
            first_instance, first_error = errors[0]
            raise HomeAssistantError(
                f"Status update failed for {len(errors)} device(s); "
                f"{first_instance.mac}: {first_error}"
            )

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_STATUS, _async_update_status
    )

    async def _for_each_device(call: ServiceCall, change) -> None:
        """Apply a schedule change to every targeted device, one by one."""
        instances = await _async_matched_instances(hass, call)
        _LOGGER.debug(
            "%s action run for %s", call.service.replace("_", " ").capitalize(),
            [i.mac for i in instances],
        )
        if not instances:
            raise HomeAssistantError(
                "No loaded Godrej Aer Smart Matic devices matched the service call"
            )
        for instance in instances:
            try:
                await async_send_command(change(instance))
            except ScheduleError as e:
                raise ServiceValidationError(str(e)) from e

    async def _async_set_schedules(call: ServiceCall) -> None:
        schedules = [_schedule_from(item) for item in call.data["schedules"]]
        await _for_each_device(call, lambda i: i.set_schedules(schedules))

    async def _async_add_schedule(call: ServiceCall) -> None:
        schedule = _schedule_from(call.data)
        await _for_each_device(call, lambda i: i.add_schedule(schedule))

    async def _async_remove_schedule(call: ServiceCall) -> None:
        number = call.data["number"]
        await _for_each_device(call, lambda i: i.remove_schedule(number))

    async def _async_clear_schedules(call: ServiceCall) -> None:
        await _for_each_device(call, lambda i: i.set_schedules([]))

    async def _async_stop_schedule(call: ServiceCall) -> None:
        target = call.data["schedule"]
        await _for_each_device(call, lambda i: i.stop_schedule(target))

    for name, handler, schema in (
        (SERVICE_STOP_SCHEDULE, _async_stop_schedule, SCHEDULE_TARGET_SCHEMA),
        (SERVICE_SET_SCHEDULES, _async_set_schedules, SET_SCHEDULES_SCHEMA),
        (SERVICE_ADD_SCHEDULE, _async_add_schedule, ADD_SCHEDULE_SCHEMA),
        (SERVICE_REMOVE_SCHEDULE, _async_remove_schedule, REMOVE_SCHEDULE_SCHEMA),
        (SERVICE_CLEAR_SCHEDULES, _async_clear_schedules, CLEAR_SCHEDULES_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema)


@callback
def async_unload_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove services once the last config entry is being unloaded.

    ``entry`` is the entry currently unloading; it may still report as
    LOADED here, so it is excluded from the check.
    """

    if any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        return

    for name in (SERVICE_UPDATE_STATUS, *SCHEDULE_SERVICES):
        hass.services.async_remove(DOMAIN, name)

