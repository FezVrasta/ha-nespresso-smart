#!/usr/bin/env python3
"""Diagnostic tool for Nespresso Smart machines over BLE.

Run this next to the machine before installing the Home Assistant integration.
It verifies that the reverse-engineered protocol actually matches your unit and
gives you the pairing seed the integration needs.

    pip install bleak bleak-retry-connector

    # 1. find the machine
    python3 tools/probe.py scan

    # 2. bind it to a fresh key (machine must be unpaired -- factory reset it
    #    first if the phone app ever paired with it) and dump everything
    python3 tools/probe.py onboard --address AA:BB:CC:DD:EE:FF

    # 3. later, reconnect with the seed printed in step 2
    python3 tools/probe.py dump --address AA:BB:CC:DD:EE:FF --seed <32-hex>

    # 4. watch live state changes (make a coffee while this runs)
    python3 tools/probe.py watch --address AA:BB:CC:DD:EE:FF --seed <32-hex>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import pathlib
import sys
import types

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import establish_connection

# --- import the integration's modules without pulling in Home Assistant ----
_PKG_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nespresso_smart"
)
if not _PKG_DIR.is_dir():  # pragma: no cover
    sys.exit(f"cannot find {_PKG_DIR}")

_pkg = types.ModuleType("nespresso_smart")
_pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
sys.modules["nespresso_smart"] = _pkg

from nespresso_smart import protocol
from nespresso_smart.device import (
    CHAR_CMID_TYPE,
    CHAR_MACHINE_INFO,
    CHAR_MACHINE_STATUS,
    CHAR_SERIAL_NUMBER,
    CHAR_USER_SETTINGS,
    VertuoDevice,
)

_LOGGER = logging.getLogger("probe")

EXTRA_CHARS = {
    "ProfileVersion": "06aa3a11-f22a-11e3-9daa-0002a5d5c51b",
    "MachineSpecificParams": "06aa3a22-f22a-11e3-9daa-0002a5d5c51b",
    "ErrorInformation": "06aa3a23-f22a-11e3-9daa-0002a5d5c51b",
}


def _hdr(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def _find(address: str | None, timeout: float = 12.0):
    """Return a BLEDevice, either by address or by scanning for Vertuo names."""
    if address:
        print(f"Looking for {address} ...")
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is None:
            sys.exit(
                f"No device {address} found. Is the machine powered on and not "
                "already connected to your phone?"
            )
        return dev

    print(f"Scanning {timeout:.0f}s for Vertuo machines ...")
    found = await BleakScanner.discover(timeout=timeout)
    matches = [d for d in found if protocol.is_vertuo_name(d.name)]
    if not matches:
        sys.exit(
            "No Vertuo machine found. Names seen: "
            + ", ".join(sorted(d.name for d in found if d.name))
            or "(none)"
        )
    if len(matches) > 1:
        print("Multiple machines found, using the first:")
        for d in matches:
            print(f"  {d.address}  {d.name}")
    return matches[0]


async def cmd_scan(args: argparse.Namespace) -> None:
    print(f"Scanning {args.timeout:.0f}s ...")
    devices = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    _hdr("Vertuo machines (vertuonext BLE profile)")
    any_match = False
    for dev, adv in devices.values():
        if protocol.is_vertuo_name(dev.name):
            any_match = True
            model = protocol.model_from_name(dev.name) or "unknown model"
            print(f"  {dev.address}  {dev.name!r}  -> {model}  (RSSI {adv.rssi})")
    if not any_match:
        print("  none")

    if args.all:
        _hdr("All BLE devices seen")
        for dev, adv in sorted(devices.values(), key=lambda x: -x[1].rssi):
            print(f"  {dev.address}  {str(dev.name)!r:32}  RSSI {adv.rssi}")


def _print_status(status: protocol.MachineStatus) -> None:
    print(f"  raw                     {status.raw.hex(' ')}")
    print(f"  state                   {status.state.name} ({status.state.value})")
    print(f"  pairing key state       {status.pairing_key_state.name}")
    for label, value in (
        ("water tank empty", status.water_tank_empty),
        ("capsule container full", status.capsule_container_full),
        ("descaling needed", status.descaling_needed),
        ("cleaning needed", status.cleaning_needed),
        ("error present", status.error_present),
        ("brewing unit closed", status.brewing_unit_closed),
        ("milk frother running", status.milk_frother_running),
        ("LED signaling active", status.led_signaling_active),
        ("bootloader active", status.bootloader_active),
        ("manual cup programming", status.manual_cup_programming),
    ):
        print(f"  {label:<23} {value}")


async def _dump(device: VertuoDevice) -> None:
    client = device._client
    assert client is not None

    _hdr("Raw characteristic reads")
    for name, uuid in {
        "MachineStatus": CHAR_MACHINE_STATUS,
        "MachineInfo": CHAR_MACHINE_INFO,
        "SerialNumber": CHAR_SERIAL_NUMBER,
        "CMIDType": CHAR_CMID_TYPE,
        "GeneralUserSettings": CHAR_USER_SETTINGS,
        **EXTRA_CHARS,
    }.items():
        try:
            raw = bytes(await client.read_gatt_char(uuid))
            print(f"  {name:<22} {raw.hex(' ') or '(empty)'}")
        except Exception as err:
            print(f"  {name:<22} !! {type(err).__name__}: {err}")

    data = await device.update()

    _hdr("Decoded MachineStatus")
    _print_status(data.status)

    _hdr("Decoded MachineInfo")
    if data.info is None:
        print("  unavailable")
    else:
        print(f"  hardware version        {data.info.hardware_version}")
        print(f"  bootloader version      {data.info.bootloader_version}")
        print(f"  firmware version        {data.info.firmware_version}")
        print(f"  recipe database version {data.info.recipe_database_version}")
        print(f"  connectivity firmware   {data.info.connectivity_firmware_version}")
        print(f"  device address          {data.info.device_address}")

    _hdr("Decoded GeneralUserSettings")
    if data.settings is None:
        print("  unavailable")
    else:
        print(f"  auto power off (min?)   {data.settings.auto_power_off_time}")
        print(f"  water hardness          {data.settings.water_hardness}")
        print(f"  standby time            {data.settings.standby_time}")

    _hdr("Identity")
    print(f"  serial number           {data.serial}")

    _hdr("GATT table")
    for service in client.services:
        print(f"  service {service.uuid}")
        for char in service.characteristics:
            print(f"    {char.uuid}  {','.join(char.properties)}")


async def cmd_inspect(args: argparse.Namespace) -> None:
    """Connect read-only: never writes to the machine, so it cannot unpair it."""
    ble_device = await _find(args.address, args.timeout)
    print(f"\nConnecting to {ble_device.name} ({ble_device.address}) ...")

    client = await establish_connection(
        BleakClient, ble_device, ble_device.name or ble_device.address, timeout=30.0
    )
    try:
        print("Connected. (No writes are performed by this command.)")

        _hdr("GATT table")
        for service in client.services:
            print(f"  service {service.uuid}")
            for char in service.characteristics:
                print(f"    {char.uuid}  {','.join(char.properties)}")

        _hdr("Readable without authentication")
        for name, uuid in {
            "CMIDType (pairing state)": CHAR_CMID_TYPE,
            "MachineInfo": CHAR_MACHINE_INFO,
            "SerialNumber": CHAR_SERIAL_NUMBER,
            "MachineStatus": CHAR_MACHINE_STATUS,
            "GeneralUserSettings": CHAR_USER_SETTINGS,
            **EXTRA_CHARS,
        }.items():
            try:
                raw = bytes(await client.read_gatt_char(uuid))
                print(f"  {name:<26} {raw.hex(' ') or '(empty)'}")
            except Exception as err:
                print(f"  {name:<26} !! {type(err).__name__}: {err}")

        # MachineInfo and SerialNumber are readable unauthenticated, so we can
        # sanity-check the decoders before any key is written.
        _hdr("Decoded (unauthenticated reads)")
        try:
            info = protocol.decode_machine_info(
                bytes(await client.read_gatt_char(CHAR_MACHINE_INFO))
            )
            print(f"  hardware version        {info.hardware_version}")
            print(f"  bootloader version      {info.bootloader_version}")
            print(f"  firmware version        {info.firmware_version}")
            print(f"  recipe database version {info.recipe_database_version}")
            print(f"  connectivity firmware   {info.connectivity_firmware_version}")
            print(f"  device address          {info.device_address}")
            expected = (ble_device.name or "").split("_")[-1].lower()
            got = info.device_address.replace(":", "")
            if expected and got == expected:
                print("  ^ matches the advertised name, so the layout is correct")
        except Exception as err:
            print(f"  MachineInfo: !! {err}")

        try:
            raw = bytes(await client.read_gatt_char(CHAR_SERIAL_NUMBER))
            serial = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            print(f"  serial number           {serial}")
        except Exception as err:
            print(f"  SerialNumber: !! {err}")

        try:
            state = protocol.decode_pairing_key_state(
                bytes(await client.read_gatt_char(CHAR_CMID_TYPE))
            )
            _hdr("Verdict")
            print(f"  Pairing key state: {state.name}")
            if state in protocol.BOUND_STATES:
                print(
                    "  The machine is ALREADY BOUND to a controller (most likely the\n"
                    "  phone app). Home Assistant cannot authenticate until you\n"
                    "  factory-reset the machine, then run `probe.py onboard`."
                )
            else:
                print("  The machine is unbound -- `probe.py onboard` will bind it.")
        except Exception as err:
            print(f"\n  Could not read pairing state: {err}")
    finally:
        await client.disconnect()


async def cmd_onboard(args: argparse.Namespace) -> None:
    ble_device = await _find(args.address, args.timeout)
    seed = args.seed or VertuoDevice.new_seed()
    device = VertuoDevice(ble_device, seed)

    print(f"\nConnecting to {ble_device.name} ({ble_device.address}) ...")
    # Connect and bond, but do NOT authenticate yet: an unbound machine has no
    # key to authenticate against, and we want to read the pairing state first.
    await device.connect_unauthenticated()

    state = await device.read_pairing_state()
    print(f"Pairing key state: {state.name}")

    if state in protocol.BOUND_STATES and not args.seed:
        print(
            "\nThis machine is already bound to a pairing key.\n"
            "If that key is the phone app's, Home Assistant cannot authenticate\n"
            "until you factory-reset the machine (hold the button per the manual)\n"
            "and re-run this command."
        )
        await device.disconnect()
        return

    await device.onboard()
    print("Onboarded.")

    print("\n" + "=" * 62)
    print("PAIRING SEED -- save this, the integration asks for it:")
    print(f"  {seed}")
    secret_hex = protocol.derive_secret(seed).hex()
    print(f"  (derived secret written to the machine: {secret_hex})")
    print("=" * 62)

    await device.authenticate()
    await _dump(device)
    await device.disconnect()


async def cmd_dump(args: argparse.Namespace) -> None:
    if not args.seed:
        sys.exit("--seed is required (get it from `probe.py onboard`)")
    ble_device = await _find(args.address, args.timeout)
    device = VertuoDevice(ble_device, args.seed)
    print(f"\nConnecting to {ble_device.name} ({ble_device.address}) ...")
    await device.connect()
    print("Authenticated.")
    await _dump(device)
    await device.disconnect()


async def cmd_watch(args: argparse.Namespace) -> None:
    if not args.seed:
        sys.exit("--seed is required (get it from `probe.py onboard`)")
    ble_device = await _find(args.address, args.timeout)
    device = VertuoDevice(ble_device, args.seed)
    await device.connect()
    print("Authenticated. Watching for status changes -- Ctrl-C to stop.\n")

    last: bytes | None = None

    def on_status(status: protocol.MachineStatus) -> None:
        nonlocal last
        if status.raw == last:
            return
        last = status.raw
        print(f"[notify] {status.raw.hex(' ')}")
        _print_status(status)
        print()

    device.set_status_callback(on_status)
    await device.start_notifications()

    try:
        while True:
            await asyncio.sleep(args.interval)
            data = await device.update()
            if data.status.raw != last:
                last = data.status.raw
                print(f"[poll] {data.status.raw.hex(' ')}")
                _print_status(data.status)
                print()
    except asyncio.CancelledError:
        pass
    finally:
        await device.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a Nespresso Smart machine over BLE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list nearby machines")
    p_scan.add_argument("--timeout", type=float, default=12.0)
    p_scan.add_argument("--all", action="store_true", help="also list all BLE devices")
    p_scan.set_defaults(func=cmd_scan)

    for name, func, helptext in (
        ("inspect", cmd_inspect, "read-only connect: report pairing state and GATT"),
        ("onboard", cmd_onboard, "bind an unpaired machine and print its seed"),
        ("dump", cmd_dump, "connect with a known seed and dump all state"),
        ("watch", cmd_watch, "stream live status changes"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--address", help="BLE MAC (or macOS UUID); omit to scan")
        sp.add_argument("--seed", help="32-hex-character pairing seed")
        sp.add_argument("--timeout", type=float, default=12.0)
        if name == "watch":
            sp.add_argument("--interval", type=float, default=30.0)
        sp.set_defaults(func=func)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
