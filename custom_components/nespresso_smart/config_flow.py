"""Config flow for Nespresso Smart."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import CONF_PAIRING_SEED, DOMAIN, SVC_IDENTITY
from .device import VertuoDevice
from .protocol import (
    BOUND_STATES,
    PairingKeyState,
    generate_pairing_seed,
    is_vertuo_name,
    model_from_name,
)

_LOGGER = logging.getLogger(__name__)


def _is_vertuo(info: BluetoothServiceInfoBleak) -> bool:
    """Whether a discovered device is a Vertuo machine.

    Prefer the advertised service UUID over the local name: the machine does
    not put its name in every advertisement, and when the name is absent HA
    falls back to the MAC address, which no name-based check can match.
    """
    if SVC_IDENTITY in {str(u).lower() for u in (info.service_uuids or ())}:
        return True
    return is_vertuo_name(info.name)


class VertuoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setting up a Vertuo machine."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, str] = {}

    # --- entry points ----------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a machine discovered by HA's Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not _is_vertuo(discovery_info):
            return self.async_abort(reason="not_supported")

        self._address = discovery_info.address
        self._name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_pair()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a manually started flow: pick from discovered machines."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = self._discovered.get(self._address)
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_pair()

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not _is_vertuo(info):
                continue
            self._discovered[info.address] = info.name or info.address

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{name} ({model_from_name(name) or 'Vertuo'})"
                            for address, name in self._discovered.items()
                        }
                    )
                }
            ),
        )

    # --- pairing ---------------------------------------------------------

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bind to the machine, or authenticate with an existing seed.

        A Vertuo stores exactly one pairing key. If the phone app already
        claimed the machine, the only way in is to factory-reset it (which
        unpairs the app) or to reuse the app's own seed.
        """
        assert self._address is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            seed = (user_input.get(CONF_PAIRING_SEED) or "").strip().lower()
            if seed:
                if len(seed) < 15 or any(c not in "0123456789abcdef" for c in seed):
                    errors["base"] = "invalid_seed"
                else:
                    result = await self._try_seed(seed)
                    if result is None:
                        return self._create_entry(seed)
                    errors["base"] = result
            else:
                seed = generate_pairing_seed()
                result = await self._try_onboard(seed)
                if result is None:
                    return self._create_entry(seed)
                errors["base"] = result

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Optional(CONF_PAIRING_SEED): str}),
            errors=errors,
            description_placeholders={"name": self._name or self._address},
        )

    def _create_entry(self, seed: str) -> ConfigFlowResult:
        assert self._address is not None
        return self.async_create_entry(
            title=self._name or self._address,
            data={CONF_ADDRESS: self._address, CONF_PAIRING_SEED: seed},
        )

    async def _try_seed(self, seed: str) -> str | None:
        """Authenticate with an existing seed. Returns an error key or None."""
        assert self._address is not None
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            return "not_found"

        device = VertuoDevice(ble_device, seed)
        try:
            await device.connect()
            await device.update()
        except Exception as err:
            _LOGGER.debug("Authenticating with supplied seed failed: %s", err)
            return "auth_failed"
        finally:
            await device.disconnect()
        return None

    async def _try_onboard(self, seed: str) -> str | None:
        """Bind an unbound machine. Returns an error key or None."""
        assert self._address is not None
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            return "not_found"

        device = VertuoDevice(ble_device, seed)
        try:
            # Connect without authenticating so the pairing state can be read
            # before anything is written to the machine.
            await device.connect_unauthenticated()
        except Exception as err:
            _LOGGER.debug("Connecting for onboarding failed: %s", err)
            return "cannot_connect"

        try:
            state = await device.read_pairing_state()
            if state in BOUND_STATES:
                return "already_paired"
            if state is PairingKeyState.UNKNOWN:
                return "cannot_connect"

            await device.onboard()
            await device.authenticate()
            await device.update()
        except Exception as err:
            _LOGGER.debug("Onboarding failed: %s", err)
            return "onboard_failed"
        finally:
            await device.disconnect()
        return None
