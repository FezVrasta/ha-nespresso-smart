"""Constants for the Nespresso Smart BLE integration.

UUIDs and name prefixes are taken from the vendor SDK shipped in the official
Android app (com.sdataway.vertuonext). See PROTOCOL.md for provenance.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "nespresso_smart"

CONF_PAIRING_SEED: Final = "pairing_seed"

# --- GATT services -------------------------------------------------------

SVC_IDENTITY: Final = "06aa1910-f22a-11e3-9daa-0002a5d5c51b"
SVC_STATE: Final = "06aa1920-f22a-11e3-9daa-0002a5d5c51b"
SVC_ERROR: Final = "06aa1930-f22a-11e3-9daa-0002a5d5c51b"
SVC_SETTINGS: Final = "06aa1940-f22a-11e3-9daa-0002a5d5c51b"
SVC_WIFI: Final = "06aa1990-f22a-11e3-9daa-0002a5d5c51b"

# --- GATT characteristics ------------------------------------------------

CHAR_PROFILE_VERSION: Final = "06aa3a11-f22a-11e3-9daa-0002a5d5c51b"
CHAR_MACHINE_INFO: Final = "06aa3a21-f22a-11e3-9daa-0002a5d5c51b"
CHAR_SERIAL_NUMBER: Final = "06aa3a31-f22a-11e3-9daa-0002a5d5c51b"
CHAR_CMID: Final = "06aa3a41-f22a-11e3-9daa-0002a5d5c51b"
CHAR_CMID_TYPE: Final = "06aa3a51-f22a-11e3-9daa-0002a5d5c51b"
CHAR_TX_LEVEL: Final = "06aa3a61-f22a-11e3-9daa-0002a5d5c51b"

CHAR_MACHINE_STATUS: Final = "06aa3a12-f22a-11e3-9daa-0002a5d5c51b"
CHAR_MACHINE_PARAMS: Final = "06aa3a22-f22a-11e3-9daa-0002a5d5c51b"

CHAR_ERROR_INFORMATION: Final = "06aa3a23-f22a-11e3-9daa-0002a5d5c51b"

CHAR_USER_SETTINGS: Final = "06aa3a44-f22a-11e3-9daa-0002a5d5c51b"

# --- Discovery -----------------------------------------------------------
# Name prefixes and model names live in protocol.py so that module stays
# importable without Home Assistant; re-exported here for convenience.

from .protocol import MODEL_NAMES, VERTUO_NAME_PREFIXES  # noqa: E402,F401

DEFAULT_SCAN_INTERVAL: Final = 60
