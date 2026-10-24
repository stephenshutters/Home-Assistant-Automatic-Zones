"""Creates, updates and removes the zones owned by one config entry.

Differences from the original integration, all deliberate:

* Zones are tracked by the zone *id* we were handed when we created them
  (persisted in the entry's store), never by name. The original matched on
  name, so a POI called "Walmart" would silently take over - and later
  delete - a zone you made by hand with the same name.
* Zones are updated only when something actually changed.
* Unloading/reloading the entry leaves the zones alone (so options changes do
  not churn entities); they are removed when the entry is deleted.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import zone
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import CONF_PASSIVE, DEFAULT_PASSIVE, DOMAIN, SIGNAL_ZONES_SYNCED, STORAGE_VERSION
from .coordinator import POIZonesCoordinator
from .helpers import entry_value

_LOGGER = logging.getLogger(__name__)

_COORD_TOLERANCE = 1e-6  # degrees (~0.1 m)
_ADOPT_TOLERANCE = 1e-5  # degrees (~1 m)


def get_zone_collection(hass: HomeAssistant) -> Any | None:
    """The zone integration's storage collection, or None if unavailable."""
    key = getattr(zone, "DATA_ZONE_STORAGE_COLLECTION", zone.DOMAIN)
    collection = hass.data.get(key)
    if collection is None or not hasattr(collection, "async_create_item"):
        return None
    return collection


def _differs(item: dict[str, Any], wanted: dict[str, Any]) -> bool:
    return (
        item.get("name") != wanted["name"]
        or abs(item.get("latitude", 0) - wanted["latitude"]) > _COORD_TOLERANCE
        or abs(item.get("longitude", 0) - wanted["longitude"]) > _COORD_TOLERANCE
        or abs(float(item.get("radius", 0)) - wanted["radius"]) > 0.01
        or bool(item.get("passive", False)) != wanted["passive"]
        or item.get("icon") != wanted.get("icon")
    )


class ZoneManager:
    """Keeps the zone collection in step with the coordinator's POI list."""

    def __init__(self, hass: HomeAssistant, entry, coordinator: POIZonesCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._lock = asyncio.Lock()

    @property
    def owned(self) -> dict[str, str]:
        """POI key -> zone id, for zones this entry created."""
        return self.coordinator.cache["zones"]

    @callback
    def async_request_sync(self) -> None:
        """Coordinator listener: schedule a sync without blocking the update."""
        self.entry.async_create_background_task(
            self.hass, self.async_sync(), f"{DOMAIN}_sync_{self.entry.entry_id}"
        )

    def entity_id_for(self, key: str) -> str | None:
        """Entity id of the zone for a POI (the registry may add a ``_2``)."""
        if not (zone_id := self.owned.get(key)):
            return None
        return er.async_get(self.hass).async_get_entity_id("zone", "zone", zone_id)

    async def async_sync(self) -> None:
        async with self._lock:
            await self._sync()
        async_dispatcher_send(self.hass, SIGNAL_ZONES_SYNCED.format(self.entry.entry_id))

    async def _sync(self) -> None:
        collection = get_zone_collection(self.hass)
        if collection is None:
            _LOGGER.warning("Zone integration not available; no zones were created")
            return
        data = self.coordinator.data
        if not data:
            return
        pois: list[dict[str, Any]] = data["pois"]
        passive = bool(entry_value(self.entry, CONF_PASSIVE, DEFAULT_PASSIVE))
        owned = self.owned

        items = {item["id"]: item for item in collection.async_items()}
        claimed = set(owned.values())
        created = updated = adopted = removed = 0

        for poi in pois:
            wanted = {
                "name": poi["zone_name"],
                "latitude": poi.get("zone_latitude", poi["latitude"]),
                "longitude": poi.get("zone_longitude", poi["longitude"]),
                "radius": float(poi["zone_radius"]),
                "icon": poi["icon"],
                "passive": passive,
            }
            key = poi["key"]
            zone_id = owned.get(key)
            if zone_id not in items:  # deleted by the user, or never created
                owned.pop(key, None)
                zone_id = None

            if zone_id is None:
                zone_id = self._find_adoptable(items, claimed, poi, wanted)
                if zone_id:
                    owned[key] = zone_id
                    claimed.add(zone_id)
                    adopted += 1

            try:
                if zone_id is None:
                    item = await collection.async_create_item(wanted)
                    owned[key] = item["id"]
                    claimed.add(item["id"])
                    created += 1
                elif _differs(items[zone_id], wanted):
                    await collection.async_update_item(zone_id, wanted)
                    updated += 1
            except Exception as err:  # noqa: BLE001 - one bad zone must not stop the rest
                _LOGGER.warning("Could not create/update zone %s: %s", wanted["name"], err)

        # Zones for POIs that disappeared from OSM. Never mass-delete on an
        # empty list: that is far more likely a failed lookup than a real
        # result.
        live = {p["key"] for p in pois}
        if pois:
            for key in [k for k in owned if k not in live]:
                if await self._delete(collection, owned.pop(key)):
                    removed += 1

        self.coordinator.schedule_save()
        if created or updated or adopted or removed:
            _LOGGER.info(
                "%s (%s): %d created, %d updated, %d adopted, %d removed",
                data["poi_type_name"], data["place"], created, updated, adopted, removed,
            )

    @staticmethod
    def _find_adoptable(
        items: dict[str, dict[str, Any]],
        claimed: set[str],
        poi: dict[str, Any],
        wanted: dict[str, Any],
    ) -> str | None:
        """Re-claim a zone made by the original integration (or a lost store).

        Only an unclaimed zone with the same name AND the same position
        qualifies, so a hand-made zone that merely shares a name is left alone.
        """
        for zone_id, item in items.items():
            if zone_id in claimed or item.get("name") != wanted["name"]:
                continue
            for lat, lon in (
                (poi["latitude"], poi["longitude"]),
                (wanted["latitude"], wanted["longitude"]),
            ):
                if (
                    abs(item.get("latitude", 0) - lat) < _ADOPT_TOLERANCE
                    and abs(item.get("longitude", 0) - lon) < _ADOPT_TOLERANCE
                ):
                    return zone_id
        return None

    @staticmethod
    async def _delete(collection: Any, zone_id: str) -> bool:
        try:
            await collection.async_delete_item(zone_id)
        except Exception:  # noqa: BLE001 - already gone
            _LOGGER.debug("Zone %s already removed", zone_id)
            return False
        return True


async def async_remove_owned_zones(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every zone an entry created, then its store (entry removed)."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
    stored = await store.async_load() or {}
    collection = get_zone_collection(hass)
    if collection is not None:
        for zone_id in (stored.get("zones") or {}).values():
            await ZoneManager._delete(collection, zone_id)
    await store.async_remove()
