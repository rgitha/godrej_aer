from homeassistant.exceptions import HomeAssistantError


class InvalidDeviceError(HomeAssistantError):
    pass

class NotConnectedError(HomeAssistantError):
    pass

class ConnectionError(HomeAssistantError):
    pass

class CommandNotConfirmed(HomeAssistantError):
    """The device did not acknowledge a command (or rejected it)."""

class NotIdle(HomeAssistantError):
    """Identify needs the device idle: no auto-spray, no schedule run.
    ``schedule`` says a schedule run is what keeps it busy."""

    def __init__(self, message: str, *, schedule: bool = False):
        super().__init__(message)
        self.schedule = schedule

class BatteryTooLow(HomeAssistantError):
    """A spray command was acknowledged, but the batteries are below the
    device's cut-off, so the motor won't run."""
