# bmsocketcan protocol analysis

The native backend follows the GPL driver sources at
[busmust/bmsocketcan, commit 70919fb](https://github.com/busmust/bmsocketcan/tree/70919fbc2a1be146498ca0df941d1d8a0a93c74b).
Paths below are relative to that reference repository; an optional local clone
can be kept under `bmsocketcan/`.
The SDK ABI is unnecessary: the reference's USB controls and frame envelopes
contain everything needed for live CAN communication.

## USB ownership and setup

`src/bmcan_usb.c` identifies vendor `0x0810` and twelve product IDs. Its
`bmcan_channels_from_pid()` and `bmcan_get_generation()` mappings are encoded
in `busmust.protocol.PRODUCTS`. CAN uses interface 0, including on composite
devices. The reference's default bulk endpoints are `0x81` and `0x01`, but
both implementations inspect descriptors. All CAN ports share these endpoints.

The Python transport claims interface 0 once per physical device. A background
reader dispatches decoded frames into bounded per-channel queues; serialized
bulk writes prevent interleaving frames. Vendor control transfers have a
separate lock. Other CAN channels survive a sibling channel closing or failing
initialization. The last channel releases USB resources and restores any
kernel driver that this session detached.

Vendor requests use recipient DEVICE, type VENDOR: `bmRequestType=0x40` for
writes, `0xC0` for reads. `wIndex` is the zero-based CAN port.

| Request | Direction | wValue | Payload |
| --- | --- | --- | --- |
| `C0` mode | OUT | Mode value | None |
| `C2` bitrate | OUT | 0 | 12 bytes |
| `C3` termination | OUT | 0 or 120 ohms | None |
| `C8` RX filter | OUT | Filter slot 0 or 1 | 32 bytes |
| `D1` status | IN | 0 | 8 bytes |

Bitrate payload: little-endian `uint16` nominal kbit/s, `uint16` data kbit/s,
then nominal/data sample-point percentages, followed by six zero bytes for
clock/reserved/explicit timing fields. The reference truncates fractional
percentages; this backend does the same and documents it.

Open sequence: configuration mode (`4`), bitrate, 10 ms settling delay, a basic
accept-all filter in slot 0, invalid filter in slot 1, optional termination,
then operating mode, followed by another 10 ms settle: measured on an X1
(firmware 2.2.4.10), the firmware silently discards bulk frames submitted
while the controller is still switching into the new mode. Basic mode values:
normal FD `0`, internal loopback `2`,
listen-only `3`, Classical CAN `6`. Non-ISO (`8`) and one-shot (`16`) are ORed
into the mode. Close returns the selected channel to configuration mode.
No persistent configuration is saved. Existing RX filters on that channel
are replaced; software filtering in `BusABC` supplies arbitrary filter lists.

Status bytes: bus-off, reserved, TX passive, RX passive, TX warning, RX warning,
TX error counter, RX error counter. `state` reads this status on demand.
Gen2/2.5 firmware occasionally latches a stale bus-off state across a
reconfiguration (observed on about 4% of channel opens with X1 firmware
2.2.4.10); a deep latch can show TEC/REC pegged at 255 and survive several
recovery passes. `recover_bus_off()` mirrors BMAPI `BM_RecoverBusOff()`:
control `0xF5` performs the recovery in firmware on Gen2/2.5 >= 2.6.0.0 and
Gen3 >= 3.1.0.0; older adapters use the reference `bmcan_loopback_recovery()`
(a 1 Mbit/s / 8 Mbit/s internal-loopback burst of 256 dummy RTR frames, then
restore), retried up to eight times like the reference driver's restart poll.
Channel open runs it automatically when the controller comes up bus-off;
`send()` probes status at most twice a second so a bus-off controller fails
sends with `CanOperationError` instead of silently discarding frames.

## CAN envelope

`bmcan_proto_build_tx()` emits a variable-length, four-byte-aligned packet:

| Offset | Width | Field |
| --- | --- | --- |
| 0 | 2 | Little-endian type/routing header |
| 2 | 2 | Payload length, including an optional tail |
| 4 | 4 | Device timestamp in microseconds; zero for host TX |
| 8 | 4 | Packed CAN identifier |
| 12 | 4 | CAN control word |
| 16 | variable | Data bytes and alignment padding |

Header: bits 0–3 type, bit 4 tail-present, bits 5–7 group, bits 8–11 destination,
bits 12–15 source. Host TX uses type `2`, destination=port, source=`15`. RX
selects source when destination is `15`, otherwise destination, matching the
reference. This implementation supports group 0, which covers all listed
adapters. Types `2` and `A` are CAN data and CAN TX echo respectively; type `8`
is an ACK without data. Unknown types are consumed without CAN decoding.

The identifier has SID in bits 0–10 and EID in bits 11–28. Extended IDs must
be rearranged: `packed = ((id & 0x3ffff) << 11) | (id >> 18)`.
Using a regular 29-bit integer directly would transmit the wrong identifier.

Control: bits 0–3 DLC; bit 4 IDE; bit 5 RTR; bit 6 BRS; bit 7 FDF; bit 8 ESI.
TX bit 17 requests echo. Sequence and other controller-specific fields are
unused. DLC maps to `0..8, 12, 16, 20, 24, 32, 48, 64` bytes. FD sends are padded
to the legal DLC length before USB alignment. RX without FDF but with DLC > 8
is interpreted as FD, following the reference's offline-replay compatibility.

## Timestamp handling

Gen2/2.5 use the envelope's 32-bit microsecond timer (wraps about every 71.6
minutes). The session's clock is shared across channels and unwraps using the
nearest epoch to the previous timestamp plus host monotonic elapsed time.
The first received frame supplies the host epoch offset. Small out-of-order
timestamps do not advance the anchor. This avoids false wraps from channel
interleaving; a device clock reset still requires reopening.

Gen3 may append a 16-byte tail at `align4(8 + data_length)` within the payload.
The last eight tail bytes hold a little-endian UTC microsecond count. Zero
timestamps fall back to the local timer. The preceding packet ID/checksum
fields are currently not verified, consistent with the reference receive path.
Echo timestamps do not update local wrap state.

## Deliberate differences and scope

- The kernel receive path discards all ACK/echo packets. Python exposes CAN
  echoes only when requested and marks them `is_rx=False`.
- The kernel clears received RTR DLC to zero. Python preserves the requested
  Classical CAN length while leaving remote-frame data empty.
- The kernel parser processes an individual URB and stops at truncation.
  Python buffers partial envelopes across reads and decodes concatenated ones.
- The kernel clamps payloads shorter than their DLC. Python reports malformed
  CAN packets as errors to avoid passing corrupt frames to diagnostic stacks.
- Exact type matching avoids decoding SYSTEM type `F` as CAN merely because
  its bitmask contains bit `2`.
- The reference enables 120-ohm termination by default. Python preserves the
  setting unless the caller explicitly supplies 0 or 120.
- The kernel batches concurrent URBs and has firmware-specific recovery,
  offline routing/storage, and TX-task support. This implementation focuses
  on live CAN with synchronous one-frame writes and one shared reader; it
  does not claim equivalent peak throughput or implement those extensions.

The plugin follows the [BusABC extension contract](https://python-can.readthedocs.io/en/stable/internal-api.html)
and uses [PyUSB's device/control/bulk APIs](https://github.com/pyusb/pyusb/blob/master/docs/tutorial.rst).
