"""POI Zones integration for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_AUTO_RADIUS,
    CONF_CITY,
    CONF_DISTINGUISH_DUPLICATES,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PASSIVE,
    CONF_PLACE_NAME,
    DOMAIN,
)
from .coordinator import POIZonesCoordinator
from .osm import OSMConnectionError, async_geocode
from .zones import ZoneManager, async_remove_owned_zones

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class POIZonesData:
    """Runtime objects of one config entry."""

    coordinator: POIZonesCoordinator
    zones: ZoneManager


type POIZonesConfigEntry = ConfigEntry[POIZonesData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the refresh service once for the whole integration."""

    async def handle_refresh(call: ServiceCall) -> None:
        """Re-query OpenStreetMap now, ignoring all caches."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator = entry.runtime_data.coordinator
            coordinator.force_refresh = True
            await coordinator.async_refresh()

    hass.services.async_register(DOMAIN, "refresh", handle_refresh, schema=vol.Schema({}))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade entries created by the original integration (version 1).

    They keep behaving as before: active zones, fixed radius. Coordinates are
    looked up on the first start (see ``_async_ensure_coordinates``).
    """
    if entry.version == 1:
        data = {**entry.data, CONF_PASSIVE: False, CONF_AUTO_RADIUS: False, CONF_DISTINGUISH_DUPLICATES: False}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated POI Zones entry %s to version 2", entry.title)
    return True


async def _async_ensure_coordinates(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Geocode once for migrated entries that only stored a city string."""
    if CONF_LATITUDE in entry.data and CONF_LONGITUDE in entry.data:
        return
    try:
        places = await async_geocode(
            async_get_clientsession(hass),
            query=entry.data[CONF_CITY],
            language=hass.config.language,
            limit=1,
        )
    except OSMConnectionError as err:
        raise ConfigEntryNotReady(f"Could not geocode {entry.data[CONF_CITY]}: {err}") from err
    if not places:
        raise ConfigEntryNotReady(f"No geocoding result for {entry.data[CONF_CITY]}")
    place = places[0]
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_LATITUDE: place.latitude,
            CONF_LONGITUDE: place.longitude,
            CONF_PLACE_NAME: place.short_name,
        },
    )


async def async_setup_entry(hass: HomeAssistant, entry: POIZonesConfigEntry) -> bool:
    """Set up POI Zones from a config entry."""
    await _async_ensure_coordinates(hass, entry)

    coordinator = POIZonesCoordinator(hass, entry)
    await coordinator.async_load_store()
    await coordinator.async_config_entry_first_refresh()

    manager = ZoneManager(hass, entry, coordinator)
    entry.runtime_data = POIZonesData(coordinator, manager)
    # Register the listener BEFORE the first sync, with no await in between:
    # the background footprint task starts during the first refresh and may
    # publish new radii at any later await. Syncs are serialised by a lock.
    entry.async_on_unload(coordinator.async_add_listener(manager.async_request_sync))
    await manager.async_sync()  # zones exist before the sensor reports them
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: POIZonesConfigEntry) -> bool:
    """Unload. Zones are deliberately kept; see ``async_remove_entry``."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Write the cache now so a pending delayed save cannot resurrect the
        # store file after the entry is deleted.
        await entry.runtime_data.coordinator.async_flush_store()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry was deleted: remove the zones it created."""
    await async_remove_owned_zones(hass, entry.entry_id)
