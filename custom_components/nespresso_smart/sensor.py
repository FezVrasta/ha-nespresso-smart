"""Sensor platform for Nespresso Smart."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VertuoConfigEntry
from .device import VertuoData
from .entity import VertuoEntity
from .protocol import MachineState


@dataclass(frozen=True, kw_only=True)
class VertuoSensorDescription(SensorEntityDescription):
    """Describes a Vertuo sensor."""

    value_fn: Callable[[VertuoData], str | int | None]


def _water_tank(data: VertuoData) -> str:
    empty = data.status.water_tank_empty or data.status.state is MachineState.TANK_EMPTY
    return "empty" if empty else "ok"


def _error(data: VertuoData) -> str:
    present = (
        data.status.error_present or data.status.state is MachineState.DEVICE_ERROR
    )
    return "present" if present else "none"


SENSORS: tuple[VertuoSensorDescription, ...] = (
    VertuoSensorDescription(
        key="machine_state",
        translation_key="machine_state",
        device_class=SensorDeviceClass.ENUM,
        options=[state.name.lower() for state in MachineState],
        value_fn=lambda data: data.status.state.name.lower(),
    ),
    VertuoSensorDescription(
        key="water_tank",
        translation_key="water_tank",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "empty"],
        value_fn=_water_tank,
    ),
    VertuoSensorDescription(
        key="capsule_container",
        translation_key="capsule_container",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "full"],
        value_fn=lambda data: "full" if data.status.capsule_container_full else "ok",
    ),
    VertuoSensorDescription(
        key="descaling",
        translation_key="descaling",
        device_class=SensorDeviceClass.ENUM,
        options=["not_needed", "needed"],
        value_fn=lambda data: (
            "needed" if data.status.descaling_needed else "not_needed"
        ),
    ),
    VertuoSensorDescription(
        key="cleaning",
        translation_key="cleaning",
        device_class=SensorDeviceClass.ENUM,
        options=["not_needed", "needed"],
        value_fn=lambda data: "needed" if data.status.cleaning_needed else "not_needed",
    ),
    VertuoSensorDescription(
        key="error",
        translation_key="error",
        device_class=SensorDeviceClass.ENUM,
        options=["none", "present"],
        value_fn=_error,
    ),
    VertuoSensorDescription(
        key="brewing_unit",
        translation_key="brewing_unit",
        device_class=SensorDeviceClass.ENUM,
        options=["closed", "open"],
        value_fn=lambda data: "closed" if data.status.brewing_unit_closed else "open",
    ),
    VertuoSensorDescription(
        key="water_hardness",
        translation_key="water_hardness",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.settings.water_hardness if data.settings is not None else None
        ),
    ),
    VertuoSensorDescription(
        key="serial_number",
        translation_key="serial_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.serial,
    ),
    VertuoSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            data.info.firmware_version if data.info is not None else None
        ),
    ),
    VertuoSensorDescription(
        key="connectivity_firmware_version",
        translation_key="connectivity_firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            data.info.connectivity_firmware_version if data.info is not None else None
        ),
    ),
    VertuoSensorDescription(
        key="recipe_database_version",
        translation_key="recipe_database_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            data.info.recipe_database_version if data.info is not None else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VertuoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Vertuo sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        VertuoSensor(coordinator, description) for description in SENSORS
    )


class VertuoSensor(VertuoEntity, SensorEntity):
    """A read-only value from the machine."""

    entity_description: VertuoSensorDescription

    def __init__(self, coordinator, description: VertuoSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
