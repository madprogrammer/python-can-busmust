import struct
from concurrent.futures import ThreadPoolExecutor

import can
import pytest
import usb.core
from conftest import FakeDevice, rx_packet

from busmust import BusMustBus, transport


def test_installed_plugin_configuration_and_cleanup(fake_usb):
    dev, _ = fake_usb
    with can.Bus(
        interface="busmust", channel="1", fd=True, termination=120, ignore_config=True
    ) as bus:
        assert isinstance(bus, BusMustBus)
        assert bus.protocol == can.CanProtocol.CAN_FD
        assert dev.controls[0] == (0xC0, 0xF1, 0, 0, 4)  # firmware version read
        assert dev.controls[1:7] == [
            (0x40, 0xC0, 4, 1, b""),
            (0x40, 0xC2, 0, 1, bytes.fromhex("f401d0075750000000000000")),
            (0x40, 0xC8, 0, 1, b"\x01" + bytes(31)),
            (0x40, 0xC8, 1, 1, bytes(32)),
            (0x40, 0xC3, 120, 1, b""),
            (0x40, 0xC0, 0, 1, b""),
        ]
        bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=[1]), timeout=0)
        assert dev.writes[0][1] == 1
        dev.incoming.put(rx_packet(channel=1))
        msg = bus.recv(1)
        assert msg.arbitration_id == 0x123 and msg.channel == 1
        assert bus.recv(0) is None
    bus.shutdown()
    assert dev.claims == dev.releases == dev.disposals == 1
    assert dev.configurations == 0
    assert dev.controls[-1] == (0x40, 0xC0, 4, 1, b"")


def test_discovery_and_selection(fake_usb):
    dev, devices = fake_usb
    assert can.detect_available_configs(interfaces="busmust") == [
        dict(interface="busmust", channel=c, product_id=0xF023, usb_bus=1, usb_address=2)
        for c in range(2)
    ]
    other = FakeDevice()
    other.address, other.serial_number = 3, "OTHER"
    devices.append(other)
    with pytest.raises(can.CanInitializationError, match="Multiple"):
        BusMustBus()
    with BusMustBus(serial="OTHER") as bus:
        assert bus._session.dev is other
    with pytest.raises(can.CanInitializationError, match="No matching"):
        BusMustBus(serial="missing")
    with pytest.raises(ValueError, match="2 channels"):
        BusMustBus(channel=2, usb_address=2)


def test_open_recovers_latched_bus_off(fake_usb):
    dev, _ = fake_usb
    dev.bus_off = True
    dev.loopback = True
    with can.Bus(interface="busmust", channel=0, fd=True, ignore_config=True) as bus:
        burst = next(p for p, _ in dev.writes if len(p) == 4096)  # 256 dummy RTR frames
        assert burst[:8] == struct.pack("<HHI", 0xF002, 8, 0)
        assert burst[8:16] == struct.pack("<II", 0, 0x20)
        bitrates = [bytes(d) for _, request, _, _, d in dev.controls if request == 0xC2]
        assert struct.pack("<HH8B", 1000, 8000, 87, 75, 0, 0, 0, 0, 0, 0) in bitrates
        assert struct.pack("<HH8B", 500, 2000, 87, 80, 0, 0, 0, 0, 0, 0) in bitrates
        modes = [(value, index) for _, request, value, index, _ in dev.controls if request == 0xC0]
        assert (2, 0) in modes and modes[-1] == (0, 0)  # loopback during recovery, FD after
        assert not dev.bus_off
        assert bus.recv(0) is None  # looped-back recovery dummies were drained
        bus.send(can.Message(arbitration_id=0x42, is_fd=True, data=bytes(range(12))), timeout=0)
        msg = bus.recv(1)
        assert msg.arbitration_id == 0x42 and bytes(msg.data) == bytes(range(12))


def test_open_fails_when_bus_off_survives_recovery(fake_usb):
    dev, _ = fake_usb
    dev.stuck_bus_off = True
    with pytest.raises(can.CanInitializationError, match="bus-off"):
        BusMustBus()
    assert dev.claims == dev.releases == dev.disposals == 1


def test_open_recovers_bus_off_via_firmware_command(fake_usb):
    dev, _ = fake_usb
    dev.fw_version = bytes([3, 1, 0, 0])  # Gen3 firmware: F5 recovery supported
    dev.f5_recovers = True
    dev.bus_off = True
    dev.loopback = True
    with can.Bus(interface="busmust", channel=0, ignore_config=True) as bus:
        assert dev.f5_calls == 1
        assert not any(len(packet) == 4096 for packet, _ in dev.writes)  # no dummies
        bitrates = [bytes(d) for _, request, _, _, d in dev.controls if request == 0xC2]
        assert struct.pack("<HH8B", 1000, 8000, 87, 75, 0, 0, 0, 0, 0, 0) not in bitrates
        bus.send(can.Message(arbitration_id=1, is_extended_id=False, data=[1]), timeout=0)
        assert bus.recv(1).data == b"\x01"


