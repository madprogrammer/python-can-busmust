import queue
import struct
from types import SimpleNamespace

import pytest
import usb.core

from busmust import transport


def rx_packet(channel=0, arb_id=0x123, data=b"\x01\x02", ctrl=None, ticks=100, kind=2, tail=None):
    """Independent firmware fixture; identifier is already in wire order."""
    if ctrl is None:
        ctrl = len(data)
    payload = struct.pack("<II", arb_id, ctrl) + data
    payload += bytes(-len(payload) % 4)
    if tail is not None:
        payload += bytes(8) + struct.pack("<Q", tail)
    header = kind | 0xF00 | (channel << 12) | (0x10 if tail is not None else 0)
    return struct.pack("<HHI", header, len(payload), ticks) + payload


class FakeDevice:
    idVendor = 0x0810
    idProduct = 0xF023
    bus = 1
    address = 2
    port_numbers = (1, 2)
    serial_number = "TEST123"

    def __init__(self):
        self.incoming = queue.Queue()
        self.controls = []
        self.writes = []
        self.claims = self.releases = self.disposals = 0
        self.detaches = self.attaches = 0
        self.active = False
        self.short_write = False
        self.write_error = None
        self.fail_control = None
        self.short_status = False
        self.status = bytes(8)
        self.bus_off = False  # 0xD1 reports bus-off until a dummy-frame burst arrives
        self.stuck_bus_off = False  # bus-off that no recovery can clear
        self.recover_after_bursts = 1  # how many dummy bursts clearing bus-off takes
        self.fw_version = bytes(4)  # zeros: older than every F5 threshold
        self.f5_recovers = False  # F5 control clears bus_off when True
        self.f5_calls = 0
        self.loopback = False
        self.bridge = False
        self.configurations = 0
        self.endpoints = [
            SimpleNamespace(bmAttributes=2, bEndpointAddress=0x82),
            SimpleNamespace(bmAttributes=2, bEndpointAddress=0x02),
        ]

    def is_kernel_driver_active(self, interface):
        assert interface == 0
        return self.active

    def detach_kernel_driver(self, interface):
        self.detaches += 1
        self.active = False

    def attach_kernel_driver(self, interface):
        self.attaches += 1
        self.active = True

    def get_active_configuration(self):
        return {(0, 0): self.endpoints}

    def set_configuration(self):
        self.configurations += 1

    def ctrl_transfer(self, direction, request, wValue, wIndex, data_or_wLength, timeout):
        self.controls.append((direction, request, wValue, wIndex, data_or_wLength))
        if self.fail_control == (request, wIndex):
            raise usb.core.USBError("test control failure")
        if direction == 0xC0:
            if request == 0xF1:
                return self.fw_version
            if request == 0xF5:
                self.f5_calls += 1
                if self.f5_recovers:
                    self.bus_off = False
                return b""
            if request == 0xD1 and (self.bus_off or self.stuck_bus_off):
                return b"\x01" + bytes(7)
            return self.status[:4] if self.short_status else self.status
        return len(data_or_wLength)

    def read(self, ep, size, timeout):
        assert ep == 0x82
        assert timeout > 0
        try:
            data = self.incoming.get(timeout=timeout / 1000)
        except queue.Empty:
            raise usb.core.USBTimeoutError("test timeout")
        if isinstance(data, Exception):
            raise data
        return data

    def write(self, ep, packet, timeout):
        assert ep == 0x02
        assert timeout > 0
        if self.write_error:
            raise self.write_error
        self.writes.append((bytes(packet), timeout))
        if self.bus_off and not self.stuck_bus_off and len(packet) >= 4096:
            self.recover_after_bursts -= 1  # recovery dummy bursts clear bus-off
            if self.recover_after_bursts <= 0:
                self.bus_off = False
        if self.loopback or self.bridge:
            header = struct.unpack_from("<H", packet)[0]
            channel = (header >> 8) & 0xF
            if self.bridge:
                channel = 1 - channel
            self.incoming.put(struct.pack("<H", 2 | 0xF00 | channel << 12) + packet[2:])
        return len(packet) - 1 if self.short_write else len(packet)


@pytest.fixture
def fake_usb(monkeypatch):
    device = FakeDevice()
    devices = [device]
    monkeypatch.setattr(usb.core, "find", lambda **kwargs: iter(devices))
    monkeypatch.setattr(
        transport.usb.util,
        "claim_interface",
        lambda dev, interface: setattr(dev, "claims", dev.claims + 1),
    )
    monkeypatch.setattr(
        transport.usb.util,
        "release_interface",
        lambda dev, interface: setattr(dev, "releases", dev.releases + 1),
    )
    monkeypatch.setattr(
        transport.usb.util,
        "dispose_resources",
        lambda dev: setattr(dev, "disposals", dev.disposals + 1),
    )
    yield device, devices
    # Always clean leaked sessions even if an assertion failed, then fail the test.
    leaked = list(transport._sessions.values())
    for session in leaked:
        for channel in list(session.channels):
            transport.close_channel(session, channel)
        if not session.stop.is_set():
            session.close()
    transport._sessions.clear()
    assert not leaked, "test leaked an open USB session"
