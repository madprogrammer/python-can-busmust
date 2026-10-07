# python-can-busmust

[![PyPI](https://img.shields.io/pypi/v/python-can-busmust)](https://pypi.org/project/python-can-busmust/)
[![CI](https://github.com/madprogrammer/python-can-busmust/actions/workflows/ci.yml/badge.svg)](https://github.com/madprogrammer/python-can-busmust/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/madprogrammer/python-can-busmust?include_prereleases)](https://github.com/madprogrammer/python-can-busmust/releases)

**Release 0.1.0, validated on physical hardware** (two X1 adapters, firmware
2.2.4.10, wired together; see the changelog for the coverage matrix). The
automated tests use simulated USB devices. This is an independent project,
not an official BUSMUST release.

A native Python/PyUSB driver for BUSMUST USB CAN and CAN FD adapters. It talks
directly to the device protocol described by the [bmsocketcan reference](https://github.com/busmust/bmsocketcan/tree/70919fbc2a1be146498ca0df941d1d8a0a93c74b);
it does not load the BMAPI shared library or require SocketCAN.

The installed package registers `interface="busmust"` with `python-can` using
its [plugin interface](https://python-can.readthedocs.io/en/stable/plugin-interface.html).
Standard `can.Message`, software filters, `Notifier`, logging, asynchronous
listeners, `ThreadSafeBus`, and software periodic transmission are supported.
Libraries accepting a `can.BusABC`, such as can-isotp and cantools, can use this
bus directly. See [the protocol analysis](https://github.com/madprogrammer/python-can-busmust/blob/main/docs/protocol.md) for implementation
details and differences from the kernel driver.

## Install

Python 3.9 or newer and a libusb backend are required:

```sh
# macOS
brew install libusb

# Debian/Ubuntu
sudo apt install libusb-1.0-0

# Install the alpha from PyPI, in your Python environment
python -m pip install --pre python-can-busmust

# Or pin this release
python -m pip install python-can-busmust==0.1.0a1
```

On Windows, install a libusb-compatible driver (usually WinUSB via Zadig) for
the adapter's **CAN interface 0** and make libusb available to PyUSB. For a
composite device, keep the other interfaces intact. Switching to WinUSB may
prevent the vendor application's driver from opening that interface. Consult
the [PyUSB installation guide](https://github.com/pyusb/pyusb#installing) for
backend setup.

On Linux, grant your user access to the adapter, for example with a udev rule
`SUBSYSTEM=="usb", ATTR{idVendor}=="0810", MODE="0660", TAG+="uaccess"`.
Reload udev rules and reconnect the adapter. Stop applications using its kernel
CAN interfaces before opening it with `detach_kernel_driver=True`. The default
refuses to detach an active kernel driver; an explicitly detached driver is
reattached when the last bus on that adapter shuts down.

## Nix

The flake provides a Python package, an interpreter containing the driver, a
development shell, and a Python package overlay for Linux and macOS on x86_64
and aarch64. `flake.lock` pins nixpkgs. Nixpkgs' PyUSB includes libusb and finds
it by its store path, so no manual library-path configuration is needed.

```sh
# Use the driver and development tools without pip
nix develop github:madprogrammer/python-can-busmust
python -c 'import can; print(can.interfaces.BACKENDS["busmust"])'

# Or just run Python with the installed driver
nix shell github:madprogrammer/python-can-busmust#python

# From a checkout: build, test, and verify plugin/libusb loading
nix build
nix flake check
```

To include it in another project's `devShell`, apply the overlay to that
project's Python package set. Following the same nixpkgs input keeps Python
and its dependencies consistent:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    busmust = {
      url = "github:madprogrammer/python-can-busmust";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, busmust, ... }:
    let
      system = "x86_64-linux"; # Set to your host system.
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ busmust.overlays.default ];
      };
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          (pkgs.python3.withPackages (ps: [
            ps.busmust ps.can-isotp ps.cantools
          ]))
        ];
      };
    };
}
```

`packages.<system>.default` (also named `busmust`) is a Python library;
`packages.<system>.python` is the ready-to-use interpreter environment.
The default development shell runs local `src/busmust` edits when entered from
a checkout and includes pytest, build, twine, and Ruff. Run `python -m pytest`
there to test changes.

On NixOS, USB access still requires a host udev rule, for example:

```nix
services.udev.extraRules = ''
  SUBSYSTEM=="usb", ATTR{idVendor}=="0810", MODE="0660", TAG+="uaccess"
'';
```

This grants access to the active local login session; headless services need
appropriate device/group permissions. A devShell does not change host USB
permissions or detach an active kernel driver automatically.

## Send and receive

```python
import can

with can.Bus(interface="busmust", channel=0, bitrate=500_000,
             fd=True, data_bitrate=2_000_000, termination=120) as bus:
    bus.send(can.Message(arbitration_id=0x123, is_extended_id=False,
                         data=[0xDE, 0xAD, 0xBE, 0xEF]), timeout=0.5)
    message = bus.recv(timeout=1.0)
    print(message)
```

Channels are **zero-based**. Set bitrates, sample points, and termination to
match the physical bus. `termination=None` preserves the device setting;
use `0` to disable or `120` to enable its resistor.

```python
configs = can.detect_available_configs(interfaces=["busmust"])
print(configs)

# Select an adapter explicitly when more than one is attached.
bus = can.Bus(interface="busmust", channel=0, serial="YOUR_SERIAL",
              bitrate=500_000)
try:
    bus.set_filters([{"can_id": 0x700, "can_mask": 0x700, "extended": False}])
    print(bus.get_status())
finally:
    bus.shutdown()
```

Separate channels on the same adapter share a USB reader and have independent
receive queues. Open each channel once per process; different processes cannot
share the adapter. Closing one channel leaves the others running.

```python
with can.Bus(interface="busmust", channel=0) as first, \
     can.Bus(interface="busmust", channel=1) as second:
    listener = can.BufferedReader()
    notifier = can.Notifier(second, [listener])
    try:
        task = first.send_periodic(
            can.Message(arbitration_id=0x123, is_extended_id=False, data=[1]),
            period=0.1,
        )
        print(listener.get_message(timeout=1.0))
        task.stop()
    finally:
        notifier.stop()
```

This example requires a two-channel adapter and a correctly wired bus between
the channels. `examples/loopback.py` provides a single-channel firmware loopback
smoke test. No hardware transmissions are needed to run the automated tests.

## Options

| Argument | Default | Meaning |
| --- | --- | --- |
| `channel` | `0` | Zero-based physical CAN port; numeric strings accepted |
| `bitrate` | `500_000` | Arbitration bitrate in bit/s; whole kbit/s |
| `fd` | `False` | Enable CAN FD alongside Classical CAN |
| `data_bitrate` | `2_000_000` | CAN FD data bitrate in bit/s |
| `sample_point` | `87.5` | Arbitration sample point in percent |
| `data_sample_point` | `80.0` | Data sample point in percent |
| `termination` | `None` | Preserve, disable (`0`), or enable (`120`) termination |
| `serial` | `None` | Exact USB serial number |
| `product_id` | `None` | Integer USB product ID |
| `usb_bus`, `usb_address` | `None` | USB location selectors returned by discovery |
| `listen_only` | `False` | Receive without acknowledging; sending rejected |
| `loopback` | `False` | Internal firmware loopback |
| `receive_own_messages` | `False` | Request and deliver firmware TX echoes as `is_rx=False` |
| `non_iso` | `False` | Non-ISO CAN FD mode; requires `fd=True` |
| `one_shot` | `False` | Disable firmware automatic retransmission |
| `auto_recover_bus_off` | `False` | Detect bus-off on send and recover, then retry once |
| `can_filters` | `None` | Standard python-can software filters |
| `rx_queue_size` | `10_000` | Maximum buffered frames per channel |
| `detach_kernel_driver` | `False` | Explicitly allow temporary USB interface detachment |

The reference protocol encodes sample points as integer percentages, so 87.5
is sent as 87. Explicit `can.BitTiming`/`BitTimingFd` configuration is unsupported.
Validation checks the wire format's range; firmware determines supported rates.

## Supported device IDs

All use USB vendor ID `0810`. These are the reference implementation's mappings.
The X1 (product ID `F012`, firmware 2.2.4.10) is validated on hardware; other
models follow the same protocol but are not yet bench-tested.

| Generation | Product | Product ID | CAN channels |
| --- | --- | --- | --- |
| 2 | X1 / X1 Pro | F012 / F112 | 1 |
| 2 | X2 / X4 / X8 Pi | F122 / F142 / F182 | 2 / 4 / 8 |
| 2.5 | X2R / X4R | E122 / E142 | 2 / 4 |
| 3 | X1 / X2 / X4 | F013 / F023 / F043 | 1 / 2 / 4 |
| 3 | XL2 / XL4 | 0043 / 0083 | 2 / 4 |

## Behavior and limits

- `send()` waits for USB submission, **not a CAN-bus acknowledgment**. `timeout`
  is in seconds; `None` uses a 1-second USB transfer timeout, and `0` uses a
  minimum 1 ms because libusb interprets zero as infinite. Partial writes are
  reported and never retried automatically. A timed-out transfer may have sent
  data; avoid blind retries for commands with side effects.
- CAN FD payloads between legal DLC lengths are zero-padded to the next legal
  length. Receiving reports the on-wire length, e.g. 9 transmitted bytes become
  a 12-byte frame. RTR messages retain their requested DLC and have empty data.
- Gen3 timestamp tails provide UTC microseconds. Older devices' wrapping
  microsecond clocks are anchored to host epoch time on the first received
  frame; absolute accuracy includes USB latency. Echoes do not advance the
  receive timestamp tracker. Clock reset requires reopening the adapter.
- USB disconnects, malformed CAN envelopes, and queue overflow raise
  `can.CanOperationError`. Overflow fails the affected channel rather than
  silently dropping frames. Reopen failed channels; reconnect all channels
  after a USB session failure. Stop `Notifier` before shutting down its bus.
- Status flags/counters are available through `get_status()` and `bus.state`.
  Channels that come up latched in bus-off are recovered automatically at
  open. Mid-session, `send()` probes the controller at most twice a second
  and raises `CanOperationError` instead of silently discarding frames while
  bus-off; `recover_bus_off()` (the BMAPI `BM_RecoverBusOff` equivalent)
  clears the state — firmware-side on Gen2/2.5 ≥ 2.6.0.0 and Gen3 ≥ 3.1.0.0,
  otherwise via the reference loopback dummy-frame procedure, which briefly
  disturbs a shared bus at 1/8 Mbit/s and can need several passes. Passing
  `auto_recover_bus_off=True` runs that recovery and retries the send once.
  SocketCAN error frames are not synthesized.
- Prefer context managers or explicit `shutdown()`. Exiting a process with
  open channels is cleaned up best-effort, but PyUSB's exit-time finalizers
  can race the background reader thread inside libusb and abort the process
  during teardown; that race is beyond this driver's control.
- Hardware TX tasks, routes, offline logging/replay, persistent configuration,
  PTP synchronization, and hardware acceptance-filter optimization are outside
  this implementation. Periodic sending and filtering run in Python.
- Hardware validation covers the X1 (Gen2, firmware 2.2.4.10); behavior on
  other models, firmware generations, and non-Linux platforms is expected to
  follow the reference protocol but is not yet bench-verified.

## Development and hardware feedback

Clone the repository before running the development commands:

```sh
git clone https://github.com/madprogrammer/python-can-busmust.git
cd python-can-busmust
python -m pip install -e '.[test]'
python -m pytest
python -m build
```

Tests use the real `python-can` and PyUSB packages with a simulated USB device.
They cover protocol vectors, shared-channel lifecycle, transfer failures,
timestamps, plugin discovery, and python-can listener/periodic APIs.

For hardware reports, include the model/product ID, firmware version, OS,
Python version, bitrate/sample-point settings, and a minimal reproduction in
[a GitHub issue](https://github.com/madprogrammer/python-can-busmust/issues).
Start with `python examples/loopback.py`, then test a correctly terminated
two-node bus. Internal loopback alone does not establish physical-bus operation.

## Releases

Published GitHub releases and prereleases trigger the release workflow: validate
the version, run the CI matrix, build and check the wheel/source distribution,
publish to PyPI, and attach those same files to the GitHub release. The current
[GitHub releases](https://github.com/madprogrammer/python-can-busmust/releases)
also provide direct wheel downloads.

See [the release guide](https://github.com/madprogrammer/python-can-busmust/blob/main/docs/releasing.md)
for the version/tag convention, dry runs, credentials, and retry instructions.

## License and provenance

GPL-2.0-or-later, see [LICENSE](https://github.com/madprogrammer/python-can-busmust/blob/main/LICENSE). Protocol behavior was derived from
the GPL-licensed `bmsocketcan/src/bmcan_proto.c`, `bmcan_usb.c`,
`bmcan_netdev.c`, and `inc/bmcan_core.h` (BUSMUST, 2026), at upstream commit
`70919fbc2a1be146498ca0df941d1d8a0a93c74b`. A local reference clone may be kept
in the ignored `bmsocketcan/` directory. Its separately licensed vendor headers are not
copied into or required by this Python distribution.
