"""Exercise real ecosystem packages over the driver and simulated USB wire."""

import asyncio

import can
import cantools
import isotp
from conftest import rx_packet

from busmust import BusMustBus


def test_cantools_dbc_frame(fake_usb):
    dev, _ = fake_usb
    dev.loopback = True
    database = cantools.database.load_string(
        """
VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 291 Measurement: 8 ECU
 SG_ Counter : 0|16@1+ (1,0) [0|65535] "" ECU
""",
        database_format="dbc",
    )
    payload = database.encode_message("Measurement", {"Counter": 1234})
    with BusMustBus() as bus:
        bus.send(can.Message(arbitration_id=291, is_extended_id=False, data=payload))
        received = bus.recv(1)
        assert received is not None
        assert database.decode_message(received.arbitration_id, received.data) == {"Counter": 1234}


def test_isotp_multiframe_flow_control(fake_usb):
    dev, _ = fake_usb
    dev.bridge = True
    with BusMustBus(channel=0) as tester, BusMustBus(channel=1) as ecu:
        notifier_a = can.Notifier(tester, [], timeout=0.01)
        notifier_b = can.Notifier(ecu, [], timeout=0.01)
        stack_a = isotp.NotifierBasedCanStack(
            tester,
            notifier_a,
            address=isotp.Address(isotp.AddressingMode.Normal_11bits, txid=0x700, rxid=0x708),
            params={"blocking_send": True},
        )
        stack_b = isotp.NotifierBasedCanStack(
            ecu,
            notifier_b,
            address=isotp.Address(isotp.AddressingMode.Normal_11bits, txid=0x708, rxid=0x700),
            params={"blocking_send": True},
        )
        try:
            stack_a.start()
            stack_b.start()
            payload = bytes(range(128))
            stack_a.send(payload, send_timeout=2)
            assert stack_b.recv(block=True, timeout=2) == payload
            stack_b.send(b"\x62\xf1\x90" + bytes(range(32)), send_timeout=2)
            assert stack_a.recv(block=True, timeout=2) == b"\x62\xf1\x90" + bytes(range(32))
        finally:
            stack_a.stop()
            stack_b.stop()
            notifier_a.stop()
            notifier_b.stop()


def test_asyncio_notifier(fake_usb):
    dev, _ = fake_usb

    async def receive():
        with BusMustBus() as bus:
            reader = can.AsyncBufferedReader()
            notifier = can.Notifier(bus, [reader], loop=asyncio.get_running_loop(), timeout=0.01)
            try:
                dev.incoming.put(rx_packet(data=b"async"))
                msg = await asyncio.wait_for(reader.get_message(), timeout=1)
                assert msg.data == b"async"
            finally:
                notifier.stop()

    asyncio.run(receive())
