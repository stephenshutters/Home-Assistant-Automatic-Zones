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
import math
from typing import Any

from homeassistant.components import zone
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONF_LABEL,
    CONF_NEW_LABEL,
    CONF_PASSIVE,
    DEFAULT_PASSIVE,
    DOMAIN,
    SIGNAL_ZONES_SYNCED,
    STORAGE_VERSION,
)
from .coordinator import POIZonesCoordinator
from .helpers import PREFIX_SEPARATOR, entry_value

_LOGGER = logging.getLogger(__name__)

_COORD_TOLERANCE = 1e-6  # degrees (~0.1 m)
_ADOPT_TOLERANCE = 1e-5  # degrees (~1 m)
_LABEL_RETRIES = 5  # new zone entities register a moment after the zone is created
_TWIN_MIN_DISTANCE = 50.0  # m; a hand-made zone this close to a place always counts


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111_320
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def _sync_lock(hass: HomeAssistant) -> asyncio.Lock:
    """One lock for every entry, so entries never judge each other's zones mid-write."""
    return hass.data.setdefault(f"{DOMAIN}_sync_lock", asyncio.Lock())


def get_zone_collection(hass: HomeAssistant) -> Any | None:
    """The zone integration's storage collection, or None if unavailable.

    Older Home Assistant keeps the collection itself in ``hass.data["zone"]``;
    newer versions (2026.x) keep a ``ZoneData`` object there that holds it as
    ``storage_collection``. Both are handled.
    """
    data = hass.data.get(zone.DOMAIN)
    for candidate in (data, getattr(data, "storage_collection", None)):
        if candidate is not None and hasattr(candidate, "async_create_item"):
            return candidate
    return None


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
        self._lock = _sync_lock(hass)
        self.twins: dict[str, str] = {}  # place key -> hand-made zone id covering it

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
        """Entity id of the zone for a POI (the registry may add a ``_2``).

        A place already covered by a hand-made zone reports that zone.
        """
        if not (zone_id := self.owned.get(key) or self.twins.get(key)):
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
        everyone = await self._async_all_owned_ids() | claimed
        created = updated = adopted = removed = skipped = 0
        self.twins = {}

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
                zone_id = self._find_adoptable(items, everyone | claimed, poi, wanted)
                if zone_id:
                    owned[key] = zone_id
                    claimed.add(zone_id)
                    adopted += 1

            # A zone you made by hand already covers this place: do not make a
            # second one, and drop ours if it was made before yours existed.
            twin = self._hand_made_twin(items, everyone | claimed, zone_id, wanted)
            if twin:
                self.twins[key] = twin
                skipped += 1
                if zone_id:
                    if await self._delete(collection, zone_id):
                        removed += 1
                    owned.pop(key, None)
                    claimed.discard(zone_id)
                continue

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

        await self._async_apply_label()

        self.coordinator.schedule_save()
        if created or updated or adopted or removed or skipped:
            _LOGGER.info(
                "%s (%s): %d created, %d updated, %d adopted, %d removed, "
                "%d skipped (already covered by a zone you made)",
                data["poi_type_name"], data["place"], created, updated, adopted, removed, skipped,
            )

    def _wanted_label_id(self) -> str | None:
        """Label id from the options: a typed name (created if needed) wins."""
        registry = lr.async_get(self.hass)
        new_name = str(entry_value(self.entry, CONF_NEW_LABEL, "") or "").strip()
        if new_name:
            if existing := registry.async_get_label_by_name(new_name):
                return existing.label_id
            return registry.async_create(new_name).label_id
        label_id = str(entry_value(self.entry, CONF_LABEL, "") or "")
        if label_id and registry.async_get_label(label_id):
            return label_id
        return None

    async def _async_apply_label(self) -> None:
        """Put the chosen label on every owned zone; take back a label we added.

        Only the label this entry added earlier is ever removed (it is
        remembered in the cache); labels you add by hand are never touched. A
        label removed by hand from one zone is put back on the next sync.
        """
        wanted = self._wanted_label_id()
        previous = self.coordinator.cache.get("label")
        if wanted is None and previous is None:
            return
        ent_reg = er.async_get(self.hass)
        pending = list(self.owned)
        for attempt in range(_LABEL_RETRIES):
            missing = []
            for key in pending:
                entity_id = self.entity_id_for(key)
                reg_entry = ent_reg.async_get(entity_id) if entity_id else None
                if reg_entry is None:
                    missing.append(key)
                    continue
                labels = set(reg_entry.labels)
                new_labels = set(labels)
                if previous and previous != wanted:
                    new_labels.discard(previous)
                if wanted:
                    new_labels.add(wanted)
                if new_labels != labels:
                    ent_reg.async_update_entity(entity_id, labels=new_labels)
            pending = missing
            if not pending or attempt == _LABEL_RETRIES - 1:
                break
            await asyncio.sleep(1)
        if pending:
            _LOGGER.debug("No entity yet for %d zone(s); label applied on next sync", len(pending))
        self.coordinator.cache["label"] = wanted

    async def _async_all_owned_ids(self) -> set[str]:
        """Zone ids created by ANY Automatic Zones entry (loaded or not yet loaded)."""
        ids: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            if runtime is not None:
                ids.update(runtime.coordinator.cache["zones"].values())
                continue
            store: Store[dict[str, Any]] = Store(self.hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
            ids.update(((await store.async_load()) or {}).get("zones", {}).values())
        return ids

    @staticmethod
    def _hand_made_twin(
        items: dict[str, dict[str, Any]],
        owned_by_us: set[str],
        own_zone_id: str | None,
        wanted: dict[str, Any],
    ) -> str | None:
        """Id of a zone not made by this integration that covers the same place.

        "Covers" means the centres are closer than the smaller of the two radii
        (at least 50 m), so a neighbour's small zone across the street does not
        count, but a zone you drew around the same store does - whatever it is
        called.
        """
        for zone_id, item in items.items():
            if zone_id in owned_by_us or zone_id == own_zone_id:
                continue
            reach = max(min(float(item.get("radius", 0)), wanted["radius"]), _TWIN_MIN_DISTANCE)
            if _distance_m(
                item.get("latitude", 0), item.get("longitude", 0),
                wanted["latitude"], wanted["longitude"],
            ) <= reach:
                return zone_id
        return None

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
            # The original integration named prefixed zones "prefix_Name"; this
            # one uses "prefix - Name". Both count as "the same name" here.
            legacy = wanted["name"].replace(PREFIX_SEPARATOR, "_", 1)
            if zone_id in claimed or item.get("name") not in (wanted["name"], legacy):
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
