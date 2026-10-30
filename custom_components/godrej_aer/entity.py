from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from .const import DOMAIN
from .godrej import SmartMatic
from .godrej.exception import BatteryTooLow, CommandNotConfirmed, ConnectionError, NotIdle


class AcceptedButFlat(HomeAssistantError):
    """The device took the command, but its batteries are too low to spray."""


async def async_send_command(command: Awaitable[None]) -> None:
    """Run a device command, turning failures into a translated HA error.

    The frontend shows it as a toast on the control that was used.
    """
    try:
        await command
    except BatteryTooLow as err:
        raise AcceptedButFlat(
            translation_domain=DOMAIN,
            translation_key="battery_too_low",
        ) from err
    except NotIdle as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="schedule_running" if err.schedule else "auto_running",
        ) from err
    except ConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_reach",
        ) from err
    except CommandNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_not_confirmed",
        ) from err


@callback
def async_device_device_info_fn(smartmatic: SmartMatic, name: str) -> DeviceInfo:
    status = smartmatic.device_status
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, smartmatic.mac)},
        manufacturer="Godrej Aer",
        model="Smart Matic",
        name=name,
        sw_version=status.firmware_version if status else None,
    )
