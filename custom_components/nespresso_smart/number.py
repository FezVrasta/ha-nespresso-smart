"""Number platform for Nespresso Smart (water hardness setting)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VertuoConfigEntry
from .device import VertuoError
from .entity import VertuoEntity

WATER_HARDNESS = NumberEntityDescription(
    key="water_hardness",
    translation_key="water_hardness",
    entity_category=EntityCategory.CONFIG,
    native_min_value=0,
    native_max_value=4,
    native_step=1,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VertuoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the water hardness control."""
    async_add_entities([VertuoWaterHardness(entry.runtime_data, WATER_HARDNESS)])


class VertuoWaterHardness(VertuoEntity, NumberEntity):
    """Water hardness level, 0 (soft) to 4 (very hard)."""

    def __init__(self, coordinator, description: NumberEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.settings is not None
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.settings is None:
            return None
        return data.settings.water_hardness

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.device.set_water_hardness(int(value))
        except (VertuoError, ValueError) as err:
            raise HomeAssistantError(f"Could not set water hardness: {err}") from err
        await self.coordinator.async_request_refresh()
