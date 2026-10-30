import pytest

from godrej import cbor
from godrej.devicestatus import DeviceStatus

# fw 3.0.8-IN, captured through an ESPHome proxy (99 bytes).
FRAME_308_IN = bytes.fromhex(
    "bf626d54189d626d4e189d6361636b18c86262561a00000c6262665668332e302e"
    "382d494e6263531872627343a362736e84190004190000190000190001627363"
    "8319000019000019000062626e8319000019000019000062735666313031313131ff"
)

# Synthetic reply with the field values parthh37/aercontrol documents for
# fw 3.0.6-T1, but a longer firmware string and an extra unknown key, so it
# is a different length and every fixed offset shifts. (aercontrol reports
# 105 bytes for the real frame; its exact bytes are not published.)
FRAME_SHIFTED = (
    b"\xbf"
    b"\x62mT\x18\x9d" b"\x62mN\x18\xa0" b"\x63ack\x18\xc8"
    b"\x62bV\x1a" + (2506).to_bytes(4, "big")
    + b"\x62fV\x6a" + b"3.0.10-T1x"
    + b"\x62xX\x01"
    + b"\x62cS\x18\x85"
    + b"\x62sC\xa3"
    + b"\x62sn\x84" + b"".join(b"\x19" + n.to_bytes(2, "big") for n in (65, 0, 0, 65))
    + b"\x62sc\x83" + b"".join(b"\x19" + n.to_bytes(2, "big") for n in (2330, 263, 0))
    + b"\x62bn\x83" + b"".join(b"\x19" + n.to_bytes(2, "big") for n in (17, 88, 4))
    + b"\x62sV\x66101110"
    + b"\xff"
)


def test_308_in_frame():
    assert len(FRAME_308_IN) == 99
    status = DeviceStatus(FRAME_308_IN)
    assert status.is_valid
    assert status.battery_mv == 3170
    assert status.battery_volts == 3.17
    assert status.firmware_version == "3.0.8-IN"
    assert status.current_status == 114
    assert status.spray_interval == 40
    assert status.is_spraying
    assert status.structure_version == "101111"
    assert status.spray_counts["spray_now_0"] == 4
    assert status.spray_counts["spray_now_40"] == 1
    assert status.total_spray_count == 5
    assert status.refill_percent == 100


def test_308_in_frame_matches_legacy_decoder():
    """CBOR decoding agrees with the old fixed-offset decoder."""
    via_cbor = DeviceStatus(FRAME_308_IN)
    legacy = DeviceStatus.__new__(DeviceStatus)
    legacy.spray_counts = {}
    legacy._from_fixed_offsets(FRAME_308_IN)
    for attr in (
        "battery_mv", "firmware_version", "current_status",
        "structure_version", "spray_counts", "total_spray_count",
    ):
        assert getattr(via_cbor, attr) == getattr(legacy, attr), attr


def test_frame_of_other_length():
    assert len(FRAME_SHIFTED) == 105
    status = DeviceStatus(FRAME_SHIFTED)
    assert status.is_valid
    assert status.battery_mv == 2506
    assert status.firmware_version == "3.0.10-T1x"
    assert status.spray_interval == 40  # cS 133
    assert status.total_spray_count == 65 + 65 + 2330 + 263 + 17 + 88 + 4
    assert status.refill_percent == 0


def test_spray_ack_is_not_a_status():
    ack = bytes.fromhex("bf626d54189a626d4e189a6361636b18c8ff")
    assert cbor.loads(ack) == {"mT": 154, "mN": 154, "ack": 200}
    assert not DeviceStatus(ack).is_valid


def test_nack_status_is_rejected():
    frame = FRAME_308_IN.replace(b"\x63ack\x18\xc8", b"\x63ack\x18\xc9")
    assert not DeviceStatus(frame).is_valid


@pytest.mark.parametrize("frame", [b"", b"\x00\x00", bytes(99), b"\xff" * 10])
def test_garbage_is_rejected(frame):
    assert not DeviceStatus(frame).is_valid


def test_missing_counters_still_gives_battery():
    frame = bytes.fromhex("bf626d54189d6361636b18c86262561a00000c62ff")
    status = DeviceStatus(frame)
    assert status.is_valid
    assert status.battery_mv == 3170
    assert status.total_spray_count is None
    assert status.refill_percent is None


def test_truncated_frame_is_incomplete():
    for cut in range(1, len(FRAME_308_IN)):
        with pytest.raises(cbor.CBORIncompleteError):
            cbor.decode(FRAME_308_IN[:cut])


def test_cbor_misc():
    assert cbor.loads(bytes.fromhex("f5")) is True
    assert cbor.loads(bytes.fromhex("f4")) is False
    assert cbor.loads(bytes.fromhex("f6")) is None
    assert cbor.loads(bytes.fromhex("3863")) == -100
    assert cbor.loads(bytes.fromhex("1b000000006ad7b813")) == 0x6AD7B813
    assert cbor.loads(bytes.fromhex("9f0102ff")) == [1, 2]
    assert cbor.loads(bytes.fromhex("a1616101")) == {"a": 1}
    assert cbor.loads(bytes.fromhex("7f62616262636cff")) == "abcl"
    assert cbor.loads(bytes.fromhex("f93c00")) == 1.0
    with pytest.raises(cbor.CBORError):
        cbor.loads(bytes.fromhex("0101"))  # trailing data
    with pytest.raises(cbor.CBORError):
        cbor.loads(bytes.fromhex("ff"))  # stray break


@pytest.mark.parametrize("mv, percent", [
    (3300, 100), (3100, 100), (3050, 98), (3000, 95), (2800, 86), (2650, 68),
    (2500, 29), (2450, 14), (2400, 0), (2100, 0),
])
def test_battery_percent_follows_the_alkaline_curve(mv, percent):
    status = DeviceStatus(FRAME_308_IN)
    status.battery_mv = mv
    assert status.battery_percent == percent


SCHEDULED_FRAME = bytes.fromhex(  # fw 3.0.8-IN while a 10-min schedule ran
    "bf626d54189d626d4e189d6361636b18c86262561a00000c0c62665668332e302e382d49"
    "4e6263531879627343a362736e8419009a1900081900001900056273638319000219000019"
    "000062626e8319000019000019000062735666313031313131ff"
)


def test_status_summary_names_what_started_spraying():
    status = DeviceStatus(SCHEDULED_FRAME)
    assert status.spray_source == "schedule" and status.spray_interval == 10
    assert status.describe() == (
        "auto every 10 min, started by schedule (cS 121); battery 3084 mV (99%); "
        "Sprays: Total 169: now 154; auto 8/0/5; scheduled 2/0/0; button 0/0/0 "
        "(each 10/20/40 min); firmware 3.0.8-IN"
    )
    for code, source in ((113, "app"), (133, "button"), (111, None), (0, None)):
        status.current_status = code
        assert status.spray_source == source
