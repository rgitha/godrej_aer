import logging
import struct
from datetime import datetime, timedelta

from . import cbor
from .const import BATTERY_CURVE, SPRAY_CAPACITY

_LOGGER = logging.getLogger(__name__)

DEVICE_STATUS_VALIDITY_TIME = timedelta(minutes=60)

# Frame length the fixed-offset fallback decoder expects.
STATUS_LEN = 99

STATUS_REPLY_MT = 157  # fetch-status (107) + 50
ACK_OK = 200

# currentStatus (cS) -> auto-spray interval in minutes. The tens digit
# encodes the trigger (spray-now / schedule / button), the units digit the
# interval. 111 / 0 means idle. Mirrors AppUtility.getCurrentDeviceInterval().
_INTERVAL_BY_STATUS = {
    112: 10, 121: 10, 131: 10,
    113: 20, 122: 20, 132: 20,
    114: 40, 123: 40, 133: 40,
}

# What started auto-spraying, by the tens digit of cS: 11x from the app (or
# Home Assistant), 12x a schedule stored on the device, 13x its button.
_SOURCE_BY_STATUS_GROUP = {11: "app", 12: "schedule", 13: "button"}

# sC sub-array -> counter names, in array order.
_COUNTER_KEYS = {
    "sn": ("spray_now_0", "spray_now_10", "spray_now_20", "spray_now_40"),
    "sc": ("schedule_10", "schedule_20", "schedule_40"),
    "bn": ("button_10", "button_20", "button_40"),
}


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _ascii(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("ascii", "replace").strip("\x00 ").strip()


def _int_or_none(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _str_or_none(value):
    return value.strip() if isinstance(value, str) else None


class DeviceStatus:
    battery_mv: int | None = None
    current_status: int | None = None
    firmware_version: str | None = None
    structure_version: str | None = None
    total_spray_count: int | None = None

    def __init__(self, status_bytes: bytes):
        self.date = datetime.now()
        self.raw = bytes(status_bytes)
        self.message_type = None
        self.message_id = None
        self.acknowledgement = None
        self.spray_counts = {}

        try:
            message = cbor.loads(self.raw)
        except cbor.CBORError as e:
            _LOGGER.debug("Status frame is not valid CBOR (%s)", e)
            message = None

        if isinstance(message, dict):
            self._from_message(message)
        else:
            self._from_fixed_offsets(self.raw)

    def _from_message(self, message: dict):
        self.message_type = message.get("mT")
        self.message_id = message.get("mN")
        self.acknowledgement = message.get("ack")

        if self.message_type != STATUS_REPLY_MT:
            return  # e.g. a command acknowledgement
        if self.acknowledgement is not None and self.acknowledgement != ACK_OK:
            _LOGGER.debug("Status reply not acknowledged (ack=%s)", self.acknowledgement)
            return

        self.battery_mv = _int_or_none(message.get("bV"))
        self.firmware_version = _str_or_none(message.get("fV"))
        self.current_status = _int_or_none(message.get("cS"))
        self.structure_version = _str_or_none(message.get("sV"))

        counters = message.get("sC")
        if not isinstance(counters, dict):
            return

        counts = {}
        for array_key, names in _COUNTER_KEYS.items():
            values = counters.get(array_key)
            if not isinstance(values, list):
                continue
            for name, value in zip(names, values):
                value = _int_or_none(value)
                if value is not None:
                    counts[name] = value

        if counts:
            self.spray_counts = counts
            self.total_spray_count = sum(counts.values())

    def _from_fixed_offsets(self, status_bytes: bytes):
        """Fallback for frames that are not valid CBOR."""
        if len(status_bytes) != STATUS_LEN or status_bytes[5] != STATUS_REPLY_MT:
            _LOGGER.debug(
                "Cannot decode status frame (%d bytes): %s",
                len(status_bytes), status_bytes.hex()
            )
            return

        self.message_type = status_bytes[5]
        self.message_id = status_bytes[10]
        self.acknowledgement = status_bytes[16]

        self.battery_mv = struct.unpack_from(">I", status_bytes, 21)[0]
        self.firmware_version = _ascii(status_bytes, 29, 37)
        self.current_status = status_bytes[41]
        self.structure_version = _ascii(status_bytes, 92, 98)

        # Per-trigger, per-interval spray counters (big-endian uint16).
        try:
            counts = {
                "spray_now_0": _u16(status_bytes, 51),
                "spray_now_10": _u16(status_bytes, 54),
                "spray_now_20": _u16(status_bytes, 57),
                "spray_now_40": _u16(status_bytes, 60),
                "schedule_10": _u16(status_bytes, 67),
                "schedule_20": _u16(status_bytes, 70),
                "schedule_40": _u16(status_bytes, 73),
                "button_10": _u16(status_bytes, 80),
                "button_20": _u16(status_bytes, 83),
                "button_40": _u16(status_bytes, 86),
            }
            self.spray_counts = counts
            self.total_spray_count = sum(counts.values())
        except struct.error:
            self.spray_counts = {}

    @property
    def battery_volts(self) -> float | None:
        if self.battery_mv is None:
            return None
        return round(self.battery_mv / 1000, 3)

    @property
    def battery_percent(self) -> int | None:
        """Estimated battery level, 0-100, from the voltage (BATTERY_CURVE)."""
        mv = self.battery_mv
        if mv is None:
            return None
        if mv >= BATTERY_CURVE[0][0]:
            return BATTERY_CURVE[0][1]
        for (hi_mv, hi_pct), (lo_mv, lo_pct) in zip(BATTERY_CURVE, BATTERY_CURVE[1:]):
            if mv >= lo_mv:
                return round(lo_pct + (hi_pct - lo_pct) * (mv - lo_mv) / (hi_mv - lo_mv))
        return 0

    @property
    def spray_interval(self) -> int:
        """Auto-spray interval in minutes (0 when idle)."""
        return _INTERVAL_BY_STATUS.get(self.current_status, 0)

    @property
    def spray_source(self) -> str | None:
        """What started auto-spraying: "app", "schedule" or "button"."""
        if not self.is_spraying:
            return None
        return _SOURCE_BY_STATUS_GROUP.get(self.current_status // 10)

    def describe(self) -> str:
        """A one-line summary for the debug log."""
        if self.is_spraying:
            mode = f"auto every {self.spray_interval} min, started by {self.spray_source}"
        else:
            mode = "auto off"
        c = self.spray_counts

        def group(prefix):
            return "/".join(str(c.get(f"{prefix}_{m}", "?")) for m in (10, 20, 40))

        counts = (
            f"Total {self.total_spray_count}: now {c.get('spray_now_0', '?')}; "
            f"auto {group('spray_now')}; scheduled {group('schedule')}; "
            f"button {group('button')} (each 10/20/40 min)"
        ) if c else "no counters"
        return (
            f"{mode} (cS {self.current_status}); battery {self.battery_mv} mV "
            f"({self.battery_percent}%); Sprays: {counts}; firmware {self.firmware_version}"
        )

    @property
    def is_spraying(self) -> bool:
        """True when the device is in an auto-spray mode."""
        return self.spray_interval > 0

    @property
    def refill_percent(self) -> int | None:
        """Estimated cartridge level, 0-100, from the lifetime spray count."""
        if self.total_spray_count is None:
            return None
        used = min(self.total_spray_count, SPRAY_CAPACITY)
        return round((SPRAY_CAPACITY - used) / SPRAY_CAPACITY * 100)

    @property
    def is_valid(self):
        return self.battery_mv is not None and \
            datetime.now() < self.date + DEVICE_STATUS_VALIDITY_TIME
