"""BLE transport for Nespresso Smart machines.

Wraps bleak with the connect / authenticate / read sequence the vendor SDK
performs. Kept free of Home Assistant imports so tools/probe.py can drive it
directly against a real machine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .protocol import (
    BOUND_STATES,
    MachineInfo,
    MachineStatus,
    PairingKeyState,
    UserSettings,
    decode_machine_info,
    decode_machine_status,
    decode_pairing_key_state,
    decode_user_settings,
    derive_secret,
    encode_user_settings,
    generate_pairing_seed,
)

_LOGGER = logging.getLogger(__name__)

# Characteristic UUIDs (duplicated from const.py so this module needs no
# Home Assistant import chain; const.py re-exports the same values).
CHAR_MACHINE_INFO = "06aa3a21-f22a-11e3-9daa-0002a5d5c51b"
CHAR_SERIAL_NUMBER = "06aa3a31-f22a-11e3-9daa-0002a5d5c51b"
CHAR_CMID = "06aa3a41-f22a-11e3-9daa-0002a5d5c51b"
CHAR_CMID_TYPE = "06aa3a51-f22a-11e3-9daa-0002a5d5c51b"
CHAR_TX_LEVEL = "06aa3a61-f22a-11e3-9daa-0002a5d5c51b"
CHAR_MACHINE_STATUS = "06aa3a12-f22a-11e3-9daa-0002a5d5c51b"
CHAR_USER_SETTINGS = "06aa3a44-f22a-11e3-9daa-0002a5d5c51b"

DEFAULT_TIMEOUT = 20.0

#: How long to wait for an SMP bond. BlueZ's ``Pair()`` is a D-Bus call with no
#: timeout of its own: if the machine never answers the pairing request, or no
#: pairing agent is registered (Home Assistant does not register one), the call
#: never returns. Bonding is best-effort, so give it a short leash.
BOND_TIMEOUT = 10.0

#: How long to wait for a single GATT read or write. Same reasoning: on BlueZ a
#: request the machine ignores leaves the call pending forever.
GATT_TIMEOUT = 15.0

#: How long to wait for a disconnect before giving up on it. This runs in
#: ``finally`` blocks, so it must not be the thing that hangs.
DISCONNECT_TIMEOUT = 10.0

_T = TypeVar("_T")


class VertuoError(Exception):
    """Base error for this integration."""


class VertuoAuthError(VertuoError):
    """The machine rejected our pairing key, or is bound to another key."""


class VertuoNotBoundError(VertuoError):
    """The machine has no pairing key and onboarding was not requested."""


class VertuoTimeoutError(VertuoError):
    """A BLE operation did not complete in time."""


class VertuoNotBondedError(VertuoError):
    """The link is not encrypted yet, so a protected read or write was refused.

    Transient: the refusal itself prompts the stack to bond, so retrying the
    connection generally succeeds. Distinct from VertuoAuthError precisely
    because it says nothing about whether our key is the right one.
    See VertuoDevice._bond.
    """


@dataclass
class VertuoData:
    """One poll's worth of machine data."""

    status: MachineStatus
    info: MachineInfo | None = None
    serial: str | None = None
    settings: UserSettings | None = None


