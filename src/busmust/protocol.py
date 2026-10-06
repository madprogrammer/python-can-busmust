# SPDX-License-Identifier: GPL-2.0-or-later
"""BUSMUST wire codec, based on bmsocketcan's GPL protocol implementation.

No SDK, ctypes, vendor headers, or native extension is needed.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from can import Message

VID = 0x0810
# PID: (model, channel count, generation)
PRODUCTS = {
    0xF012: ("X1", 1, 2),
    0xF112: ("X1 Pro", 1, 2),
    0xF122: ("X2", 2, 2),
    0xF142: ("X4", 4, 2),
    0xF182: ("X8 Pi", 8, 2),
    0xE122: ("X2R", 2, 2.5),
    0xE142: ("X4R", 4, 2.5),
    0xF013: ("X1", 1, 3),
    0xF023: ("X2", 2, 3),
    0xF043: ("X4", 4, 3),
    0x0043: ("XL2", 2, 3),
    0x0083: ("XL4", 4, 3),
}
LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
HEADER = struct.Struct("<HHI")
IDE, RTR, BRS, FDF, ESI, ECHO = 0x10, 0x20, 0x40, 0x80, 0x100, 0x20000


class ProtocolError(ValueError):
    """Malformed USB data; the stream cannot be consumed safely."""


def align4(value: int) -> int:
    return (value + 3) & ~3


def encode_message(msg: Message, channel: int, echo: bool = False) -> bytes:
    """Encode a single frame, padding CAN FD lengths to the next legal DLC."""
    if not 0 <= channel < 8:
        raise ValueError("channel must be between 0 and 7")
    if msg.is_error_frame:
        raise ValueError("BUSMUST cannot transmit error frames")
    if not 0 <= msg.arbitration_id <= (0x1FFFFFFF if msg.is_extended_id else 0x7FF):
        raise ValueError("arbitration_id is outside the selected CAN ID range")
    if not 0 <= msg.dlc <= (64 if msg.is_fd else 8):
        raise ValueError("invalid CAN payload length")
    if msg.is_remote_frame and msg.is_fd:
        raise ValueError("CAN FD does not support remote frames")
    if not msg.is_fd and (msg.bitrate_switch or msg.error_state_indicator):
        raise ValueError("BRS and ESI require CAN FD")
    if msg.is_remote_frame:
        if msg.data:
            raise ValueError("remote frames cannot contain data")
    elif len(msg.data) != msg.dlc:
        raise ValueError("message.dlc must equal len(message.data)")
    dlc = next(i for i, size in enumerate(LENGTHS) if size >= msg.dlc)
    wire_len = LENGTHS[dlc]
    ctrl = dlc | (IDE if msg.is_extended_id else 0) | (RTR if msg.is_remote_frame else 0)
    ctrl |= (FDF if msg.is_fd else 0) | (BRS if msg.bitrate_switch else 0)
    ctrl |= (ESI if msg.error_state_indicator else 0) | (ECHO if echo else 0)
    arb_id = msg.arbitration_id
    packed_id = ((arb_id & 0x3FFFF) << 11) | (arb_id >> 18) if msg.is_extended_id else arb_id
    data = b"" if msg.is_remote_frame else bytes(msg.data)
    payload = struct.pack("<II", packed_id, ctrl) + data.ljust(align4(wire_len), b"\0")
    return HEADER.pack(0xF002 | (channel << 8), len(payload), 0) + payload


@dataclass(frozen=True)
class Frame:
    message: Message
    ticks: int
    utc_us: int | None


class Decoder:
    """Incremental decoder: USB transfers may split or concatenate envelopes."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= HEADER.size:
            header, length, ticks = HEADER.unpack_from(self.buffer)
            if length > 1024:
                self.buffer.clear()
                raise ProtocolError("USB payload exceeds the reference protocol's 1024-byte limit")
            total = align4(HEADER.size + length)
            if len(self.buffer) < total:
                break
            payload = bytes(self.buffer[HEADER.size : HEADER.size + length])
            del self.buffer[:total]
            kind = header & 0xF
            # Exact type matching matters: SYSTEM (0xF) also contains CAN's bit.
            if kind not in (0x2, 0xA) or header & 0xE0:
                continue
            has_tail = bool(header & 0x10)
            if length < 8 + (16 if has_tail else 0):
                raise ProtocolError("CAN envelope is missing its control words or tail")
            raw_id, ctrl = struct.unpack_from("<II", payload)
            dlc = ctrl & 0xF
            remote = bool(ctrl & RTR)
            fd = bool(ctrl & FDF) or (dlc > 8 and not remote)
            if remote and (fd or dlc > 8):
                raise ProtocolError("invalid remote frame flags/DLC")
            size = LENGTHS[dlc] if fd else dlc
            data_size = 0 if remote else size
            body_len = length - (16 if has_tail else 0)
            if body_len < 8 + data_size:
                raise ProtocolError("CAN DLC exceeds the available payload")
            # Tail follows the aligned data. RTRs carry no data on receive.
            tail_offset = align4(8 + data_size)
            utc_us = None
            if has_tail:
                if length < tail_offset + 16:
                    raise ProtocolError("truncated CAN timestamp tail")
                utc_us = struct.unpack_from("<Q", payload, tail_offset + 8)[0]
            extended = bool(ctrl & IDE)
            arb_id = (
                ((raw_id & 0x7FF) << 18) | ((raw_id >> 11) & 0x3FFFF)
                if extended
                else raw_id & 0x7FF
            )
            dest, source = (header >> 8) & 0xF, (header >> 12) & 0xF
            channel = source if dest == 0xF else dest
            message = Message(
                arbitration_id=arb_id,
                is_extended_id=extended,
                is_remote_frame=remote,
                is_fd=fd,
                bitrate_switch=bool(ctrl & BRS),
                error_state_indicator=bool(ctrl & ESI),
                dlc=size,
                data=payload[8 : 8 + data_size],
                channel=channel,
                is_rx=kind == 0x2,
            )
            result.append(Frame(message, ticks, utc_us))
        return result


class TimestampClock:
    """Anchor a wrapping device clock to host epoch time, shared by all ports."""

    def __init__(self):
        self.last_ticks = None
        self.last_host = None
        self.offset = None

    def convert(self, ticks: int, utc_us: int | None, *, update: bool = True) -> float:
        if utc_us:
            return utc_us / 1_000_000
        now = time.monotonic()
        if self.last_ticks is None:
            if not update:
                return time.time()
            extended = ticks
            self.offset = time.time() - ticks / 1_000_000
        else:
            # Host elapsed time also disambiguates wraps after long idle periods.
            expected = self.last_ticks + (now - self.last_host) * 1_000_000
            extended = ticks + round((expected - ticks) / (1 << 32)) * (1 << 32)
        if update and (self.last_ticks is None or extended >= self.last_ticks):
            self.last_ticks, self.last_host = extended, now
        return self.offset + extended / 1_000_000


def bitrate_payload(
    bitrate: int, data_bitrate: int, sample_point: float, data_sample_point: float
) -> bytes:
    for rate in (bitrate, data_bitrate):
        if not isinstance(rate, int) or rate <= 0 or rate % 1000 or rate > 65_535_000:
            raise ValueError("bitrates must be positive whole kbit/s fitting uint16")
    for point in (sample_point, data_sample_point):
        if not 1 <= point < 100:
            raise ValueError("sample points are percentages in [1, 100)")
    return struct.pack(
        "<HH8B",
        bitrate // 1000,
        data_bitrate // 1000,
        int(sample_point),
        int(data_sample_point),
        0,
        0,
        0,
        0,
        0,
        0,
    )
