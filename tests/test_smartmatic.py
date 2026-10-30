"""SmartMatic against simulated devices for each known firmware variant."""
import asyncio
import functools

import pytest

from godrej import cbor
from godrej import godrej as gm
from godrej.const import MAIN_SVC, NOTIFY_CHAR
from godrej.exception import BatteryTooLow, CommandNotConfirmed
from godrej.exception import ConnectionError as DeviceConnectionError

from test_devicestatus import FRAME_SHIFTED, FRAME_308_IN

C1 = NOTIFY_CHAR
C2 = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
C3 = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
C4 = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
SPRAY_ACK = bytes.fromhex("bf626d54189a626d4e189a6361636b18c8ff")


class FakeChar:
    def __init__(self, uuid, handle, properties):
        self.uuid = uuid
        self.handle = handle
        self.properties = properties


class FakeService:
    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.characteristics = characteristics

    def get_characteristic(self, uuid):
        return next((c for c in self.characteristics if c.uuid == uuid), None)


class FakeServices:
    def __init__(self, services):
        self._services = services

    def __iter__(self):
        return iter(self._services)

    def get_service(self, uuid):
        return next((s for s in self._services if s.uuid == uuid), None)


class FakeProxyApi:
    """The slice of aioesphomeapi's client that the raw fallback uses."""

    def __init__(self, device):
        self.device = device

    async def bluetooth_gatt_start_notify(self, address, handle, callback):
        uuid = self.device.uuid_by_handle[handle]
        if uuid in self.device.broken_notify:
            raise Exception("raw notify failed")
        self.device.callbacks[uuid] = lambda data: callback(handle, bytearray(data))
        self.device.raw_subscriptions.append(uuid)

        async def stop():
            self.device.callbacks.pop(uuid, None)

        return stop, lambda: self.device.callbacks.pop(uuid, None)


class FakeBackend:
    def __init__(self, device):
        self._client = FakeProxyApi(device)
        self._address_as_int = 0xA4C138CB0337
        self._notify_cancels = {}


class FakeDevice:
    """A Smart Matic that answers on ``command_char`` (one or a tuple).

    Commands are acknowledged (reply mT = request mT + 50) with ``ack``,
    or not at all if it is None. ``echo_mn``: the status reply carries the
    request's mN. ``reply_delay``: seconds before the status reply.

    ``via_proxy``: start_notify fails on characteristics without a CCCD
    (6e400001), as bleak-esphome does on v3 proxy connections.
    ``chunk``: notification payload size (ATT MTU - 3).
    ``needs_time``: stays silent until it has received a set-time message.
    """

    def __init__(
        self, *, command_char, status_frame, reply_char=C1,
        via_proxy=True, chunk=182, needs_time=False, extra_chars=(),
        ack=200, echo_mn=False, reply_delay=0.0,
    ):
        self.command_char = command_char
        self.ack = ack
        self.echo_mn = echo_mn
        self.reply_delay = reply_delay
        self.status_frame = status_frame
        self.reply_char = reply_char
        self.via_proxy = via_proxy
        self.chunk = chunk
        self.needs_time = needs_time
        self.time_set = False

        chars = [
            FakeChar(C1, 0x10, ["read", "notify"]),
            FakeChar(C2, 0x13, ["read", "write-without-response", "notify"]),
            FakeChar(C3, 0x17, ["read", "write-without-response", "notify"]),
            FakeChar(C4, 0x1B, ["read", "write-without-response", "notify"]),
            *extra_chars,
        ]
        self.uuid_by_handle = {c.handle: c.uuid for c in chars}
        self.services = FakeServices([FakeService(MAIN_SVC, chars)])
        self.callbacks = {}
        self.raw_subscriptions = []
        self.subscribe_attempts = []
        self.broken_notify = set()
        self.writes = []

    def make_client(self, disconnected_callback):
        return FakeClient(self, disconnected_callback)

    def _notify(self, uuid, frame, delay=0.0):
        def send():
            callback = self.callbacks.get(uuid)
            if callback is None:
                return
            for i in range(0, len(frame), self.chunk):
                callback(frame[i:i + self.chunk])

        asyncio.get_running_loop().call_later(delay, send)

    def on_write(self, uuid, payload):
        self.writes.append((uuid, bytes(payload)))
        accepted = (
            self.command_char if isinstance(self.command_char, tuple)
            else (self.command_char,)
        )
        if uuid not in accepted:
            return
        message = cbor.loads(bytes(payload))
        mt = message["mT"]
        if mt == 102:  # set time
            self.time_set = True
        elif mt == 107:
            if self.status_frame and (self.time_set or not self.needs_time):
                frame = self.status_frame
                if self.echo_mn:
                    frame = frame.replace(
                        bytes.fromhex("626d4e189d"),
                        bytes.fromhex("626d4e18") + bytes([message["mN"]]), 1,
                    )
                self._notify(self.reply_char, frame, self.reply_delay)
        elif self.ack is not None:
            self._notify(
                self.reply_char,
                gm.encode_message(mT=mt + 50, mN=mt + 50, ack=self.ack),
            )


