"""Tests for the BLE transport's timeouts.

The regression these guard against is issue #1: nothing under bleak imposes a
deadline of its own, so a BlueZ request the machine never answers -- classically
``Pair()`` -- left the config flow spinning forever instead of coming back with
an error.

Run with:  python3 -m pytest tests/ -q      (needs bleak installed)
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from typing import Any

import pytest

# device.py is part of a package whose __init__ imports Home Assistant, so load
# it through a synthetic package the way tools/probe.py does.
_PKG_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nespresso_smart"
)
_pkg = types.ModuleType("nespresso_smart")
_pkg.__path__ = [str(_PKG_DIR)]  # type: ignore[attr-defined]
sys.modules.setdefault("nespresso_smart", _pkg)

_REASON = "bleak is not installed"
device = pytest.importorskip("nespresso_smart.device", reason=_REASON)

SEED = "0123456789abcdef0123456789abcdef"

#: MachineStatus for a bound machine that is ready. See PROTOCOL.md section 4.
READY_STATUS = bytes([0x40, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


class FakeClient:
    """A stand-in for BleakClient that can be told to hang.

    Every operation named in ``hangs`` never completes, which is what a BlueZ
    D-Bus call looks like when the peer stops answering.
    """

    def __init__(self, hangs: set[str] | None = None) -> None:
        self.hangs = hangs or set()
        self.is_connected = True
        self.pair_calls = 0
        self.writes: list[tuple[str, bytes]] = []

    async def _maybe_hang(self, what: str) -> None:
        if what in self.hangs:
            await asyncio.Event().wait()

    async def pair(self, *_args: Any, **_kwargs: Any) -> None:
        self.pair_calls += 1
        await self._maybe_hang("pair")

    async def read_gatt_char(self, char: str) -> bytearray:
        await self._maybe_hang("read")
        if char == device.CHAR_MACHINE_STATUS:
            return bytearray(READY_STATUS)
        if char == device.CHAR_CMID_TYPE:
            return bytearray([2])  # PairingKeyState.FINAL
        return bytearray(4)

    async def write_gatt_char(self, char: str, data: bytes, response: bool) -> None:
        await self._maybe_hang("write")
        self.writes.append((char, bytes(data)))

    async def disconnect(self) -> None:
        await self._maybe_hang("disconnect")
        self.is_connected = False


def _make_device(monkeypatch: pytest.MonkeyPatch, client: FakeClient):
    """A VertuoDevice wired to ``client`` instead of a real radio."""
    ble_device = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="CV5_test")

    async def fake_establish_connection(*_args: Any, **_kwargs: Any) -> FakeClient:
        return client

    monkeypatch.setattr(device, "establish_connection", fake_establish_connection)
    dev = device.VertuoDevice(ble_device, SEED)  # type: ignore[arg-type]
    # is_connected consults self._client, which starts as None.
    return dev


def test_hanging_bond_does_not_block_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Pair() that never returns must not wedge the connect sequence.

    This is the issue #1 failure: HA connected, called Pair(), and sat there.
    """
    client = FakeClient(hangs={"pair"})
    monkeypatch.setattr(device, "BOND_TIMEOUT", 0.05)
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        async with asyncio.timeout(5):
            await dev.connect()

    asyncio.run(scenario())

    # It gave up on the bond and carried on to write the pairing key.
    assert client.pair_calls == 1
    assert [char for char, _ in client.writes] == [device.CHAR_CMID]


def test_bonding_is_retried_on_a_later_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bond that stalled once may well succeed next time, so keep trying.

    The machine genuinely needs the bond -- BlueZ refuses the pairing-key
    write without one -- so giving up on it permanently would strand the
    integration.
    """
    client = FakeClient(hangs={"pair"})
    monkeypatch.setattr(device, "BOND_TIMEOUT", 0.05)
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        await dev.connect()
        client.is_connected = False  # the machine dropped the link
        await dev.connect()

    asyncio.run(scenario())

    assert client.pair_calls == 2


def test_backend_without_pairing_is_only_probed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NotImplementedError is a fixed property of the backend, so latch it."""
    client = FakeClient()

    async def unsupported() -> None:
        client.pair_calls += 1
        raise NotImplementedError

    client.pair = unsupported  # type: ignore[method-assign]
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        await dev.connect()
        client.is_connected = False
        await dev.connect()

    asyncio.run(scenario())

    assert client.pair_calls == 1


def test_unbonded_write_refusal_gets_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BlueZ's "Not paired" refusal is transient and must be distinguishable.

    Reported as a plain auth failure it would read as "wrong pairing key",
    which is exactly the wrong thing to tell someone whose key is fine.
    """
    from bleak.exc import BleakError

    client = FakeClient()

    async def refuse(char: str, data: bytes, response: bool) -> None:
        raise BleakError("[org.bluez.Error.NotPermitted] Not paired")

    client.write_gatt_char = refuse  # type: ignore[method-assign]
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        with pytest.raises(device.VertuoNotBondedError):
            await dev.connect()

    asyncio.run(scenario())


def test_hanging_read_raises_rather_than_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GATT read the machine ignores becomes an error the caller can report."""
    client = FakeClient(hangs={"read"})
    monkeypatch.setattr(device, "GATT_TIMEOUT", 0.05)
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        async with asyncio.timeout(5):
            with pytest.raises(device.VertuoTimeoutError):
                await dev.connect()

    asyncio.run(scenario())


def test_hanging_write_raises_rather_than_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same for the pairing-key write."""
    client = FakeClient(hangs={"write"})
    monkeypatch.setattr(device, "GATT_TIMEOUT", 0.05)
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        async with asyncio.timeout(5):
            with pytest.raises(device.VertuoTimeoutError):
                await dev.connect()

    asyncio.run(scenario())


def test_hanging_disconnect_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """disconnect() runs in finally blocks, so it must never be the hang."""
    client = FakeClient(hangs={"disconnect"})
    monkeypatch.setattr(device, "DISCONNECT_TIMEOUT", 0.05)
    dev = _make_device(monkeypatch, client)

    async def scenario() -> None:
        async with asyncio.timeout(5):
            await dev.connect()
            await dev.disconnect()

    asyncio.run(scenario())

    assert not dev.is_connected


def test_optional_read_timeout_does_not_fail_the_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled optional characteristic degrades that field, not the poll."""
    client = FakeClient()
    dev = _make_device(monkeypatch, client)

    slow = {device.CHAR_MACHINE_INFO, device.CHAR_SERIAL_NUMBER}
    plain_read = client.read_gatt_char

    async def read_gatt_char(char: str) -> bytearray:
        if char in slow:
            await asyncio.Event().wait()
        return await plain_read(char)

    client.read_gatt_char = read_gatt_char  # type: ignore[method-assign]
    monkeypatch.setattr(device, "GATT_TIMEOUT", 0.05)

    async def scenario() -> device.VertuoData:
        async with asyncio.timeout(5):
            await dev.connect()
            return await dev.update()

    data = asyncio.run(scenario())

    assert data.status.state is not None
    assert data.info is None
    assert data.serial is None
