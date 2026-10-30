"""Services for the Godrej Aer Smart Matic integration."""
from __future__ import annotations

import asyncio
import inspect
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import DOMAIN
from .godrej import SmartMatic

_LOGGER = logging.getLogger(__name__)

SERVICE_UPDATE_STATUS = "update_status"


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

        results = await asyncio.gather(
            *(instance.connect(purpose="update status") for instance in instances),
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

    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_STATUS)
