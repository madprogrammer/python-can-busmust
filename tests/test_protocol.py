import struct

import can
import pytest
from conftest import rx_packet

from busmust.protocol import Decoder, ProtocolError, TimestampClock, bitrate_payload, encode_message


def test_standard_golden_vector():
    msg = can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\xde\xad")
    assert encode_message(msg, 1).hex() == "02f10c00000000002301000002000000dead0000"


def test_extended_fd_golden_vector():
    msg = can.Message(
        arbitration_id=0x18FF50E5,
        is_extended_id=True,
        is_fd=True,
        bitrate_switch=True,
        error_state_indicator=True,
        data=bytes(range(12)),
    )
    # SID=0x63F, EID=0x350E5, packed ID=0x1A872E3F; DLC=9.
    expected = bytes.fromhex("02f01400000000003f2e871ad9010000") + bytes(range(12))
    assert encode_message(msg, 0) == expected
    frame = Decoder().feed(rx_packet(arb_id=0x1A872E3F, data=bytes(range(12)), ctrl=0x1D9))[0]
    assert frame.message.arbitration_id == 0x18FF50E5
    assert frame.message.is_fd and frame.message.error_state_indicator
    assert frame.message.bitrate_switch


@pytest.mark.parametrize(
    "size,dlc,wire_size",
    [
        (0, 0, 0),
        (8, 8, 8),
        (9, 9, 12),
        (12, 9, 12),
        (13, 10, 16),
        (17, 11, 20),
        (21, 12, 24),
        (25, 13, 32),
        (33, 14, 48),
        (49, 15, 64),
        (64, 15, 64),
    ],
)
def test_fd_padding(size, dlc, wire_size):
    msg = can.Message(arbitration_id=1, is_fd=True, data=bytes(range(size)))
    packet = encode_message(msg, 0)
    assert packet[12] == 0x90 | dlc
    assert len(packet) == 16 + (wire_size + 3) // 4 * 4
    assert packet[16 : 16 + wire_size] == bytes(range(size)) + bytes(wire_size - size)


def test_remote_dlc_preserved():
    msg = can.Message(arbitration_id=0x321, is_extended_id=False, is_remote_frame=True, dlc=8)
    assert encode_message(msg, 0)[12] == 0x28
    frame = Decoder().feed(rx_packet(arb_id=0x321, ctrl=0x28, data=b""))[0]
    assert frame.message.is_remote_frame
    assert frame.message.dlc == 8 and not frame.message.data


def test_every_fragment_boundary_and_multiple_packets():
    first, second = rx_packet(channel=1), rx_packet(channel=0, data=b"abc")
    for split in range(len(first) + 1):
        decoder = Decoder()
        frames = decoder.feed(first[:split]) + decoder.feed(first[split:] + second)
        assert [f.message.channel for f in frames] == [1, 0]
        assert frames[1].message.data == b"abc"
        assert not decoder.buffer


def test_unknown_types_ack_and_timestamp_tail():
    decoder = Decoder()
    # SYSTEM type has CAN's bit set but must never be treated as a CAN packet.
    skipped = struct.pack("<HHI", 15, 4, 0) + bytes(4)
    ack = struct.pack("<HHI", 8, 0, 0)
    frames = decoder.feed(skipped + ack + rx_packet(kind=10, tail=1_700_000_000_123456))
    assert len(frames) == 1 and not frames[0].message.is_rx
    assert frames[0].utc_us == 1_700_000_000_123456


def test_group_and_routing():
    packet = bytearray(rx_packet(channel=2))
    # Directed frame uses destination instead of source.
    struct.pack_into("<H", packet, 0, 0xF102)
    assert Decoder().feed(packet)[0].message.channel == 1
    packet[0] |= 0x20
    assert Decoder().feed(packet) == []


@pytest.mark.parametrize(
    "packet",
    [
        struct.pack("<HHI", 2, 1025, 0),
        struct.pack("<HHI", 2, 4, 0) + bytes(4),
        rx_packet(data=b"a", ctrl=8),
        rx_packet(data=b"", ctrl=0xA0),
        struct.pack("<HHI", 0x12, 8, 0) + bytes(8),
    ],
)
def test_malformed_envelopes(packet):
    with pytest.raises(ProtocolError):
        Decoder().feed(packet)


def test_missing_fdf_compatibility():
    frame = Decoder().feed(rx_packet(data=bytes(12), ctrl=9))[0]
    assert frame.message.is_fd and frame.message.dlc == 12


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(arbitration_id=0x800, is_extended_id=False),
        dict(arbitration_id=-1),
        dict(arbitration_id=1 << 29),
        dict(is_error_frame=True),
        dict(data=bytes(9)),
        dict(data=b"abc", dlc=2),
        dict(is_remote_frame=True, is_fd=True),
        dict(bitrate_switch=True),
        dict(error_state_indicator=True),
    ],
)
def test_invalid_transmit(kwargs):
    with pytest.raises(ValueError):
        encode_message(can.Message(**kwargs), 0)


def test_bitrate_wire_format():
    assert bitrate_payload(500_000, 2_000_000, 87.5, 80).hex() == "f401d0075750000000000000"
    for rate in (0, 123456, 65_536_000):
        with pytest.raises(ValueError):
            bitrate_payload(rate, 2_000_000, 87.5, 80)
    for point in (0, 100, float("nan")):
        with pytest.raises(ValueError):
            bitrate_payload(500_000, 2_000_000, point, 80)


def test_timestamp_wrap_late_packets_and_echo(monkeypatch):
    monkeypatch.setattr("busmust.protocol.time.time", lambda: 1_700_000_000.0)
    monkeypatch.setattr("busmust.protocol.time.monotonic", lambda: 10.0)
    clock = TimestampClock()
    assert clock.convert(0xFFFFFFF0, None) == 1_700_000_000.0
    wrapped = clock.convert(0x10, None)
    assert wrapped == pytest.approx(1_700_000_000 + 32e-6, abs=1e-6)
    last = clock.last_ticks
    clock.convert(0xFFFFFFF8, None)  # late frame from preceding epoch
    assert clock.last_ticks == last
    clock.convert(1234567, None, update=False)
    assert clock.last_ticks == last
    assert clock.convert(0, 1_700_123_456_000_000) == 1_700_123_456


def test_timestamp_after_multiple_idle_wraps(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("busmust.protocol.time.monotonic", lambda: now[0])
    clock = TimestampClock()
    first = clock.convert(100, None)
    now[0] = (2 * (1 << 32) + 50) / 1_000_000
    second = clock.convert(150, None)
    assert second - first == pytest.approx(now[0], abs=1e-6)
