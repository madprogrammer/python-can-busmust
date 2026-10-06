# Changelog

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