class VertuoDevice:
    """A connection to one Vertuo machine."""

    def __init__(
        self,
        ble_device: BLEDevice,
        seed: str,
        *,
        disconnected_callback: Callable[[], None] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._seed = seed
        self._secret = derive_secret(seed)
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._disconnected_callback = disconnected_callback
        self._status_callback: Callable[[MachineStatus], None] | None = None
        self._notifying = False
        # Cleared only once the backend says it cannot bond at all; a bond
        # that merely failed this time is worth attempting again. See _bond.
        self._try_bonding = True

        # Cached: these never change while connected.
        self._info: MachineInfo | None = None
        self._serial: str | None = None

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def name(self) -> str:
        return self._ble_device.name or self._ble_device.address

    @property
    def seed(self) -> str:
        return self._seed

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Refresh the underlying device (HA hands us a new one per advert)."""
        self._ble_device = ble_device

    def set_status_callback(
        self, callback: Callable[[MachineStatus], None] | None
    ) -> None:
        """Register a callback fired on every MachineStatus notification."""
        self._status_callback = callback

    # --- timeouts --------------------------------------------------------

    async def _bounded(
        self,
        operation: Coroutine[Any, Any, _T],
        timeout: float,
        what: str,
    ) -> _T:
        """Await one BLE operation, turning a stuck one into an error.

        Nothing below us imposes a deadline: bleak hands BlueZ requests to
        D-Bus and waits for a reply that a machine which has stopped answering
        will never send. Unbounded, that surfaces as a config flow spinner
        that never resolves and a coordinator refresh that never completes.
        """
        try:
            async with asyncio.timeout(timeout):
                return await operation
        except TimeoutError as err:
            raise VertuoTimeoutError(f"{what} timed out after {timeout:.0f}s") from err

    # --- connection ------------------------------------------------------

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("%s: disconnected", self.name)
        self._notifying = False
        if self._disconnected_callback:
            self._disconnected_callback()

    async def connect(self) -> None:
        """Connect, bond and authenticate. Idempotent."""
        async with self._lock:
            await self._connect_locked()

    async def connect_unauthenticated(self) -> None:
        """Connect and bond, but do not write the pairing key.

        Used during onboarding: an unbound machine has no key to authenticate
        against, and the pairing state must be read before anything is written.
        """
        async with self._lock:
            if self.is_connected:
                return
            self._client = await establish_connection(
                BleakClient,
                self._ble_device,
                self.name,
                disconnected_callback=self._on_disconnect,
                timeout=DEFAULT_TIMEOUT,
            )
            await self._bond()

    async def authenticate(self) -> None:
        """Write the pairing key on an already-open connection."""
        async with self._lock:
            await self._authenticate()

    async def _connect_locked(self) -> None:
        if self.is_connected:
            return

        _LOGGER.debug("%s: connecting", self.name)
        self._client = await establish_connection(
            BleakClient,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnect,
            timeout=DEFAULT_TIMEOUT,
        )

        await self._bond()
        await self._authenticate()

    async def _bond(self) -> None:
        """Establish an SMP bond, without ever blocking on one.

        The bond is real, not decorative: on BlueZ the CMID write comes back
        ``[org.bluez.Error.NotPermitted] Not paired`` until the link is
        encrypted. But it must not be a hard gate either, for two reasons.

        It can hang. ``Pair()`` is a D-Bus call that returns when bluetoothd
        resolves the request, and bluetoothd has no deadline of its own: a
        machine that ignores the SMP exchange, or an install with no pairing
        agent registered, leaves it pending forever.

        And it can be unnecessary. A failed write is itself enough to make
        BlueZ raise the bond, so pressing on and letting _authenticate() fail
        is a *recovery path*, not a dead end -- the caller retries and the
        second attempt succeeds. Failing loudly beats hanging silently.

        Only NotImplementedError disables future attempts: that is a fixed
        property of the backend (macOS bonds implicitly instead). Everything
        else may well succeed on the next connection, so keep trying.
        """
        assert self._client is not None
        if not self._try_bonding:
            return
        try:
            await self._bounded(self._client.pair(), BOND_TIMEOUT, "bonding")
        except NotImplementedError:
            self._try_bonding = False
            _LOGGER.debug("%s: pair() unsupported on this backend, skipping", self.name)
        except VertuoTimeoutError:
            _LOGGER.debug(
                "%s: bonding did not complete within %.0fs, continuing anyway -- "
                "the write below may prompt the stack to bond",
                self.name,
                BOND_TIMEOUT,
            )
        except Exception as err:
            # "Already paired", "no agent available" and friends are benign,
            # and the backends disagree about which exception type they use.
            _LOGGER.debug("%s: pair() returned %s (continuing)", self.name, err)

    async def _write(self, char: str, data: bytes, what: str) -> None:
        """Write a characteristic, naming BlueZ's unencrypted-link refusal.

        Until the link is bonded, BlueZ answers a protected write with
        ``[org.bluez.Error.NotPermitted] Not paired``. That is worth its own
        exception type because it is transient -- the refusal is what prompts
        the bond, so the next connection attempt succeeds.
        """
        assert self._client is not None
        try:
            await self._bounded(
                self._client.write_gatt_char(char, data, response=True),
                GATT_TIMEOUT,
                what,
            )
        except BleakError as err:
            if "not paired" in str(err).lower():
                raise VertuoNotBondedError(
                    f"{what} needs a bonded connection: {err}"
                ) from err
            raise

    async def _read(self, char: str, what: str) -> bytearray:
        """Read a characteristic, naming BlueZ's unencrypted-link refusal.

        The counterpart to _write: the protected characteristics are refused
        on an unbonded link in both directions, so a read can be transient
        for exactly the same reason a write can.
        """
        assert self._client is not None
        try:
            return await self._bounded(
                self._client.read_gatt_char(char), GATT_TIMEOUT, what
            )
        except BleakError as err:
            if "not paired" in str(err).lower():
                raise VertuoNotBondedError(
                    f"{what} needs a bonded connection: {err}"
                ) from err
            raise

    async def _authenticate(self) -> None:
        """Write the pairing secret to the CMID characteristic."""
        assert self._client is not None
        try:
            await self._write(CHAR_CMID, self._secret, "writing the pairing key")
        except BleakError as err:
            raise VertuoAuthError(f"writing pairing key failed: {err}") from err

        # Prove it worked: MachineStatus is only readable once authenticated.
        # Read through _read, so that a refusal for want of a bond stays a
        # VertuoNotBondedError and gets retried, rather than being reported as
        # "this machine belongs to someone else" -- a conclusion this failure
        # does not actually support.
        try:
            await self._read(CHAR_MACHINE_STATUS, "reading MachineStatus")
        except BleakError as err:
            raise VertuoAuthError(
                f"the machine did not accept our pairing key ({err}). It is most "
                "likely bound to a different key, though a dropped link looks the "
                "same from here -- retry before factory-resetting the machine"
            ) from err

    async def disconnect(self) -> None:
        async with self._lock:
            if self._client is not None:
                try:
                    await self._bounded(
                        self._client.disconnect(),
                        DISCONNECT_TIMEOUT,
                        "disconnecting",
                    )
                except (BleakError, VertuoTimeoutError) as err:
                    _LOGGER.debug("%s: error on disconnect: %s", self.name, err)
                self._client = None
            self._notifying = False

    # --- onboarding ------------------------------------------------------

    async def read_pairing_state(self) -> PairingKeyState:
        """Read CMIDType. Readable without authentication."""
        if self._client is None:
            raise VertuoError("not connected")
        data = await self._read(CHAR_CMID_TYPE, "reading the pairing state")
        return decode_pairing_key_state(data)

    @staticmethod
    def new_seed() -> str:
        """Generate a fresh pairing seed to onboard an unbound machine."""
        return generate_pairing_seed()

    async def onboard(self) -> None:
        """Bind an unpaired machine to our seed.

        Mirrors the app: set the TX level, then write the CMID. The caller must
        already be connected and bonded.
        """
        assert self._client is not None

        state = await self.read_pairing_state()
        if state in BOUND_STATES:
            _LOGGER.debug("%s: already bound (%s)", self.name, state.name)
            return

        _LOGGER.debug("%s: onboarding (state was %s)", self.name, state.name)
        await self._write(CHAR_TX_LEVEL, bytes([1]), "setting the TX level")
        await self._write(CHAR_CMID, self._secret, "writing the pairing key")
        await asyncio.sleep(2)

        state = await self.read_pairing_state()
        if state not in BOUND_STATES:
            raise VertuoAuthError(
                f"onboarding failed, machine still reports {state.name}"
            )

    # --- notifications ---------------------------------------------------

    def _handle_status_notify(self, _sender: object, data: bytearray) -> None:
        if self._status_callback is None:
            return
        try:
            self._status_callback(decode_machine_status(bytes(data)))
        except (ValueError, IndexError) as err:
            _LOGGER.debug("%s: bad status notification %s: %s", self.name, data, err)

    async def start_notifications(self) -> None:
        """Subscribe to MachineStatus so state changes arrive immediately."""
        async with self._lock:
            await self._connect_locked()
            if self._notifying:
                return
            assert self._client is not None
            try:
                await self._bounded(
                    self._client.start_notify(
                        CHAR_MACHINE_STATUS, self._handle_status_notify
                    ),
                    GATT_TIMEOUT,
                    "subscribing to MachineStatus",
                )
                self._notifying = True
            except (BleakError, VertuoTimeoutError) as err:
                # Not fatal -- polling still works.
                _LOGGER.debug("%s: could not subscribe to status: %s", self.name, err)

    # --- reads -----------------------------------------------------------

    async def update(self) -> VertuoData:
        """Connect if needed and read the full machine state."""
        async with self._lock:
            await self._connect_locked()
            assert self._client is not None
            client = self._client

            status = decode_machine_status(
                await self._read(CHAR_MACHINE_STATUS, "reading MachineStatus")
            )

            if self._info is None:
                self._info = await self._read_optional(
                    client, CHAR_MACHINE_INFO, decode_machine_info, "machine info"
                )
            if self._serial is None:
                raw = await self._read_optional(
                    client, CHAR_SERIAL_NUMBER, bytes, "serial number"
                )
                if raw is not None:
                    self._serial = raw.split(b"\x00", 1)[0].decode(
                        "utf-8", errors="replace"
                    )

            settings = await self._read_optional(
                client, CHAR_USER_SETTINGS, decode_user_settings, "user settings"
            )

            return VertuoData(
                status=status,
                info=self._info,
                serial=self._serial,
                settings=settings,
            )

    async def _read_optional(
        self,
        client: BleakClient,
        char: str,
        decoder: Callable[[bytes], object],
        label: str,
    ):
        """Read a non-essential characteristic; log and return None on failure.

        Not every model in the family exposes every characteristic, and a
        missing optional value must not fail the whole update -- including
        one this machine happens to protect more tightly than the rest.
        """
        try:
            raw = await self._read(char, f"reading {label}")
            return decoder(bytes(raw))
        except (
            BleakError,
            VertuoTimeoutError,
            VertuoNotBondedError,
            ValueError,
        ) as err:
            _LOGGER.debug("%s: could not read %s: %s", self.name, label, err)
            return None

    # --- writes ----------------------------------------------------------

    async def set_water_hardness(self, level: int) -> None:
        """Set water hardness (0-4) via read-modify-write."""
        if not 0 <= level <= 4:
            raise ValueError("water hardness must be between 0 and 4")

        async with self._lock:
            await self._connect_locked()
            assert self._client is not None
            current = decode_user_settings(
                bytes(await self._read(CHAR_USER_SETTINGS, "reading user settings"))
            )
            updated = UserSettings(
                auto_power_off_time=current.auto_power_off_time,
                water_hardness=level,
                standby_time=current.standby_time,
            )
            await self._write(
                CHAR_USER_SETTINGS,
                encode_user_settings(updated),
                "writing user settings",
            )