def test_open_falls_back_when_firmware_recovery_fails(fake_usb):
    dev, _ = fake_usb
    dev.fw_version = bytes([3, 2, 0, 0])  # F5 supported (Gen3 >= 3.1) but ineffective
    dev.bus_off = True
    dev.loopback = True
    with BusMustBus():
        assert dev.f5_calls == 1
        assert any(len(packet) == 4096 for packet, _ in dev.writes)
        assert not dev.bus_off


def test_send_fails_and_recovers_mid_session_bus_off(fake_usb):
    dev, _ = fake_usb
    dev.loopback = True
    with BusMustBus() as bus:
        dev.bus_off = True  # controller enters bus-off mid-session
        with pytest.raises(can.CanOperationError, match="bus-off"):
            bus.send(can.Message(arbitration_id=2, is_extended_id=False, data=[2]), timeout=0)
        assert any(len(packet) == 4096 for packet, _ in dev.writes) is False
        status = bus.recover_bus_off()
        assert not status.bus_off
        assert any(len(packet) == 4096 for packet, _ in dev.writes)
        assert bus.recv(0) is None  # looped-back dummies were drained
        bus.send(can.Message(arbitration_id=2, is_extended_id=False, data=[2]), timeout=0)
        assert bus.recv(1).data == b"\x02"
        assert bus.recover_bus_off().bus_off is False  # healthy no-op


def test_send_auto_recovers_bus_off(fake_usb):
    dev, _ = fake_usb
    dev.loopback = True
    with BusMustBus(auto_recover_bus_off=True) as bus:
        dev.bus_off = True
        bus.send(can.Message(arbitration_id=3, is_extended_id=False, data=[3]), timeout=0)
        assert not dev.bus_off
        assert bus.recv(1).data == b"\x03"
    with pytest.raises(ValueError, match="auto_recover_bus_off"):
        BusMustBus(auto_recover_bus_off="yes")


def test_open_retries_recovery_until_bus_off_clears(fake_usb):
    dev, _ = fake_usb
    dev.bus_off = True
    dev.recover_after_bursts = 3  # a deep bus-off needs several dummy bursts
    dev.loopback = True
    with BusMustBus() as bus:
        assert sum(1 for packet, _ in dev.writes if len(packet) == 4096) == 3
        assert not dev.bus_off
        bus.send(can.Message(arbitration_id=5, is_extended_id=False, data=[5]), timeout=0)
        assert bus.recv(1).data == b"\x05"


def test_atexit_cleanup_survives_late_shutdown(fake_usb):
    dev, _ = fake_usb
    bus = BusMustBus()
    transport._close_all_sessions()  # what atexit does when a script exits
    bus.shutdown()  # python-can's exit hook then calls shutdown again
    assert dev.claims == dev.releases == dev.disposals == 1


def test_multichannel_dispatch_and_independent_close(fake_usb):
    dev, _ = fake_usb
    with BusMustBus(channel=0) as first:
        with BusMustBus(channel=1) as second:
            assert first._session is second._session and dev.claims == 1
            dev.incoming.put(rx_packet(1, data=b"B") + rx_packet(0, data=b"A"))
            assert first.recv(1).data == b"A"
            assert second.recv(1).data == b"B"
            with pytest.raises(can.CanInitializationError, match="already open"):
                BusMustBus(channel=0)
        assert dev.releases == 0
        dev.incoming.put(rx_packet(0, data=b"C"))
        assert first.recv(1).data == b"C"
    assert dev.releases == 1


def test_filters_notifier_and_periodic(fake_usb):
    dev, _ = fake_usb
    dev.loopback = True
    with BusMustBus(can_filters=[dict(can_id=0x123, can_mask=0x7FF, extended=False)]) as bus:
        dev.incoming.put(rx_packet(arb_id=0x124) + rx_packet(arb_id=0x123))
        assert bus.recv(1).arbitration_id == 0x123
        reader = can.BufferedReader()
        notifier = can.Notifier(bus, [reader], timeout=0.02)
        try:
            task = bus.send_periodic(
                can.Message(arbitration_id=0x123, is_extended_id=False, data=[0x55]), 0.02
            )
            assert reader.get_message(1).data == b"\x55"
            task.stop()
        finally:
            notifier.stop()


def test_thread_safe_bus(fake_usb):
    with can.ThreadSafeBus(interface="busmust", ignore_config=True) as bus:
        bus.send(can.Message(arbitration_id=0x123, data=[1]))
        assert bus.recv(0) is None


def test_echo_policy_and_request_bit(fake_usb):
    dev, _ = fake_usb
    with BusMustBus() as bus:
        dev.incoming.put(rx_packet(kind=10) + rx_packet(data=b"R"))
        msg = bus.recv(1)
        assert msg.is_rx and msg.data == b"R"
    with BusMustBus(receive_own_messages=True) as bus:
        bus.send(can.Message(data=[1]))
        assert struct.unpack_from("<I", dev.writes[-1][0], 12)[0] & 0x20000
        dev.incoming.put(rx_packet(kind=10))
        assert bus.recv(1).is_rx is False


