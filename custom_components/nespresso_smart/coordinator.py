"""Update coordinator for Nespresso Smart machines."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .device import VertuoData, VertuoDevice, VertuoError
from .protocol import MachineStatus

_LOGGER = logging.getLogger(__name__)


class VertuoCoordinator(DataUpdateCoordinator[VertuoData]):
    """Keeps one machine's state fresh.

    The machine pushes MachineStatus notifications while connected, so state
    changes (brewing, tank empty) show up immediately. The periodic poll is a
    backstop that also reconnects after the machine drops the link when it
    goes into standby.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        seed: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{address}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            config_entry=entry,
        )
        self.address = address
        self._seed = seed
        self._device: VertuoDevice | None = None

    def _get_ble_device(self):
        """Ask HA's Bluetooth stack for the current BLEDevice."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"Nespresso Smart {self.address} is not in range of any Bluetooth "
                "adapter or proxy"
            )
        return ble_device

    @callback
    def _handle_status_push(self, status: MachineStatus) -> None:
        """Publish a notification-driven status without a full re-read."""
        if self.data is None:
            return
        self.async_set_updated_data(
            VertuoData(
                status=status,
                info=self.data.info,
                serial=self.data.serial,
                settings=self.data.settings,
            )
        )

    @property
    def device(self) -> VertuoDevice:
        """The BLE device, created on first use and reused thereafter."""
        if self._device is None:
            self._device = VertuoDevice(self._get_ble_device(), self._seed)
            self._device.set_status_callback(self._handle_status_push)
        else:
            self._device.set_ble_device(self._get_ble_device())
        return self._device

    async def _async_update_data(self) -> VertuoData:
        device = self.device
        try:
            data = await device.update()
            await device.start_notifications()
        except VertuoError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with the machine: {err}") from err
        return data

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._device is not None:
            await self._device.disconnect()
            self._device = None
