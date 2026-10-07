# SPDX-License-Identifier: GPL-2.0-or-later
"""python-can BusABC interface for BUSMUST USB adapters."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import usb.core
from can import BusABC, BusState, CanInitializationError, CanOperationError, CanProtocol

from . import transport
from .protocol import PRODUCTS, bitrate_payload, encode_message

_STATUS_PROBE_INTERVAL = 0.5


@dataclass(frozen=True)
class CanStatus:
    bus_off: bool
    tx_passive: bool
    rx_passive: bool
    tx_warning: bool
    rx_warning: bool
    tx_error_counter: int
    rx_error_counter: int


class BusMustBus(BusABC):
    """One zero-based CAN channel; sibling channels share a USB connection.

    See README.md for configuration options, platform setup, and limitations.
    """

    def __init__(
        self,
        channel=0,
        *,
        bitrate=500_000,
        data_bitrate=2_000_000,
        fd=False,
        serial=None,
        product_id=None,
        usb_bus=None,
        usb_address=None,
        termination=None,
        sample_point=87.5,
        data_sample_point=80.0,
        listen_only=False,
        loopback=False,
        receive_own_messages=False,
        non_iso=False,
        one_shot=False,
        auto_recover_bus_off=False,
        can_filters=None,
        rx_queue_size=10_000,
        detach_kernel_driver=False,
        **kwargs,
    ):
        self._is_shutdown = True
        self._session = None
        self._shutdown_lock = threading.Lock()
        if isinstance(channel, str) and channel.isdecimal():
            channel = int(channel)
        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel < 8:
            raise ValueError("channel must be a zero-based integer (0..7)")
        if termination not in (None, 0, 120):
            raise ValueError("termination must be None, 0, or 120 ohms")
        if not isinstance(rx_queue_size, int) or rx_queue_size < 1:
            raise ValueError("rx_queue_size must be a positive integer")
        if listen_only and loopback:
            raise ValueError("listen_only and loopback are mutually exclusive")
        if non_iso and not fd:
            raise ValueError("non_iso requires fd=True")
        if not isinstance(auto_recover_bus_off, bool):
            raise ValueError("auto_recover_bus_off must be True or False")
        if kwargs.get("timing") is not None:
            raise ValueError("explicit BitTiming is unsupported; use bitrate and sample_point")
        br = bitrate_payload(
            bitrate, data_bitrate if fd else bitrate, sample_point, data_sample_point
        )
        self.channel = channel
        self.fd = fd
        self._br = br
        self._auto_recover = auto_recover_bus_off
        self._recovery_lock = threading.Lock()
        self._next_bus_off_probe = 0.0
        self._listen_only = listen_only
        self._receive_own = receive_own_messages
        self._mode = 3 if listen_only else 2 if loopback else 0 if fd else 6
        self._mode |= (8 if non_iso else 0) | (16 if one_shot else 0)
        self._can_protocol = (
            CanProtocol.CAN_FD_NON_ISO
            if non_iso
            else CanProtocol.CAN_FD
            if fd
            else CanProtocol.CAN_20
        )
        try:
            devices = transport.find_devices(
                serial=serial, product_id=product_id, usb_bus=usb_bus, usb_address=usb_address
            )
            if not devices:
                raise CanInitializationError("No matching BUSMUST USB adapter found")
            if len(devices) != 1:
                raise CanInitializationError(
                    "Multiple BUSMUST adapters found; select serial or usb_bus/usb_address"
                )
            dev = devices[0]
            model, count, generation = PRODUCTS[dev.idProduct]
            if channel >= count:
                raise ValueError(f"{model} has {count} channels (0..{count - 1})")
            self._session, self._inbox = transport.open_channel(
                dev,
                channel,
                br,
                self._mode,
                termination,
                receive_own_messages,
                rx_queue_size,
                detach_kernel_driver,
            )
            fw = self._session.fw_version
            fw_text = (
                f" fw {fw >> 24}.{(fw >> 16) & 0xFF}.{(fw >> 8) & 0xFF}.{fw & 0xFF}" if fw else ""
            )
            self.channel_info = (
                f"BUSMUST {model} Gen{generation} USB {dev.bus}:{dev.address}"
                f" channel {channel}{fw_text}"
            )
            super().__init__(channel=channel, can_filters=can_filters, **kwargs)
            self._is_shutdown = False  # BusABC before 4.4 does not set this itself.
        except Exception as exc:
            if self._session is not None:
                transport.close_channel(self._session, channel)
                self._session = None
            self._is_shutdown = True
            if isinstance(exc, (usb.core.USBError, CanOperationError)):
                raise CanInitializationError(f"Cannot initialize BUSMUST adapter: {exc}") from exc
            raise

    def _check_open(self):
        if self._is_shutdown:
            raise CanOperationError("BUSMUST channel is shut down")
        if self._session.error:
            raise self._session.error

    def _probe_bus_off(self):
        """Rate-limited controller health check: a bus-off channel must fail
        sends instead of silently discarding them. Detection latency is
        bounded by the probe interval, like the reference driver's 1 s poll."""
        now = time.monotonic()
        if now < self._next_bus_off_probe:
            return False
        self._next_bus_off_probe = now + _STATUS_PROBE_INTERVAL
        return self.get_status().bus_off

    def recover_bus_off(self) -> CanStatus:
        """BMAPI BM_RecoverBusOff() equivalent: clear a latched bus-off state.

        Recent firmware recovers itself (USB control 0xF5); older adapters
        run the reference driver's loopback dummy-frame procedure, which
        briefly disturbs a shared bus at 1/8 Mbit/s. No-op when healthy;
        raises CanOperationError when bus-off survives recovery.
        """
        self._check_open()
        with self._recovery_lock:
            status = self.get_status()
            if not status.bus_off:
                return status
            fallback = transport.recover_bus_off(self._session, self.channel, self._br, self._mode)
            if fallback:
                while self._inbox.get(0) is not None:  # discard looped-back dummies
                    pass
            status = self.get_status()
            if status.bus_off:
                raise CanOperationError("BUSMUST adapter is stuck in bus-off; power-cycle it")
            self._next_bus_off_probe = time.monotonic() + _STATUS_PROBE_INTERVAL
            return status

    def send(self, msg, timeout=None):
        """Submit a frame to USB; successful return does not imply CAN ACK.

        timeout is seconds (None: 1-second USB bound, 0: minimum 1 ms). A
        rate-limited status probe turns a bus-off controller into
        CanOperationError, or into a recovery plus one retry when
        auto_recover_bus_off was enabled.
        """
        self._check_open()
        if self._listen_only:
            raise CanOperationError("Cannot send in listen-only mode")
        if msg.is_fd and not self.fd:
            raise ValueError("CAN FD transmission requires fd=True")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and nonnegative")
        if self._probe_bus_off():
            if not self._auto_recover:
                raise CanOperationError(
                    "BUSMUST controller is in bus-off; call recover_bus_off() or reopen"
                )
            self.recover_bus_off()
        self._session.write(encode_message(msg, self.channel, self._receive_own), timeout)

    def _recv_internal(self, timeout):
        self._check_open()
        return self._inbox.get(timeout), False  # BusABC applies software filters.

    def get_status(self) -> CanStatus:
        """Query hardware error flags and counters; does not change controller mode."""
        self._check_open()
        try:
            raw = self._session.control(0xD1, channel=self.channel, size=8)
        except usb.core.USBError as exc:
            raise CanOperationError(f"Cannot read BUSMUST status: {exc}") from exc
        return CanStatus(
            bool(raw[0]), bool(raw[2]), bool(raw[3]), bool(raw[4]), bool(raw[5]), raw[6], raw[7]
        )

    @property
    def state(self):
        status = self.get_status()
        if status.bus_off:
            return BusState.ERROR
        if status.tx_passive or status.rx_passive or self._listen_only:
            return BusState.PASSIVE
        return BusState.ACTIVE

    def shutdown(self):
        with self._shutdown_lock:
            if self._is_shutdown:
                return
            super().shutdown()  # Stop python-can periodic tasks before releasing USB.
            transport.close_channel(self._session, self.channel)
            # Keep the closed session reference for concurrent recv/send callers
            # that passed their initial open check just before shutdown began.

    @staticmethod
    def _detect_available_configs():
        configs = []
        for dev in transport.find_devices():
            for channel in range(PRODUCTS[dev.idProduct][1]):
                configs.append(
                    dict(
                        interface="busmust",
                        channel=channel,
                        product_id=dev.idProduct,
                        usb_bus=dev.bus,
                        usb_address=dev.address,
                    )
                )
        return configs
