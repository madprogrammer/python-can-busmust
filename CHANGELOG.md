# Changelog

## 0.1.0 — 2026-10-07

First release validated on physical adapters: two X1 units (firmware 2.2.4.10)
wired together, covering bidirectional classical CAN and CAN FD at 125 k —
1 Mbit/s and data phases to 5 Mbit/s, every DLC length, extended IDs, RTR,
software filters, termination settings, TX echoes, and zero-loss 1000-frame
bursts.

- Fixed the first transmitted frame after opening a channel being silently
  dropped by the adapter: the firmware (validated on an X1, 2.2.4.10)
  discards bulk frames submitted while switching operating modes, so channel
  open now waits 10 ms after the mode command before returning.
- Channels that come up latched in a stale bus-off state (about 4% of opens
  on Gen2/2.5 firmware) are now recovered automatically at open using the
  reference driver's loopback dummy-frame procedure; opens that stay bus-off
  fail with `CanInitializationError` instead of silently dropping frames.
- Added `BusMustBus.recover_bus_off()` mirroring BMAPI `BM_RecoverBusOff()`:
  firmware-side recovery (control `0xF5`) on Gen2/2.5 firmware >= 2.6.0.0 and
  Gen3 >= 3.1.0.0, with the loopback dummy-frame fallback (up to eight
  passes) for older adapters. `send()` now probes controller status at most
  twice a second and raises `CanOperationError` while bus-off, or recovers
  and retries once when `auto_recover_bus_off=True` is set. The firmware
  version is read at open and shown in `channel_info`.
- Hardened interpreter-exit cleanup: open channels are released best-effort,
  closed sessions never touch libusb again, and a wedged reader thread no
  longer skips or corrupts cleanup. Exiting mid-traffic without shutdown can
  still abort during PyUSB teardown; prefer context managers.

## 0.1.0a1 — 2026-10-06

Initial alpha release, pending validation on physical BUSMUST adapters.

- Pure Python/PyUSB backend registered as `python-can` interface `busmust`.
- Classical CAN and CAN FD framing, extended IDs, RTR, BRS, ESI, and TX echoes.
- Adapter discovery and independent channels sharing one USB reader.
- Bitrate, sample-point, termination, listen-only, loopback, and one-shot settings.
- Software acceptance filters, receive timestamps, and controller status queries.
- Tests covering the USB protocol, failure handling, python-can listeners and
  periodic sending, cantools DBC encoding, and a multi-frame ISO-TP exchange.

No physical-adapter or peak-throughput validation is claimed. Hardware TX tasks,
offline storage/routing, and firmware-specific bus-off recovery are not implemented.