class FakeClient:
    def __init__(self, device, disconnected_callback):
        self.device = device
        self.services = device.services
        self.is_connected = True
        self.mtu_size = device.chunk + 3
        self._disconnected_callback = disconnected_callback
        if device.via_proxy:
            self._backend = FakeBackend(device)

    async def start_notify(self, char, callback):
        self.device.subscribe_attempts.append(char.uuid)
        if char.uuid in self.device.broken_notify:
            raise Exception("notify failed")
        if self.device.via_proxy and char.uuid == C1:
            raise Exception(
                f"Characteristic {char.uuid} does not have a characteristic "
                "client config descriptor."
            )

        def deliver(data):
            asyncio.get_running_loop().create_task(callback(char, bytearray(data)))

        self.device.callbacks[char.uuid] = deliver

    async def write_gatt_char(self, uuid, payload):
        self.device.on_write(uuid, payload)

    async def disconnect(self):
        self.is_connected = False


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gm, "PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(gm, "ACK_TIMEOUT", 0.2)
    real_sleep = asyncio.sleep

    async def short_sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01), *args, **kwargs)

    monkeypatch.setattr(gm.asyncio, "sleep", short_sleep)


def attach(monkeypatch, device, **kwargs):
    async def establish_connection(_cls, _ble_device, _mac, disconnected_callback, **_kwargs):
        return device.make_client(disconnected_callback)

    monkeypatch.setattr(gm, "establish_connection", establish_connection)
    return gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37", **kwargs)


def probes(device):
    return [uuid for uuid, payload in device.writes if cbor.loads(payload)["mT"] == 107]


ALL = (C2, C3, C4)  # fw 3.0.8-IN takes commands on any of them


def short_status_timeout(monkeypatch, matic, timeout=0.2):
    """Make connect() (as called by connect_if_needed) give up quickly."""
    monkeypatch.setattr(
        matic, "connect", functools.partial(type(matic).connect, matic, status_timeout=timeout)
    )


# --- detection (adding a device, Reconfigure) -------------------------------

async def test_detect_accepts_working_defaults(monkeypatch):
    """Fw 3.0.8-IN through a proxy: the defaults answer, nothing is probed."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C2, C1)
    assert device.subscribe_attempts == [C1]  # only the default reply channel
    assert device.raw_subscriptions == [C1]  # via the CCCD-less fallback
    assert probes(device) == [C2]
    assert device.writes[0] == (C2, gm.FETCH_STATUS_MSG)  # the app's request
    assert device.time_set
    assert matic.device_status.firmware_version == "3.0.8-IN"
    assert matic.client is None  # disconnected afterwards


async def test_detect_falls_back_to_probing(monkeypatch):
    """A firmware that only takes commands on 0004."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C2, C3, C4]  # defaults, then each candidate
    assert set(device.subscribe_attempts) == {C1, C2, C3, C4}
    assert device.time_set


