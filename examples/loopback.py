"""Explicit hardware smoke test: python examples/loopback.py [--serial SERIAL]."""

import argparse

import can


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--detach-kernel-driver", action="store_true")
    args = parser.parse_args()
    with can.Bus(
        interface="busmust",
        channel=args.channel,
        serial=args.serial,
        fd=True,
        loopback=True,
        detach_kernel_driver=args.detach_kernel_driver,
    ) as bus:
        sent = can.Message(
            arbitration_id=0x123,
            is_extended_id=False,
            is_fd=True,
            bitrate_switch=True,
            data=bytes(range(12)),
        )
        bus.send(sent, timeout=1)
        received = bus.recv(2)
        if (
            received is None
            or received.arbitration_id != sent.arbitration_id
            or received.data != sent.data
        ):
            raise RuntimeError(f"Loopback did not return the expected frame: {received}")
        print(received)
        print(bus.get_status())


if __name__ == "__main__":
    main()
