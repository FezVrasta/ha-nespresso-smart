"""The Nespresso Smart BLE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_PAIRING_SEED
from .coordinator import VertuoCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SENSOR,
]

type VertuoConfigEntry = ConfigEntry[VertuoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VertuoConfigEntry) -> bool:
    """Set up a Vertuo machine from a config entry."""
    coordinator = VertuoCoordinator(
        hass,
        entry,
        address=entry.data[CONF_ADDRESS],
        seed=entry.data[CONF_PAIRING_SEED],
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VertuoConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