async def test_detect_other_frame_length_fragmented_on_local_adapter(monkeypatch):
    """Commands on 0003 only, 105-byte reply in 20-byte chunks."""
    device = FakeDevice(
        command_char=C3, status_frame=FRAME_SHIFTED, via_proxy=False, chunk=20,
    )
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C3, C1)
    assert device.raw_subscriptions == []
    assert matic.device_status.firmware_version == "3.0.10-T1x"


async def test_detect_finds_replies_on_another_characteristic(monkeypatch):
    device = FakeDevice(command_char=C2, reply_char=C2, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C2, C2)
    assert probes(device) == [C2, C2]


async def test_detect_device_that_needs_time_first(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, needs_time=True)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C2, C3, C4, C2, C3, C4]


async def test_detect_matches_late_replies_by_message_number(monkeypatch):
    """A reply that arrives after its probe timed out is credited correctly."""
    device = FakeDevice(
        command_char=C3, status_frame=FRAME_308_IN, echo_mn=True, reply_delay=0.3,
    )
    matic = attach(monkeypatch, device)

    # The reply to the 0003 probe only arrives while 0004 is being probed.
    assert await matic.detect_channels() == (C3, C1)


async def test_detect_without_replies(monkeypatch):
    device = FakeDevice(command_char=ALL, status_frame=b"")
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels(timeout=1) is None
    assert matic.client is None


async def test_detect_rejects_other_devices(monkeypatch):
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    device.services = FakeServices([])
    matic = attach(monkeypatch, device)

    with pytest.raises(gm.InvalidDeviceError):
        await matic.detect_channels()
    assert device.writes == []
    assert matic.client is None


async def test_detect_respects_the_time_budget(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    # 0.5s covers the defaults check and the 0002 probe, not 0004.
    assert await matic.detect_channels(timeout=0.5) is None
    assert C4 not in probes(device)
    assert matic.client is None


async def test_detect_on_a_running_instance_leaves_its_channels_alone(monkeypatch):
    """Reconfigure checks on a fresh link; the entry reload applies the result."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]

    device.command_char = C3  # e.g. after a firmware update
    assert await matic.detect_channels() == (C3, C1)
    assert (matic.command_char, matic.reply_char) == (C4, C1)

    device.status_frame = b""
    assert await matic.detect_channels(timeout=1) is None
    assert (matic.command_char, matic.reply_char) == (C4, C1)


# --- runtime: saved channels are used as-is ---------------------------------

async def test_saved_channels_are_used_without_probing(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]
    assert matic.device_status.battery_mv == 3170
    await matic.spray_now()
    await matic._refresh_task
    assert {uuid for uuid, _ in device.writes} == {C4}


async def test_missed_reply_keeps_the_saved_channels(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)

    device.status_frame = b""  # e.g. weak signal
    with pytest.raises(DeviceConnectionError):
        await matic.get_device_status(timeout=0.2)
    assert (matic.command_char, matic.reply_char) == (C4, C1)
    assert probes(device) == [C4, C4]


async def test_broken_saved_reply_channel_has_no_fallback(monkeypatch):
    device = FakeDevice(command_char=C4, reply_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C3)
    device.broken_notify = {C3}

    with pytest.raises(DeviceConnectionError):
        await matic.connect(status_timeout=0.2)
    assert device.subscribe_attempts == [C3]
    assert (matic.command_char, matic.reply_char) == (C4, C3)


async def test_entry_without_saved_channels_uses_the_defaults(monkeypatch, caplog):
    """Devices added before detection existed use 0002/0001."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    caplog.set_level("WARNING", logger=gm.__name__)

    assert (matic.command_char, matic.reply_char) == (C2, C1)
    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]
    await matic.spray_now()
    await matic._refresh_task
    assert {uuid for uuid, _ in device.writes} == {C2}
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_actions_never_probe(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device)

    for _ in range(2):
        device.writes.clear()
        with pytest.raises(CommandNotConfirmed):
            await matic.spray_now()
        assert device.writes[0] == (C2, gm.SPRAY_NOW_MSG)
    matic._refresh_task.cancel()
    assert set(probes(device)) <= {C2}


