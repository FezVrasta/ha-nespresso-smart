"""Pure codec for the Nespresso Smart BLE protocol.

No Home Assistant or bleak imports live here on purpose: everything in this
module is a plain function over bytes, so it can be unit tested and reused by
tools/probe.py.

Every decoder mirrors a class in the vendor SDK; see PROTOCOL.md.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import IntEnum


class MachineState(IntEnum):
    """MachineStatus.MachineState from the vendor SDK."""

    FACTORY_RESET = 0
    HEATUP = 1
    READY = 2
    DESCALING_READY = 3
    BREWING = 4
    CLEANING = 5
    DESCALING = 6
    EMPTYING = 7
    DEVICE_ERROR = 8
    POWER_SAVE = 9
    COOLDOWN = 10
    SERVICE_MODE = 11
    STANDBY = 12
    UPDATING = 13
    RINSING = 14
    CAPSULE_READING = 17
    DESCALE_SEQUENCE_DECODING = 18
    TANK_EMPTY = 19
    DESCALING_PAUSED = 20
    INITIALIZATION = 21
    RINSING_READY = 22
    MAINTENANCE_MENU = 23
    CLEANING_PAUSED = 26
    EMPTYING_READY = 33
    CLEANING_READY = 34
    READY_OLD_CAPSULE = 35
    RINSING_PAUSED = 36
    UNKNOWN = 255


class PairingKeyState(IntEnum):
    """MachineStatus.PairingKeyState / CCMIDType.CMIDTypeEnum."""

    NONE = 0
    TEMPORARY = 1
    FINAL = 2
    UNDEFINED = 3
    UNKNOWN = 255


#: States in which the machine considers itself bound to a controller.
BOUND_STATES = frozenset({PairingKeyState.TEMPORARY, PairingKeyState.FINAL})


# --- Pairing -------------------------------------------------------------


def generate_pairing_seed() -> str:
    """Return a fresh 32-hex-character pairing seed.

    Mirrors PairingUtils.generatePairingKey(): SHA-1 of a random UUID string,
    rendered as an *unsigned big-endian integer* in base 16 (so leading zero
    nibbles vanish) and then truncated to 32 characters.
    """
    digest = hashlib.sha1(str(uuid.uuid4()).encode("utf-8")).digest()
    as_hex = format(int.from_bytes(digest, "big"), "x")
    # A 20-byte digest renders as at most 40 hex chars and, unless the top
    # 4 bytes are all zero (p < 2^-32), at least 33 -- but be defensive.
    return as_hex.rjust(32, "0")[:32]


def derive_secret(seed: str) -> bytes:
    """Derive the 8-byte value written to the CMID characteristic.

    Mirrors PairingUtils.prepareHashForPairing() followed by
    getBufferFromByteArray(). The result is the first 15 nibbles of the seed
    shifted right by one nibble, with a literal ``8`` prepended.
    """
    if len(seed) < 15:
        raise ValueError("pairing seed must be at least 15 hex characters")

    raw = bytes.fromhex((seed + "0")[:16])

    out = bytearray(8)
    out[0] = ((raw[0] & 0xF0) >> 4) | 0x80
    for i in range(1, 8):
        out[i] = ((raw[i - 1] & 0x0F) << 4) | ((raw[i] & 0xF0) >> 4)
    return bytes(out)


# --- MachineStatus (06AA3A12) -------------------------------------------


@dataclass(frozen=True)
class MachineStatus:
    """Decoded MachineStatus payload."""

    state: MachineState
    pairing_key_state: PairingKeyState
    bootloader_active: bool
    error_present: bool
    led_signaling_active: bool
    descaling_needed: bool
    cleaning_needed: bool
    water_tank_empty: bool
    brewing_unit_closed: bool
    capsule_container_full: bool
    manual_cup_programming: bool
    milk_frother_running: bool
    raw: bytes = field(repr=False)


def decode_machine_status(data: bytes) -> MachineStatus:
    """Decode the MachineStatus characteristic.

    The vendor SDK zero-pads short payloads to 8 bytes before decoding, so we
    do the same rather than rejecting them.
    """
    if not data:
        raise ValueError("empty MachineStatus payload")

    buf = bytes(data).ljust(8, b"\x00")
    b0, b1, b2 = buf[0], buf[1], buf[2]

    raw_state = (b1 & 0x0F) + (b2 & 0xF0)
    try:
        state = MachineState(raw_state)
    except ValueError:
        state = MachineState.UNKNOWN

    return MachineStatus(
        state=state,
        pairing_key_state=PairingKeyState((b0 & 0x60) >> 5),
        bootloader_active=bool(b0 & 0x80),
        error_present=bool(b0 & 0x10),
        led_signaling_active=bool(b0 & 0x08),
        descaling_needed=bool(b0 & 0x04),
        cleaning_needed=bool(b0 & 0x02),
        water_tank_empty=bool(b0 & 0x01),
        brewing_unit_closed=bool(b1 & 0x80),
        capsule_container_full=bool(b1 & 0x40),
        manual_cup_programming=bool(b1 & 0x20),
        milk_frother_running=bool(b1 & 0x10),
        raw=buf,
    )


# --- MachineInfo (06AA3A21) ---------------------------------------------


def _version(value: int) -> str:
    """Utils.b(): a uint16 rendered as ``major.minor``."""
    return f"{value // 100}.{value % 100}"


def _connectivity_version(value: int) -> str:
    """Three-part version used for the connectivity firmware."""
    return f"{value // 10000}.{(value % 10000) // 100}.{value % 100}"


@dataclass(frozen=True)
class MachineInfo:
    """Decoded MachineInfo payload."""

    hardware_version: str
    bootloader_version: str
    firmware_version: str
    recipe_database_version: str
    connectivity_firmware_version: str
    device_address: str


def decode_machine_info(data: bytes) -> MachineInfo:
    """Decode the 16-byte MachineInfo characteristic (big-endian uint16s)."""
    if len(data) < 16:
        raise ValueError(f"MachineInfo payload too short: {len(data)} bytes")

    def u16(offset: int) -> int:
        return int.from_bytes(data[offset : offset + 2], "big")

    return MachineInfo(
        hardware_version=_version(u16(0)),
        bootloader_version=_version(u16(2)),
        firmware_version=_version(u16(4)),
        recipe_database_version=_version(u16(6)),
        connectivity_firmware_version=_connectivity_version(u16(8)),
        device_address=":".join(f"{b:02x}" for b in data[10:16]),
    )


# --- GeneralUserSettings (06AA3A44) -------------------------------------


@dataclass(frozen=True)
class UserSettings:
    """Decoded GeneralUserSettings payload."""

    auto_power_off_time: int
    water_hardness: int
    standby_time: int


def decode_user_settings(data: bytes) -> UserSettings:
    """Decode the 4-byte GeneralUserSettings characteristic.

    Note the APO time is little-endian here (ByteBufferManager.a) while
    MachineInfo uses big-endian (ByteBufferManager.b).
    """
    if len(data) < 4:
        raise ValueError(f"GeneralUserSettings payload too short: {len(data)} bytes")

    return UserSettings(
        auto_power_off_time=int.from_bytes(data[0:2], "little"),
        water_hardness=data[2],
        standby_time=data[3],
    )


def encode_user_settings(settings: UserSettings) -> bytes:
    """Re-encode GeneralUserSettings for a read-modify-write."""
    return settings.auto_power_off_time.to_bytes(2, "little") + bytes(
        [settings.water_hardness & 0xFF, settings.standby_time & 0xFF]
    )


# --- CMIDType (06AA3A51) ------------------------------------------------


def decode_pairing_key_state(data: bytes) -> PairingKeyState:
    """Decode the CMIDType characteristic."""
    if not data:
        return PairingKeyState.UNKNOWN
    try:
        return PairingKeyState(data[0])
    except ValueError:
        return PairingKeyState.UNKNOWN


# --- Model identification -----------------------------------------------

#: MachineTypeKt.f8164a -- BLE name prefix to marketing name, for the models
#: driven by the vertuonext SDK (VertuoUp and Barista use other GATT profiles).
MODEL_NAMES: dict[str, str] = {
    "CV1": "Vertuo Next",
    "DV1": "Vertuo Next",
    "CV3": "Vertuo Next",
    "DV3": "Vertuo Next",
    "CV2": "Vertuo Pop",
    "DV2": "Vertuo Pop",
    "DV5": "Vertuo Lattissima",
    "CV5": "Vertuo Creatista",
    "CV6": "Vertuo Pop+",
    "DV6": "Vertuo Pop+",
}

#: Advertised-name prefixes we accept during discovery.
VERTUO_NAME_PREFIXES: tuple[str, ...] = (*MODEL_NAMES, "Venus")


def model_from_name(name: str | None) -> str | None:
    """Map a BLE advertised name to a marketing model name."""
    if not name:
        return None
    upper = name.upper()
    for prefix, model in MODEL_NAMES.items():
        if upper.startswith(prefix):
            return model
    return None


def model_from_serial(serial: str | None) -> str | None:
    """Map a serial number to a marketing model name.

    The serial embeds the model code (e.g. ``12345DV2a09876543Zz`` -> DV2 ->
    Vertuo Pop). Useful because the machine does not put its local name in
    every BLE advertisement, so discovery may only know the MAC address.
    """
    if not serial:
        return None
    upper = serial.upper()
    for prefix, model in MODEL_NAMES.items():
        if prefix in upper:
            return model
    return None


def is_vertuo_name(name: str | None) -> bool:
    """Whether a BLE advertised name looks like a vertuonext-profile machine."""
    if not name:
        return False
    upper = name.upper()
    return any(upper.startswith(p.upper()) for p in VERTUO_NAME_PREFIXES)