@pytest.mark.parametrize(
    "flag,state", [(0, can.BusState.ERROR), (2, can.BusState.PASSIVE), (3, can.BusState.PASSIVE)]
)
def test_status(fake_usb, flag, state):
    dev, _ = fake_usb
    with BusMustBus() as bus:
        status = bytearray(8)  # set after opening: runtime status, not open-time state
        status[flag], status[6], status[7] = 1, 42, 12
        dev.status = status
        assert bus.state == state
        assert bus.get_status().tx_error_counter == 42
        dev.short_status = True
        with pytest.raises(can.CanOperationError, match="Short USB control"):
            bus.get_status()


def test_listen_only_and_send_validation(fake_usb):
    with BusMustBus(listen_only=True) as bus:
        assert bus.state == can.BusState.PASSIVE
        with pytest.raises(can.CanOperationError, match="listen-only"):
            bus.send(can.Message())
    with BusMustBus() as bus:
        with pytest.raises(ValueError, match="fd=True"):
            bus.send(can.Message(is_fd=True))
        for timeout in (-1, float("inf"), float("nan")):
            with pytest.raises(ValueError, match="timeout"):
                bus.send(can.Message(), timeout)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(channel=-1),
        dict(channel=1.2),
        dict(termination=60),
        dict(rx_queue_size=0),
        dict(listen_only=True, loopback=True),
        dict(non_iso=True),
        dict(timing=object()),
    ],
)
def test_bad_options_before_usb(fake_usb, kwargs):
    dev, _ = fake_usb
    with pytest.raises(ValueError):
        BusMustBus(**kwargs)
    assert dev.claims == 0


def test_write_errors(fake_usb):
    dev, _ = fake_usb
    with BusMustBus() as bus:
        dev.short_write = True
        with pytest.raises(can.CanOperationError, match="Short USB write"):
            bus.send(can.Message())
        dev.write_error = usb.core.USBTimeoutError("timeout")
        with pytest.raises(can.CanTimeoutError):
            bus.send(can.Message())
        dev.write_error = usb.core.USBError("disconnected")
        with pytest.raises(can.CanOperationError, match="disconnected"):
            bus.send(can.Message())


def test_init_failure_cleans_up_and_preserves_other_channel(fake_usb):
    dev, _ = fake_usb
    dev.fail_control = (0xC2, 0)
    with pytest.raises(can.CanInitializationError):
        BusMustBus()
    assert dev.releases == 1 and not transport._sessions
    dev.fail_control = (0xC2, 1)
    with BusMustBus() as bus:
        with pytest.raises(can.CanInitializationError):
            BusMustBus(channel=1)
        dev.incoming.put(rx_packet())
        assert bus.recv(1) is not None
        assert dev.releases == 1


def test_kernel_driver_ownership(fake_usb):
    dev, _ = fake_usb
    dev.active = True
    with pytest.raises(can.CanInitializationError, match="kernel driver"):
        BusMustBus()
    assert dev.detaches == 0
    with BusMustBus(detach_kernel_driver=True):
        assert dev.detaches == 1 and dev.attaches == 0
    assert dev.attaches == 1


def test_shutdown_wakes_infinite_receiver(fake_usb):
    bus = BusMustBus()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(bus.recv, None)
        bus.shutdown()
        with pytest.raises(can.CanOperationError, match="shut down"):
            future.result(timeout=1)
    with pytest.raises(can.CanOperationError):
        bus.send(can.Message())


@pytest.mark.parametrize(
    "incoming", [usb.core.USBError("disconnected"), struct.pack("<HHI", 2, 1025, 0)]
)
def test_rx_failure_wakes_all_channels(fake_usb, incoming):
    dev, _ = fake_usb
    with BusMustBus() as first, BusMustBus(channel=1) as second:
        dev.incoming.put(incoming)
        for bus in (first, second):
            with pytest.raises(can.CanOperationError, match="receive failed"):
                bus.recv(1)


def test_queue_overflow_is_reported(fake_usb):
    dev, _ = fake_usb
    with BusMustBus(rx_queue_size=1) as bus:
        dev.incoming.put(rx_packet() * 2)
        # Wait for dispatch without racing the consumer against the producer.
        with bus._inbox.condition:
            assert bus._inbox.condition.wait_for(lambda: bus._inbox.error, 1)
        with pytest.raises(can.CanOperationError, match="overflow"):
            bus.recv(0)


def test_discovery_backend_error(monkeypatch):
    def fail(**kwargs):
        raise usb.core.NoBackendError("No backend available")

    monkeypatch.setattr(usb.core, "find", fail)
    with pytest.raises(can.CanInitializationError, match="backend"):
        BusMustBus()


def test_concurrent_channel_open(fake_usb):
    dev, _ = fake_usb
    with ThreadPoolExecutor(2) as pool:
        buses = list(pool.map(lambda channel: BusMustBus(channel=channel), (0, 1)))
    try:
        assert dev.claims == 1
        assert buses[0]._session is buses[1]._session
    finally:
        for bus in buses:
            bus.shutdown()