async def test_startup_failure_cooldown_and_logging(monkeypatch, caplog):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    caplog.set_level("INFO", logger=gm.__name__)

    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    remaining = (matic._retry_after - gm.datetime.now()).total_seconds()
    assert 25 < remaining <= 30  # first retry soon
    assert [r.levelname for r in caplog.records if "Could not get status" in r.message] == ["WARNING"]

    # Repeated failures after the cooldown are not warned about again...
    caplog.clear()
    matic._retry_after = None
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "Could not get status" in r.message]

    # ...and recovery is reported.
    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    assert any("available again" in r.message for r in caplog.records)


# --- button presses ---------------------------------------------------------

async def test_press_subscribes_before_writing(monkeypatch):
    """The acknowledgement can only be caught with the reply channel open."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    subscribed_at_write = []
    on_write = device.on_write

    def record(uuid, payload):
        if payload == gm.SPRAY_NOW_MSG:
            subscribed_at_write.append(set(device.callbacks))
        on_write(uuid, payload)

    device.on_write = record
    await matic.spray_now()
    await matic._refresh_task

    assert subscribed_at_write == [{C1}]
    assert device.writes[0] == (C4, gm.SPRAY_NOW_MSG)
    assert matic.device_status.battery_mv == 3170


async def test_status_read_on_unsubscribed_connection_subscribes(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    async with matic._connect_lock:
        await matic._open_connection(subscribe=False)
    assert device.callbacks == {}
    assert await matic.connect(status_timeout=5)
    assert matic.device_status.battery_mv == 3170


async def test_press_returns_on_ack_before_read_back(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    await matic.spray_now()
    assert device.writes == [(C4, gm.SPRAY_NOW_MSG)]  # no read-back yet
    assert matic.device_status is None
    await matic._refresh_task
    assert matic.device_status.battery_mv == 3170


@pytest.mark.parametrize("action, mt", [
    (lambda m: m.spray_now(), 104),
    (lambda m: m.set_spray_interval(20), 104),
    (lambda m: m.stop_spray(), 106),
    (lambda m: m.reset_refill(), 105),
])
async def test_each_command_waits_for_its_own_ack(monkeypatch, caplog, action, mt):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    caplog.set_level("DEBUG", logger=gm.__name__)

    await action(matic)
    await matic._refresh_task
    assert cbor.loads(device.writes[0][1])["mT"] == mt
    assert any(r.message.endswith(f"acknowledged by {matic.mac}") for r in caplog.records)
    assert matic._ack_waiters == {}


async def test_missing_ack_raises_and_still_reads_back(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, ack=None)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    with pytest.raises(CommandNotConfirmed):
        await matic.stop_spray()
    await matic._refresh_task
    assert probes(device) == [C4]  # the read-back shows what happened
    assert matic.device_status.battery_mv == 3170
    assert matic._ack_waiters == {}


async def test_rejected_command_raises(monkeypatch, caplog):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, ack=500)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    caplog.set_level("WARNING", logger=gm.__name__)

    with pytest.raises(CommandNotConfirmed):
        await matic.reset_refill()
    await matic._refresh_task
    assert any("Refill reset rejected" in r.message for r in caplog.records)


async def test_failed_write_raises(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    on_write = device.on_write

    def fail_spray(uuid, payload):
        if payload == gm.SPRAY_NOW_MSG:
            raise Exception("write failed")
        on_write(uuid, payload)

    device.on_write = fail_spray
    with pytest.raises(CommandNotConfirmed):
        await matic.spray_now()
    await matic._refresh_task


async def test_quick_presses_are_each_confirmed(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    for _ in range(3):
        await matic.spray_now()
    await matic._refresh_task
    sprays = [u for u, p in device.writes if p == gm.SPRAY_NOW_MSG]
    assert sprays == [C4, C4, C4]
    assert matic.device_status.battery_mv == 3170


async def test_overlapping_commands_are_each_confirmed(monkeypatch):
    """Spray and Auto Spray share an ack type; overlapping presses mustn't steal acks."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    await asyncio.gather(
        matic.spray_now(), matic.set_spray_interval(20), matic.spray_now(), matic.stop_spray()
    )
    await matic._refresh_task
    assert len([w for w in device.writes if cbor.loads(w[1])["mT"] != 107]) == 4
    assert matic._ack_waiters == {}


