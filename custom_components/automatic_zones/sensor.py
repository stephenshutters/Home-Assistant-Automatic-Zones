"""Sensor platform for Automatic Zones."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_AUTO_RADIUS,
    CONF_PASSIVE,
    CONF_PLACE_NAME,
    CONF_POI_TYPE,
    DEFAULT_AUTO_RADIUS,
    DEFAULT_PASSIVE,
    POI_TYPES,
    SIGNAL_ZONES_SYNCED,
)
from .coordinator import POIZonesCoordinator
from .helpers import entry_value, slugify
from .zones import ZoneManager

_LOCATION_FIELDS = (
    "address", "phone", "website", "opening_hours", "brand", "operator",
    "emergency", "cuisine", "capacity", "fuel_types",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities([POISensor(data.coordinator, data.zones, entry)])


class POISensor(CoordinatorEntity[POIZonesCoordinator], SensorEntity):
    """Number of POIs found, with every POI and its zone in attributes."""

    # Hundreds of POIs exceed the recorder's 16 KB attribute limit, which makes
    # it drop ALL of this sensor's attributes from history. They are only
    # useful live (templates/automations), so keep them out of the database.
    _unrecorded_attributes = frozenset({"locations", "zone_ids", "zone_entity_ids", "zone_names"})

    def __init__(self, coordinator: POIZonesCoordinator, zones: ZoneManager, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._zones = zones
        self._entry = entry
        poi_config = POI_TYPES[entry.data[CONF_POI_TYPE]]
        self._attr_name = f"{poi_config['name']} near {entry.data.get(CONF_PLACE_NAME, '')}".strip()
        self._attr_unique_id = f"{entry.entry_id}_sensor"
        self._attr_icon = poi_config["icon"]
        self._attr_native_unit_of_measurement = "locations"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Zone entity ids only exist once the zones have been synced.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ZONES_SYNCED.format(self._entry.entry_id),
                self._handle_zones_synced,
            )
        )

    @callback
    def _handle_zones_synced(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data["pois"]) if self.coordinator.data else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}

        locations = []
        zone_entity_ids = []
        for poi in data["pois"]:
            # The registry decides the real entity id (it appends _2, _3 ... on
            # clashes), so ask it instead of guessing from the name.
            entity_id = self._zones.entity_id_for(poi["key"]) or f"zone.{slugify(poi['zone_name'])}"
            zone_entity_ids.append(entity_id)
            location = {
                "name": poi["name"],
                "zone_name": poi["zone_name"],
                "zone_entity_id": entity_id,
                "zone_id": entity_id.removeprefix("zone."),
                "latitude": poi["latitude"],
                "longitude": poi["longitude"],
                "zone_latitude": poi.get("zone_latitude", poi["latitude"]),
                "zone_longitude": poi.get("zone_longitude", poi["longitude"]),
                "zone_radius_m": poi["zone_radius"],
                "radius_source": poi["radius_source"],
                "osm_id": poi["key"],
            }
            location.update({f: poi[f] for f in _LOCATION_FIELDS if f in poi})
            locations.append(location)

        return {
            "city": data["place"],  # kept for compatibility with the original
            "place": data["place"],
            "center_latitude": data["latitude"],
            "center_longitude": data["longitude"],
            "poi_type": data["poi_type"],
            "poi_type_name": data["poi_type_name"],
            "search_radius_km": self.coordinator.search_radius_km,
            "passive": bool(entry_value(self._entry, CONF_PASSIVE, DEFAULT_PASSIVE)),
            "auto_radius": bool(entry_value(self._entry, CONF_AUTO_RADIUS, DEFAULT_AUTO_RADIUS)),
            "fallback_zone_radius_m": self.coordinator.fallback_radius,
            "zones_sized_from_osm": sum(p["radius_source"] == "osm_footprint" for p in data["pois"]),
            "zones_pending_size": data["pending_radius"],
            "zone_ids": [e.removeprefix("zone.") for e in zone_entity_ids],
            "zone_entity_ids": zone_entity_ids,
            # A person inside an ACTIVE zone has this as their state.
            "zone_names": [p["zone_name"] for p in data["pois"]],
            "locations": locations,
        }
