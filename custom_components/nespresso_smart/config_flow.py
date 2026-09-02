"""Config flow for Nespresso Smart."""

from __future__ import annotations

import asyncio
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
from .device import VertuoDevice, VertuoNotBondedError, VertuoTimeoutError
from .protocol import (
    BOUND_STATES,
    PairingKeyState,
    generate_pairing_seed,
    is_vertuo_name,
    model_from_name,
)

_LOGGER = logging.getLogger(__name__)

#: Overall deadline for one pairing attempt, covering connect, bond and every
#: read and write. The individual operations are bounded too; this is the
#: backstop that guarantees the form always comes back with an answer rather
#: than leaving the user staring at a spinner.
PAIRING_TIMEOUT = 90.0


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
            async with asyncio.timeout(PAIRING_TIMEOUT):
                return await self._authenticate_with_seed(device)
        except (TimeoutError, VertuoTimeoutError) as err:
            _LOGGER.debug("Authenticating with supplied seed timed out: %s", err)
            return "timeout"
        finally:
            await device.disconnect()

    @staticmethod
    async def _authenticate_with_seed(device: VertuoDevice) -> str | None:
        """Connect and read once, retrying a refused-because-unbonded write.

        On BlueZ the first attempt against a machine the host has not bonded
        with is *expected* to fail: the pairing-key write comes back "not
        paired", and it is that refusal which prompts the bond. The
        coordinator gets this for free from Home Assistant's setup retry; the
        config flow only ever tries once, so it has to retry here or it would
        reject a perfectly good seed.
        """
        for attempt in (1, 2):
            try:
                await device.connect()
                await device.update()
                return None
            except VertuoNotBondedError as err:
                _LOGGER.debug("Seed attempt %d needs a bond first: %s", attempt, err)
                await device.disconnect()
            except (TimeoutError, VertuoTimeoutError):
                raise  # reported as "timeout" by the caller
            except Exception as err:
                _LOGGER.debug("Authenticating with supplied seed failed: %s", err)
                return "auth_failed"
        return "not_bonded"

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
            # A deadline for the whole attempt, on top of the per-operation
            # ones in VertuoDevice: whatever goes wrong, the form comes back.
            async with asyncio.timeout(PAIRING_TIMEOUT):
                # Same expected first-attempt bond refusal as _try_seed.
                for attempt in (1, 2):
                    result = await self._onboard(device, attempt)
                    if result != "not_bonded":
                        return result
                    await device.disconnect()
                return "not_bonded"
        except (TimeoutError, VertuoTimeoutError) as err:
            _LOGGER.debug("Onboarding timed out: %s", err)
            return "timeout"
        finally:
            await device.disconnect()

    @staticmethod
    async def _onboard(device: VertuoDevice, attempt: int) -> str | None:
        """Run the onboarding sequence once. Returns an error key or None."""
        try:
            # Connect without authenticating so the pairing state can be read
            # before anything is written to the machine.
            await device.connect_unauthenticated()
        except (TimeoutError, VertuoTimeoutError):
            raise  # reported as "timeout" by the caller
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
        except VertuoNotBondedError as err:
            _LOGGER.debug("Onboard attempt %d needs a bond first: %s", attempt, err)
            return "not_bonded"
        except (TimeoutError, VertuoTimeoutError):
            raise  # reported as "timeout" by the caller
        except Exception as err:
            _LOGGER.debug("Onboarding failed: %s", err)
            return "onboard_failed"
        return None