async def test_unreachable_device_fails_queued_commands_at_once(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    calls = []

    async def fail(*args, max_attempts=None, **kwargs):
        calls.append(max_attempts)
        await asyncio.sleep(0)
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", fail)
    results = await asyncio.gather(
        matic.stop_spray(), matic.spray_now(), matic.reset_refill(),
        return_exceptions=True,
    )
    assert all(isinstance(r, DeviceConnectionError) for r in results)
    assert calls == [1]  # one single-attempt connect for all three
    assert matic.reachable is False

    # A later press tries again.
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert len(calls) == 2


async def test_press_gives_up_connecting_after_the_time_limit(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=gm.__name__)
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.1)
    never = asyncio.Event()

    async def hang(*args, **kwargs):
        await never.wait()  # the proxy never gets a connection

    monkeypatch.setattr(gm, "establish_connection", hang)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert loop.time() - start < 1
    assert matic.reachable is False
    assert any("(command): timed out after 0.1 s" in r.message for r in caplog.records)


async def test_poll_connect_is_limited_too(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.1)
    never = asyncio.Event()
    calls = []

    async def hang(*args, max_attempts=None, **kwargs):
        calls.append(max_attempts)
        await never.wait()

    monkeypatch.setattr(gm, "establish_connection", hang)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert loop.time() - start < 1
    assert calls == [1]


async def test_press_during_a_failing_poll_fails_with_it(monkeypatch):
    """A press waits for a poll that is connecting, not for a second attempt."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    release = asyncio.Event()
    calls = []

    async def slow_fail(*args, **kwargs):
        calls.append(1)
        await release.wait()
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", slow_fail)
    poll = asyncio.ensure_future(matic.connect_if_needed())
    await asyncio.sleep(0)
    press = asyncio.ensure_future(matic.spray_now())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(DeviceConnectionError):
        await poll
    with pytest.raises(DeviceConnectionError):
        await press
    assert calls == [1]  # the press did not start its own attempt


async def test_press_during_a_successful_poll_uses_its_link(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    poll = asyncio.ensure_future(matic.connect(status_timeout=5))
    await asyncio.sleep(0)
    await matic.spray_now()  # confirmed on the poll's link
    assert await poll
    assert [u for u, p in device.writes if p == gm.SPRAY_NOW_MSG] == [C4]
    if matic._refresh_task is not None:
        matic._refresh_task.cancel()


@pytest.mark.parametrize("action", [
    lambda m: m.spray_now(), lambda m: m.set_spray_interval(10),
])
async def test_spray_below_the_cut_off_is_reported(monkeypatch, caplog, action):
    """The device acks sprays it can't do; the last status tells."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    matic.device_status.battery_mv = 2390
    caplog.set_level("WARNING", logger=gm.__name__)

    with pytest.raises(BatteryTooLow):
        await action(matic)
    assert any("2390 mV" in r.message for r in caplog.records)
    matic._refresh_task.cancel()


