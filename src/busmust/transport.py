# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared PyUSB ownership and receive dispatch for a physical adapter."""

from __future__ import annotations

import atexit
import logging
import math
import os
import sys
import threading
import time
from collections import deque

import usb.core
import usb.util
from can import CanInitializationError, CanOperationError, CanTimeoutError, Message

from .protocol import (
    BUSOFF_RECOVERY,
    F5_MIN_FW,
    GET_VERSION,
    PRODUCTS,
    VID,
    Decoder,
    ProtocolError,
    TimestampClock,
    bitrate_payload,
    encode_message,
)

log = logging.getLogger(__name__)


class Inbox:
    def __init__(self, size: int, receive_own: bool):
        self.size = size
        self.receive_own = receive_own
        self.messages = deque()
        self.condition = threading.Condition()
        self.error = None

    def fail(self, error):
        with self.condition:
            self.error = error
            self.condition.notify_all()

    def put(self, message):
        with self.condition:
            if self.error or (not message.is_rx and not self.receive_own):
                return
            if len(self.messages) >= self.size:
                self.error = CanOperationError(
                    "BUSMUST receive queue overflow; reopen this channel"
                )
                self.condition.notify_all()
                return
            self.messages.append(message)
            self.condition.notify()

    def get(self, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.error is not None or self.messages, timeout)
            if self.error:
                raise self.error
            return self.messages.popleft() if self.messages else None


def find_devices(*, serial=None, product_id=None, usb_bus=None, usb_address=None):
    try:
        devices = usb.core.find(find_all=True, idVendor=VID)
        result = []
        for dev in devices:
            if dev.idProduct not in PRODUCTS:
                continue
            if product_id is not None and dev.idProduct != product_id:
                continue
            if usb_bus is not None and dev.bus != usb_bus:
                continue
            if usb_address is not None and dev.address != usb_address:
                continue
            if serial is not None and dev.serial_number != serial:
                continue
            result.append(dev)
        return result
    except (usb.core.USBError, ValueError) as exc:
        raise CanInitializationError(f"Cannot enumerate BUSMUST USB devices: {exc}") from exc


def device_key(dev):
    # PyUSB creates new Device wrappers during enumeration. Bus/address identify
    # the underlying device across those wrappers (including backend instances).
    return (dev.bus, dev.address, tuple(dev.port_numbers or ()), dev.idProduct)


