"""Binary sensor platform for Nespresso Smart.

Only genuinely binary, well-labelled things live here. The machine's condition
readouts (water tank, capsule container, descaling, cleaning, error, brewing
unit) are enum sensors instead, so they can say "Empty" and "Needed" rather
than the generic "Problem" that device_class problem would force.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VertuoConfigEntry
from .device import VertuoData
from .entity import VertuoEntity
from .protocol import MachineState

#: States in which the machine is actively producing a drink.
_BREWING_STATES = frozenset({MachineState.BREWING, MachineState.CAPSULE_READING})


@dataclass(frozen=True, kw_only=True)
class VertuoBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Vertuo binary sensor."""

    value_fn: Callable[[VertuoData], bool]


BINARY_SENSORS: tuple[VertuoBinarySensorDescription, ...] = (
    VertuoBinarySensorDescription(
        key="brewing",
        translation_key="brewing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.status.state in _BREWING_STATES,
    ),
    VertuoBinarySensorDescription(
        key="milk_frother_running",
        translation_key="milk_frother_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.status.milk_frother_running,
    ),
    VertuoBinarySensorDescription(
        key="bootloader_active",
        translation_key="bootloader_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.status.bootloader_active,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VertuoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Vertuo binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        VertuoBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class VertuoBinarySensor(VertuoEntity, BinarySensorEntity):
    """A boolean flag from the MachineStatus bitfield."""

    entity_description: VertuoBinarySensorDescription

    def __init__(self, coordinator, description: VertuoBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
