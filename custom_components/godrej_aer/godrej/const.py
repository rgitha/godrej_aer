"""Device- and BLE-connection constants for the Godrej Aer Smart Matic."""

# Nordic UART-style GATT service and characteristics exposed by the device.
MAIN_SVC = "6e400000-b5a3-f393-e0a9-e50e24dcca9e"
# Replies arrive here on all firmware seen so far (the vendor app's
# RESPONSE characteristic). It has no CCCD; see _raw_proxy_notify.
NOTIFY_CHAR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# Default channels: commands on the UART RX characteristic, replies on
# NOTIFY_CHAR. Fw 3.0.8-IN accepts commands on 6e400002, 6e400003 and
# 6e400004 alike. Setup checks the defaults and only falls back to probing
# every writable characteristic (these first) if they don't answer.
COMMAND_CHAR_CANDIDATES = (
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400004-b5a3-f393-e0a9-e50e24dcca9e",
)
DEFAULT_COMMAND_CHAR = COMMAND_CHAR_CANDIDATES[0]
# The official app writes schedule uploads (message type 103) here only.
SCHEDULE_CHAR = COMMAND_CHAR_CANDIDATES[2]
DEFAULT_REPLY_CHAR = NOTIFY_CHAR

# Seconds to stay connected after a successful exchange before dropping the
# link (keeping it open drains the device battery).
DISCONNECT_DELAY = 15
# Seconds to wait for the status notification after asking for it. Replies
# usually take well under a second; the slowest seen took about 3 s.
STATUS_TIMEOUT = 5
# Seconds to wait for a reply from each candidate while probing (replies
# have been seen to take up to ~3 s).
PROBE_TIMEOUT = 8
# Total time detection may take (defaults check plus fallback probing).
DETECT_TIMEOUT = 60
# Schedules: seconds to wait for their replies, and the pause the app
# leaves between "remove all" and the upload that follows it.
SCHEDULE_REPLY_TIMEOUT = 5
SCHEDULE_PAUSE = 3
# Seconds to wait for a command's acknowledgement, counted from its write.
# Fresh links answer in ~0.1 s; on a link left open for a few seconds the
# device has been seen to take just over 3 s.
ACK_TIMEOUT = 5
# Every connection (presses, polls, setup) makes one attempt of at most
# this many seconds; a working device connects in about 1 s. This keeps
# press feedback quick even when a poll is connecting at the same time.
# (bleak-retry-connector's own default is 4 attempts of up to 20 s.)
CONNECT_ATTEMPTS = 1
CONNECT_TIMEOUT = 5
# Seconds to wait after connecting before sending a button command.
# Polls wait 2 s, like the official app.
ACTION_SETTLE = 0.5
# Seconds to back off background polls after consecutive failed attempts:
# short at first (e.g. a proxy still reconnecting after a restart), then
# every 5 minutes while the device stays unreachable. A success resets it.
CONNECT_RETRY_BACKOFF = (30, 60, 120, 300)

# Auto-spray intervals the device (and the official app) support, in minutes.
SPRAY_INTERVALS = (10, 20, 40)
# Interval used when auto-spray is switched on without an explicit choice
# (matches the official app's on/off toggle).
DEFAULT_SPRAY_INTERVAL = 10
# The device reports the open-circuit voltage of its two AA cells and
# stops driving the motor below 2.4 V (it still acknowledges sprays).
BATTERY_CUTOFF_MV = 2400
# (mV, %) points of the battery level, interpolated linearly in between:
# an alkaline cell's light-load discharge curve (a fit to datasheet points
# at 1 mA, per cell, doubled), rescaled so the cut-off is 0 %.
BATTERY_CURVE = (
    (3100, 100),
    (3000, 95),
    (2900, 91),
    (2800, 86),
    (2700, 78),
    (2600, 57),
    (2500, 29),
    (2450, 14),
    (BATTERY_CUTOFF_MV, 0),
)
# Sprays a full cartridge is rated for; used to derive a refill percentage.
SPRAY_CAPACITY = 2200
