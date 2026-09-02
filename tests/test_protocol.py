"""Tests for the Vertuo BLE codec.

Run with:  python3 -m pytest tests/ -q     (or plain `python3 tests/test_protocol.py`)
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_PROTOCOL_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nespresso_smart"
    / "protocol.py"
)
_spec = importlib.util.spec_from_file_location("vertuo_protocol", _PROTOCOL_PATH)
assert _spec and _spec.loader
protocol = importlib.util.module_from_spec(_spec)
sys.modules["vertuo_protocol"] = protocol
_spec.loader.exec_module(protocol)


def test_derive_secret_shifts_nibbles_and_prefixes_8() -> None:
    # Worked by hand from PairingUtils.getBufferFromByteArray():
    # the output is "8" followed by the first 15 nibbles of the input.
    seed = "0123456789abcdef0123456789abcdef"
    assert protocol.derive_secret(seed).hex() == "80123456789abcde"


def test_derive_secret_always_starts_with_high_nibble_8() -> None:
    for seed in ("ffffffffffffffff" + "0" * 16, "00000000000000000000000000000000"):
        assert protocol.derive_secret(seed)[0] >> 4 == 0x8


def test_derive_secret_pads_odd_length_seed() -> None:
    # prepareHashForPairing concatenates "0" before truncating to 16 chars,
    # so a 15-char seed is still usable.
    assert len(protocol.derive_secret("0123456789abcde")) == 8


def test_generate_pairing_seed_is_32_hex_chars() -> None:
    for _ in range(200):
        seed = protocol.generate_pairing_seed()
        assert len(seed) == 32
        int(seed, 16)  # must parse as hex
        assert len(protocol.derive_secret(seed)) == 8


def test_decode_machine_status_all_flags_clear() -> None:
    # byte0 = 0, byte1 = 0x02 -> state 2 (READY), byte2 = 0
    status = protocol.decode_machine_status(bytes([0x00, 0x02, 0x00]))
    assert status.state is protocol.MachineState.READY
    assert status.pairing_key_state is protocol.PairingKeyState.NONE
    assert not status.water_tank_empty
    assert not status.descaling_needed
    assert not status.brewing_unit_closed


def test_decode_machine_status_flags_and_high_state() -> None:
    # byte0: bit7 bootloader, bits6-5 = 2 (FINAL), bit4 error, bit2 descaling,
    #        bit1 cleaning, bit0 tank empty  -> 0x80|0x40|0x10|0x04|0x02|0x01
    b0 = 0x80 | 0x40 | 0x10 | 0x04 | 0x02 | 0x01
    # byte1: bit7 unit closed, bit6 container full, low nibble 3
    b1 = 0x80 | 0x40 | 0x03
    # byte2 high nibble 2 -> state = 3 + 0x20 = 35 (READY_OLD_CAPSULE)
    b2 = 0x20
    status = protocol.decode_machine_status(bytes([b0, b1, b2]))

    assert status.state is protocol.MachineState.READY_OLD_CAPSULE
    assert status.pairing_key_state is protocol.PairingKeyState.FINAL
    assert status.bootloader_active
    assert status.error_present
    assert status.descaling_needed
    assert status.cleaning_needed
    assert status.water_tank_empty
    assert status.brewing_unit_closed
    assert status.capsule_container_full
    assert not status.milk_frother_running


def test_decode_machine_status_pads_short_payload() -> None:
    status = protocol.decode_machine_status(bytes([0x01]))
    assert status.water_tank_empty
    assert status.state is protocol.MachineState.FACTORY_RESET
    assert len(status.raw) == 8


def test_decode_machine_status_unknown_state_does_not_raise() -> None:
    # low nibble 15 + high nibble 0xF0 -> 255+... not a defined state
    status = protocol.decode_machine_status(bytes([0x00, 0x0F, 0xF0]))
    assert status.state is protocol.MachineState.UNKNOWN


def test_decode_machine_info() -> None:
    payload = bytes.fromhex(
        "0065"  # hw 101 -> 1.1
        "00c8"  # bootloader 200 -> 2.0
        "01f4"  # firmware 500 -> 5.0
        "000a"  # recipe db 10 -> 0.10
        "3039"  # connectivity 12345 -> 1.23.45
        "aabbccddeeff"  # MAC
    )
    info = protocol.decode_machine_info(payload)
    assert info.hardware_version == "1.1"
    assert info.bootloader_version == "2.0"
    assert info.firmware_version == "5.0"
    assert info.recipe_database_version == "0.10"
    assert info.connectivity_firmware_version == "1.23.45"
    assert info.device_address == "aa:bb:cc:dd:ee:ff"


def test_user_settings_roundtrip() -> None:
    raw = bytes([0x2C, 0x01, 0x03, 0x09])  # APO 300 (LE), hardness 3, standby 9
    settings = protocol.decode_user_settings(raw)
    assert settings.auto_power_off_time == 300
    assert settings.water_hardness == 3
    assert settings.standby_time == 9
    assert protocol.encode_user_settings(settings) == raw


def test_model_from_name() -> None:
    assert protocol.model_from_name("CV2_123456") == "Vertuo Pop"
    assert protocol.model_from_name("DV2ABCDEF") == "Vertuo Pop"
    assert protocol.model_from_name("CV6_9") == "Vertuo Pop+"
    assert protocol.model_from_name("W10_1") is None
    assert protocol.model_from_name(None) is None


def test_is_vertuo_name() -> None:
    assert protocol.is_vertuo_name("CV2_123456")
    assert protocol.is_vertuo_name("Venus_abc")
    assert not protocol.is_vertuo_name("MC1_x")  # VertuoUp, different profile
    assert not protocol.is_vertuo_name("")


def test_decode_pairing_key_state() -> None:
    assert protocol.decode_pairing_key_state(b"\x00") is protocol.PairingKeyState.NONE
    assert protocol.decode_pairing_key_state(b"\x02") is protocol.PairingKeyState.FINAL
    assert protocol.decode_pairing_key_state(b"") is protocol.PairingKeyState.UNKNOWN
    assert (
        protocol.decode_pairing_key_state(b"\x63") is protocol.PairingKeyState.UNKNOWN
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print("\nall passed" if not failures else f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