async def test_non_spray_commands_ignore_the_battery(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    matic.device_status.battery_mv = 2390

    await matic.stop_spray()
    await matic.reset_refill()
    matic._refresh_task.cancel()


async def test_poll_backoff_grows_and_resets(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    waits = []
    for _ in range(5):
        matic._retry_after = None
        with pytest.raises(DeviceConnectionError):
            await matic.connect_if_needed()
        waits.append(round((matic._retry_after - gm.datetime.now()).total_seconds() / 10) * 10)
    assert waits == [30, 60, 120, 300, 300]

    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    matic.device_status = None
    device.status_frame = b""
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert (matic._retry_after - gm.datetime.now()).total_seconds() <= 30


async def test_background_check_recovers_after_a_failed_press(monkeypatch):
    """A recent status mustn't stop the device being checked again."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    await matic.disconnect()
    assert not await matic.connect_if_needed()  # fresh status: nothing to do

    real = gm.establish_connection

    async def off(*args, **kwargs):
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", off)
    with pytest.raises(DeviceConnectionError):
        await matic.stop_spray()  # device switched off
    assert matic.reachable is False

    monkeypatch.setattr(gm, "establish_connection", real)  # back on
    assert await matic.connect_if_needed()
    assert matic.reachable is True
    assert not await matic.connect_if_needed()  # and quiet again


async def test_press_during_a_slow_status_reply_is_not_held_up(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, reply_delay=0.5)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "SET_TIME_PACING", 0)
    loop = asyncio.get_running_loop()

    poll = asyncio.ensure_future(matic.connect(status_timeout=5))
    while not any(cbor.loads(p)["mT"] == 107 for _, p in device.writes):
        await asyncio.sleep(0.01)  # the poll is now waiting for its reply
    start = loop.time()
    await matic.spray_now()
    assert loop.time() - start < 0.3  # well before the 0.5 s reply
    assert await poll
    matic._refresh_task.cancel()


async def test_press_cancels_a_pending_disconnect(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    pending = matic._disconnect_task
    assert pending is not None and not pending.done()

    await matic.spray_now()
    await asyncio.sleep(0)
    assert pending.cancelled()
    await matic._refresh_task


# --- notification handling --------------------------------------------------

async def test_stale_fragment_does_not_corrupt_next_frame(monkeypatch):
    matic = gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37")
    clock = [1000.0]
    monkeypatch.setattr(gm.time, "monotonic", lambda: clock[0])

    matic._on_notification(C1, FRAME_308_IN[:30])  # partial, never finished
    clock[0] += gm._FRAGMENT_TIMEOUT + 1
    matic._on_notification(C1, FRAME_308_IN)
    assert matic.device_status is not None and matic.device_status.is_valid
    assert matic._rx_buffers == {}


async def test_acks_do_not_count_as_status(monkeypatch):
    matic = gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37")
    matic._on_notification(C1, SPRAY_ACK)
    assert matic.device_status is None
    assert not matic._device_status_event.is_set()


# --- reachability -----------------------------------------------------------

async def test_reachable_survives_the_normal_disconnect(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert matic.reachable is None and matic.last_update is None

    assert await matic.connect(status_timeout=5)
    assert matic.reachable is True
    assert matic.last_update is not None and matic.last_update.tzinfo is not None

    await matic.disconnect()  # the routine drop after an exchange
    assert matic.reachable is True


async def test_reachable_turns_off_on_real_trouble_and_back_on(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    events = []
    matic.eventbus.add_listener(gm.DEVICE_REACHABILITY, AsyncMockRecorder(events))

    assert await matic.connect_if_needed()
    first = matic.last_update

    device.status_frame = b""  # stops answering
    matic.device_status = None
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert matic.reachable is False
    assert matic.last_update == first  # unchanged by the failure

    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    assert matic.reachable is True
    await asyncio.sleep(0)
    assert events == [True, False, True]  # one event per change only


async def test_reachable_follows_advertisements(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)

    matic.set_advertising(False)  # out of range / flat battery
    assert matic.reachable is False
    matic.set_advertising(True)
    assert matic.reachable is True


async def test_failed_press_marks_unreachable(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    async def fail(*args, **kwargs):
        raise Exception("out of range")

    monkeypatch.setattr(gm, "establish_connection", fail)
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert matic.reachable is False


class AsyncMockRecorder:
    def __init__(self, sink):
        self.sink = sink

    async def __call__(self, matic):
        self.sink.append(matic.reachable)


async def test_detect_reports_each_stage(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    stages = []

    assert await matic.detect_channels(progress=lambda *a: stages.append(a)) == (C4, C1)
    assert stages == [
        ("connecting", {}, 0.0),
        ("checking_defaults", {}, 0.0),
        ("probing", {"index": "1", "total": "3", "channel": "6e400002"}, 0.0),
        ("probing", {"index": "2", "total": "3", "channel": "6e400003"}, 1 / 6),
        ("probing", {"index": "3", "total": "3", "channel": "6e400004"}, 2 / 6),
    ]


C5 = "6e400005-b5a3-f393-e0a9-e50e24dcca9e"
C6 = "6e400006-b5a3-f393-e0a9-e50e24dcca9e"


async def test_detect_probes_unknown_writable_characteristics_last(monkeypatch):
    """A firmware that takes commands somewhere new is still found."""
    device = FakeDevice(
        command_char=C5, status_frame=FRAME_308_IN,
        extra_chars=(
            FakeChar(C6, 0x23, ["read", "notify"]),               # not writable: skipped
            FakeChar(C5, 0x1F, ["read", "write-without-response"]),
        ),
    )
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C5, C1)
    assert probes(device) == [C2, C2, C3, C4, C5]


async def test_detect_skips_known_channels_the_device_lacks(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    device.services = FakeServices([FakeService(MAIN_SVC, [
        FakeChar(C1, 0x10, ["read", "notify"]),
        FakeChar(C4, 0x1B, ["read", "write-without-response", "notify"]),
    ])])
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C4]  # the default is tried regardless


class CountingService(FakeService):
    """Counts how often the characteristic list is walked."""

    def __init__(self, uuid, characteristics):
        self._chars = characteristics
        self.uuid = uuid
        self.walks = 0

    @property
    def characteristics(self):
        self.walks += 1
        return self._chars

    def get_characteristic(self, uuid):
        return next((c for c in self._chars if c.uuid == uuid), None)


async def test_normal_connection_does_not_walk_the_characteristics(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    service = CountingService(MAIN_SVC, device.services.get_service(MAIN_SVC).characteristics)
    device.services = FakeServices([service])
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    assert await matic.connect(status_timeout=5)
    await matic.spray_now()
    await matic._refresh_task
    assert service.walks == 0
    assert device.subscribe_attempts == [C1]
    assert matic.device_status.is_valid


async def test_identify_lights_then_clears_while_idle(monkeypatch):
    from unittest.mock import AsyncMock
    from godrej.devicestatus import DeviceStatus

    monkeypatch.setattr(gm, "IDENTIFY_SECONDS", 0)
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    idle = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=0))

    async def fresh_status(**_):
        matic.device_status = idle
    matic.connect = AsyncMock(side_effect=fresh_status)
    await matic.identify()
    await matic._refresh_task
    sent = [cbor.loads(payload)["mT"] for _, payload in device.writes]
    assert sent[:2] == [113, 106]  # LED test, then auto off to clear the lights


async def test_identify_refuses_unless_idle(monkeypatch):
    from unittest.mock import AsyncMock
    from godrej.devicestatus import DeviceStatus
    from godrej.exception import NotIdle

    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    busy = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=121))

    async def fresh_status(**_):
        matic.device_status = busy
    matic.connect = AsyncMock(side_effect=fresh_status)
    with pytest.raises(NotIdle) as err:
        await matic.identify()
    assert err.value.schedule  # cS 121: started by a schedule
    assert device.writes == []
