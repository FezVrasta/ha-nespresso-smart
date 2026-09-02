"""Shared entity base for Nespresso Smart."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VertuoCoordinator
from .protocol import model_from_name, model_from_serial


class VertuoEntity(CoordinatorEntity[VertuoCoordinator]):
    """Base entity: shares the device registry entry and availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VertuoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        ble_name = self.coordinator.device.name

        # The advertised local name is often absent, in which case bleak reports
        # the MAC address. Fall back to the model code embedded in the serial so
        # the device is called "Vertuo Pop" rather than "AA:BB:CC:DD:EE:FF".
        model = model_from_name(ble_name)
        if model is None and data is not None:
            model = model_from_serial(data.serial)

        name = ble_name
        if model and not model_from_name(ble_name):
            name = f"Nespresso {model}"

        info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, self.coordinator.address)},
            manufacturer="Nespresso",
            model=model or "Vertuo",
            name=name,
        )
        if data is not None:
            if data.serial:
                info["serial_number"] = data.serial
            if data.info is not None:
                info["hw_version"] = data.info.hardware_version
                info["sw_version"] = data.info.firmware_version
        return info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None