class Session:
    def __init__(self, dev, detach_kernel_driver):
        self.dev = dev
        self.channels = {}
        self.channels_lock = threading.Lock()
        self.control_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.closed = False
        self.claimed = False
        self.detached = False
        self.thread = None
        self.decoder = Decoder()
        self.clock = TimestampClock()
        self.generation = PRODUCTS[dev.idProduct][2]
        self.fw_version = 0
        try:
            try:
                active = dev.is_kernel_driver_active(0)
            except NotImplementedError:
                active = False
            if active:
                if not detach_kernel_driver:
                    raise CanInitializationError(
                        "USB interface 0 has a kernel driver; release it or use detach_kernel_driver=True"
                    )
                dev.detach_kernel_driver(0)
                self.detached = True
            try:
                config = dev.get_active_configuration()
            except usb.core.USBError as exc:
                if "Configuration not set" not in str(exc):
                    raise
                dev.set_configuration()
                config = dev.get_active_configuration()
            interface = config[(0, 0)]
            usb.util.claim_interface(dev, 0)
            self.claimed = True
            inputs, outputs = [], []
            for ep in interface:
                if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                    (
                        inputs
                        if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                        else outputs
                    ).append(ep.bEndpointAddress)
            if len(inputs) != 1 or len(outputs) != 1:
                raise CanInitializationError(
                    "Expected one bulk IN and OUT endpoint on USB interface 0"
                )
            self.ep_in, self.ep_out = inputs[0], outputs[0]
            try:
                version = self.control(GET_VERSION, size=4)
                self.fw_version = int.from_bytes(version, "big")
            except usb.core.USBError as exc:
                log.debug("Cannot read BUSMUST firmware version: %s", exc)
                self.fw_version = 0
            self.thread = threading.Thread(target=self._read, name="busmust-usb-rx", daemon=True)
            self.thread.start()
        except Exception:
            self.close()
            raise

    def control(self, request, value=0, channel=0, data=b"", size=None):
        with self.control_lock:
            if self.stop.is_set():  # a closed session must never touch libusb again
                raise CanOperationError("BUSMUST USB session is closed")
            result = self.dev.ctrl_transfer(
                0xC0 if size is not None else 0x40,
                request,
                wValue=value,
                wIndex=channel,
                data_or_wLength=size if size is not None else data,
                timeout=2000,
            )
        actual = len(result) if size is not None else result
        expected = size if size is not None else len(data)
        if actual != expected:
            raise CanOperationError(
                f"Short USB control transfer 0x{request:02x}: {actual}/{expected}"
            )
        return bytes(result) if size is not None else None

    def write(self, packet, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        acquired = (
            self.write_lock.acquire()
            if deadline is None
            else self.write_lock.acquire(timeout=timeout)
        )
        if not acquired:
            raise CanTimeoutError("Timed out waiting for USB transmit lock")
        try:
            if self.stop.is_set() or self.error:
                raise CanOperationError(f"BUSMUST USB session is closed or failed: {self.error}")
            # A PyUSB timeout of zero means infinity, not nonblocking.
            milliseconds = (
                1000
                if deadline is None
                else max(1, math.ceil((deadline - time.monotonic()) * 1000))
            )
            sent = self.dev.write(self.ep_out, packet, timeout=milliseconds)
            if sent != len(packet):
                raise CanOperationError(
                    f"Short USB write: {sent}/{len(packet)} bytes; frame not retried"
                )
        except usb.core.USBTimeoutError as exc:
            raise CanTimeoutError(f"BUSMUST USB transmit timed out: {exc}") from exc
        except usb.core.USBError as exc:
            raise CanOperationError(f"BUSMUST USB transmit failed: {exc}") from exc
        finally:
            self.write_lock.release()

    def _read(self):
        try:
            while not self.stop.is_set():
                if sys.is_finalizing():  # PyUSB finalizers may free libusb mid-read
                    return
                try:
                    data = self.dev.read(self.ep_in, 16 * 1024, timeout=10)
                except usb.core.USBTimeoutError:
                    continue
                for frame in self.decoder.feed(bytes(data)):
                    message = frame.message
                    message.timestamp = self.clock.convert(
                        frame.ticks,
                        frame.utc_us if self.generation >= 3 else None,
                        update=message.is_rx,
                    )
                    with self.channels_lock:
                        inbox = self.channels.get(message.channel)
                    if inbox:
                        inbox.put(message)
        except (usb.core.USBError, ProtocolError) as exc:
            self.error = CanOperationError(f"BUSMUST USB receive failed: {exc}")
            with self.channels_lock:
                for inbox in self.channels.values():
                    inbox.fail(self.error)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.thread:
            self.thread.join(0.5)
            if self.thread.is_alive():
                # The reader is stuck inside libusb (possibly wedged by
                # PyUSB's exit-time finalizers); release would race it.
                log.warning("BUSMUST USB reader did not stop; leaving interface claimed")
                return
        with self.write_lock, self.control_lock:
            if self.claimed:
                try:
                    usb.util.release_interface(self.dev, 0)
                except usb.core.USBError as exc:
                    log.debug("USB release failed during cleanup: %s", exc)
                self.claimed = False
            if self.detached:
                try:
                    self.dev.attach_kernel_driver(0)
                except (usb.core.USBError, NotImplementedError) as exc:
                    log.warning("Could not restore USB kernel driver: %s", exc)
                self.detached = False
            usb.util.dispose_resources(self.dev)


def _bus_off(session, channel):
    return bool(session.control(0xD1, channel=channel, size=8)[0])


RECOVERY_ATTEMPTS = 8  # a deep bus-off (TEC/REC pegged) can need several passes


def recover_bus_off(session, channel, bitrate_data, mode, attempts=RECOVERY_ATTEMPTS):
    """Mirror BMAPI BM_RecoverBusOff().

    Firmware-side recovery (control 0xF5) on Gen2/2.5 >= 2.6.0.0 and
    Gen3 >= 3.1.0.0; otherwise, or when that fails to clear the state, the
    reference bmcan_loopback_recovery(): reconfigure at 1 Mbit/s / 8 Mbit/s
    in internal loopback, flush 256 dummy RTR frames, then restore the
    requested bitrate and mode. Retries up to `attempts` times, like the
    reference driver's restart poll. Returns True when the fallback ran at
    least once (its looped-back dummies need draining); raises nothing.
    """
    fallback = False
    for attempt in range(attempts):
        if session.fw_version >= F5_MIN_FW.get(session.generation, 0):
            session.control(BUSOFF_RECOVERY, channel=channel, size=0)
            time.sleep(0.05)
            if not _bus_off(session, channel):
                return fallback
            log.debug("channel %d: firmware recovery incomplete; using fallback", channel)
        session.control(0xC0, 4, channel)  # configuration mode
        session.control(0xC2, channel=channel, data=bitrate_payload(1_000_000, 8_000_000, 87, 75))
        time.sleep(0.01)
        session.control(0xC0, 2, channel)  # internal loopback
        time.sleep(0.01)
        dummy = encode_message(
            Message(arbitration_id=0, is_extended_id=False, is_remote_frame=True, dlc=0), channel
        )
        session.write(dummy * 256, 1.0)
        time.sleep(0.05)
        session.control(0xC0, 4, channel)
        session.control(0xC2, channel=channel, data=bitrate_data)
        time.sleep(0.01)
        session.control(0xC0, mode, channel)
        time.sleep(0.01)  # firmware drops bulk TX sent while switching modes
        fallback = True
        if not _bus_off(session, channel):
            return fallback
        log.debug("channel %d bus-off survives recovery attempt %d", channel, attempt + 1)
        time.sleep(0.1)
    return fallback


_sessions = {}
_registry_lock = threading.RLock()


def open_channel(
    dev, channel, bitrate_data, mode, termination, receive_own, queue_size, detach_kernel_driver
):
    key = device_key(dev)
    with _registry_lock:
        session = _sessions.get(key)
        if session is None:
            session = Session(dev, detach_kernel_driver)
            _sessions[key] = session
        if channel in session.channels:
            raise CanInitializationError(
                f"BUSMUST channel {channel} is already open in this process"
            )
        inbox = Inbox(queue_size, receive_own)
        try:
            if session.error:
                raise session.error
            session.control(0xC0, 4, channel)  # configuration mode
            session.control(0xC2, channel=channel, data=bitrate_data)
            time.sleep(0.01)  # reference firmware settling interval
            session.control(0xC8, 0, channel, b"\x01" + bytes(31))  # accept all
            session.control(0xC8, 1, channel, bytes(32))  # invalidate second filter
            if termination is not None:
                session.control(0xC3, termination, channel)
            with session.channels_lock:
                if session.error:
                    raise session.error
                session.channels[channel] = inbox
            session.control(0xC0, mode, channel)
            time.sleep(0.01)  # firmware drops bulk TX sent while switching modes
            if _bus_off(session, channel):
                if recover_bus_off(session, channel, bitrate_data, mode):
                    time.sleep(0.01)
                    while inbox.get(0) is not None:  # discard looped-back recovery dummies
                        pass
                if _bus_off(session, channel):
                    raise CanInitializationError(
                        "BUSMUST adapter is stuck in bus-off; power-cycle it"
                    )
            return session, inbox
        except Exception:
            close_channel(session, channel)
            raise


def close_channel(session, channel):
    with _registry_lock:
        with session.channels_lock:
            inbox = session.channels.pop(channel, None)
        if inbox:
            inbox.fail(CanOperationError("BUSMUST channel is shut down"))
        try:
            session.control(0xC0, 4, channel)
        except (usb.core.USBError, CanOperationError) as exc:
            log.debug("Cannot stop CAN channel during cleanup: %s", exc)
        finally:
            if not session.channels:
                _sessions.pop(device_key(session.dev), None)
                session.close()


def _close_all_sessions():
    # Best-effort cleanup when a script exits without shutting its buses down.
    # PyUSB's exit-time finalizers free libusb objects while daemon reader
    # threads may still be inside the library, so USB cleanup is only safe
    # once a session's reader has stopped; sessions whose reader is stuck are
    # left untouched rather than crashed. Posthumous Bus.shutdown() calls fail
    # cleanly through the session-closed guard in Session.control/write.
    with _registry_lock:
        for session in list(_sessions.values()):
            session.stop.set()
        for session in list(_sessions.values()):
            if session.thread:
                session.thread.join(0.25)
                if session.thread.is_alive():
                    continue
            for channel in list(session.channels):
                close_channel(session, channel)


if hasattr(os, "register_atexit"):
    # Runs before threading shutdown and PyUSB's weakref finalizers, which
    # free libusb objects under daemon reader threads and crash the process.
    os.register_atexit(_close_all_sessions)
else:  # older interpreters: best-effort, see _close_all_sessions docstring
    atexit.register(_close_all_sessions)
