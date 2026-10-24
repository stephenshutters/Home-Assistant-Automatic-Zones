"""DataUpdateCoordinator for POI Zones."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTO_RADIUS,
    CONF_CITY,
    CONF_DISTINGUISH_DUPLICATES,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PLACE_NAME,
    CONF_POI_TYPE,
    CONF_SEARCH_RADIUS,
    CONF_ZONE_RADIUS,
    DEFAULT_AUTO_RADIUS,
    DEFAULT_DISTINGUISH_DUPLICATES,
    DEFAULT_SEARCH_RADIUS,
    DOMAIN,
    GEOMETRY_BATCH_SIZE,
    POI_CACHE_TTL,
    POI_TYPES,
    RADIUS_ALGORITHM_VERSION,
    RADIUS_CACHE_TTL,
    REQUEST_STAGGER_DELAY,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)
from .geometry import haversine_m
from .helpers import assign_zone_names, entry_value, zone_prefix
from .osm import (
    OSMConnectionError,
    async_overpass,
    build_geometry_query,
    build_poi_query,
    compute_footprints,
)

_LOGGER = logging.getLogger(__name__)

_RATE_KEY = f"{DOMAIN}_overpass_rate"

# Tags worth keeping in the on-disk cache (keeps the cache file small).
_KEEP_TAGS = {
    "name", "phone", "website", "opening_hours", "brand", "operator", "emergency",
    "beds", "healthcare", "cuisine", "capacity", "building",
}
_KEEP_PREFIXES = ("addr:", "contact:", "fuel:", "socket:")


def _rate_state(hass: HomeAssistant) -> dict[str, Any]:
    """Process-wide Overpass lock + timestamp of the previous request."""
    return hass.data.setdefault(_RATE_KEY, {"lock": asyncio.Lock(), "last": 0.0})


def _trim(element: dict[str, Any]) -> dict[str, Any]:
    tags = element.get("tags", {})
    kept = {
        k: v for k, v in tags.items() if k in _KEEP_TAGS or k.startswith(_KEEP_PREFIXES)
    }
    out = {"type": element["type"], "id": element["id"], "tags": kept}
    if "lat" in element:
        out["lat"], out["lon"] = element["lat"], element["lon"]
    if "center" in element:
        out["center"] = element["center"]
    return out


class POIZonesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches POIs from OpenStreetMap and sizes their zones."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.poi_type: str = entry.data[CONF_POI_TYPE]
        self.latitude: float = entry.data[CONF_LATITUDE]
        self.longitude: float = entry.data[CONF_LONGITUDE]
        self.place_name: str = (
            entry.data.get(CONF_PLACE_NAME) or entry.data.get(CONF_CITY) or ""
        )
        self.force_refresh = False
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # pois: raw Overpass elements; radii: footprint results per POI key;
        # zones: POI key -> id of the zone WE created in the zone collection.
        self.cache: dict[str, Any] = {"pois": None, "radii": {}, "zones": {}}
        self._elements: list[dict[str, Any]] = []
        self._enrich_task: asyncio.Task | None = None

    # -- live options -------------------------------------------------------
    @property
    def search_radius_km(self) -> int:
        return int(entry_value(self.config_entry, CONF_SEARCH_RADIUS, DEFAULT_SEARCH_RADIUS))

    @property
    def fallback_radius(self) -> int:
        value = entry_value(self.config_entry, CONF_ZONE_RADIUS, None)
        return int(value) if value else POI_TYPES[self.poi_type]["radius"]

    @property
    def auto_radius(self) -> bool:
        return bool(entry_value(self.config_entry, CONF_AUTO_RADIUS, DEFAULT_AUTO_RADIUS))

    # -- persistence --------------------------------------------------------
    async def async_load_store(self) -> None:
        if stored := await self._store.async_load():
            self.cache.update(stored)
            self.cache.setdefault("radii", {})
            self.cache.setdefault("zones", {})

    async def async_flush_store(self) -> None:
        await self._store.async_save(self.cache)

    def schedule_save(self) -> None:
        self._store.async_delay_save(lambda: self.cache, 5)

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    # -- update -------------------------------------------------------------
    def _signature(self) -> str:
        return (
            f"{self.poi_type}|{self.latitude:.5f}|{self.longitude:.5f}|{self.search_radius_km}"
        )

    async def _rate_limited_overpass(self, query: str) -> list[dict[str, Any]]:
        """Serialise Overpass requests across all entries, with a pause."""
        state = _rate_state(self.hass)
        async with state["lock"]:
            wait = state["last"] + REQUEST_STAGGER_DELAY - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await async_overpass(async_get_clientsession(self.hass), query)
            finally:
                state["last"] = time.monotonic()

    async def _async_update_data(self) -> dict[str, Any]:
        sig = self._signature()
        cached = self.cache.get("pois")
        fresh = (
            cached
            and cached.get("sig") == sig
            and time.time() - cached.get("ts", 0) < POI_CACHE_TTL
            and not self.force_refresh
        )

        if self.force_refresh:
            if self._enrich_task and not self._enrich_task.done():
                self._enrich_task.cancel()
            self.cache["radii"] = {}

        if fresh:
            self._elements = cached["elements"]
        else:
            poi_config = POI_TYPES[self.poi_type]
            query = build_poi_query(
                poi_config["filters"],
                self.latitude,
                self.longitude,
                self.search_radius_km * 1000,
            )
            try:
                raw = await self._rate_limited_overpass(query)
            except OSMConnectionError as err:
                if cached and cached.get("sig") == sig:
                    _LOGGER.warning("Overpass unavailable (%s); using the previous POI list", err)
                    self._elements = cached["elements"]
                else:
                    raise UpdateFailed(f"Could not fetch POIs from OpenStreetMap: {err}") from err
            else:
                self._elements = [_trim(e) for e in raw if "type" in e and "id" in e]
                self.cache["pois"] = {"sig": sig, "ts": time.time(), "elements": self._elements}
                self.schedule_save()

        self.force_refresh = False
        data = self._build_data()
        # Forget footprints of POIs that no longer exist.
        if data["pois"]:
            live = {p["key"] for p in data["pois"]}
            self.cache["radii"] = {k: v for k, v in self.cache["radii"].items() if k in live}
            self.schedule_save()
        self._schedule_enrichment()
        return data

    # -- building the data the rest of the integration uses ----------------
    def _radius_entry(self, key: str) -> dict[str, Any] | None:
        entry = self.cache["radii"].get(key)
        if (
            entry
            and entry.get("v") == RADIUS_ALGORITHM_VERSION
            and time.time() - entry.get("ts", 0) < RADIUS_CACHE_TTL
        ):
            return entry
        return None

    def _build_data(self) -> dict[str, Any]:
        pois = self._process_elements(self._elements)
        pending = 0
        for poi in pois:
            footprint = self._radius_entry(poi["key"]) if self.auto_radius else None
            if footprint and footprint.get("radius"):
                poi["zone_radius"] = footprint["radius"]
                poi["zone_latitude"] = footprint["latitude"]
                poi["zone_longitude"] = footprint["longitude"]
                poi["radius_source"] = "osm_footprint"
                poi["parking_lots"] = footprint.get("parking", 0)
            elif not self.auto_radius:
                poi["radius_source"] = "fixed"
            elif footprint:
                poi["radius_source"] = "default_no_footprint"
            else:
                poi["radius_source"] = "pending"
                pending += 1
        assign_zone_names(
            pois,
            zone_prefix(self.config_entry),
            bool(
                entry_value(
                    self.config_entry, CONF_DISTINGUISH_DUPLICATES, DEFAULT_DISTINGUISH_DUPLICATES
                )
            ),
        )
        return {
            "place": self.place_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "poi_type": self.poi_type,
            "poi_type_name": POI_TYPES[self.poi_type]["name"],
            "pois": pois,
            "pending_radius": pending,
        }

    # -- phase 2: footprint sizing (background) -----------------------------
    def _schedule_enrichment(self) -> None:
        if not self.auto_radius:
            return
        if self._enrich_task and not self._enrich_task.done():
            return
        self._enrich_task = self.config_entry.async_create_background_task(
            self.hass, self._async_enrich(), f"{DOMAIN}_footprints_{self.config_entry.entry_id}"
        )

    async def _async_enrich(self) -> None:
        """Look up building/parking footprints in batches and resize zones."""
        try:
            pending = [
                p["key"] for p in self._process_elements(self._elements)
                if self._radius_entry(p["key"]) is None
            ]
            if not pending:
                return
            _LOGGER.debug("Sizing %d %s zones from OSM footprints", len(pending), self.poi_type)
            default_radius = POI_TYPES[self.poi_type]["radius"]
            failures = 0
            for i in range(0, len(pending), GEOMETRY_BATCH_SIZE):
                batch = pending[i : i + GEOMETRY_BATCH_SIZE]
                try:
                    elements = await self._rate_limited_overpass(build_geometry_query(batch))
                except OSMConnectionError as err:
                    failures += 1
                    _LOGGER.warning("Footprint lookup failed (%s); will retry on next refresh", err)
                    if failures >= 2:
                        break
                    continue
                results = await self.hass.async_add_executor_job(
                    compute_footprints, elements, batch, default_radius
                )
                now = time.time()
                for key in batch:
                    entry: dict[str, Any] = {"v": RADIUS_ALGORITHM_VERSION, "ts": now}
                    entry.update(results.get(key) or {"radius": None})
                    self.cache["radii"][key] = entry
                self.schedule_save()
                self.async_set_updated_data(self._build_data())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background task must never crash HA
            _LOGGER.exception("Unexpected error while sizing zones from footprints")

    # -- phase 1 processing -------------------------------------------------
    def _process_elements(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        poi_config = POI_TYPES[self.poi_type]
        pois: list[dict[str, Any]] = []
        for element in elements:
            if element["type"] == "node":
                lat, lon = element.get("lat"), element.get("lon")
            else:
                center = element.get("center") or {}
                lat, lon = center.get("lat"), center.get("lon")
            if lat is None or lon is None:
                continue
            tags = element.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name:  # unnamed POIs make useless zones
                continue

            if self.poi_type in ("hospital", "urgent_care"):
                radius = self._calculate_hospital_radius(element, tags)
            else:
                radius = self.fallback_radius

            poi: dict[str, Any] = {
                "key": f"{element['type']}/{element['id']}",
                "osm_type": element["type"],
                "osm_id": element["id"],
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "poi_type": self.poi_type,
                "poi_type_name": poi_config["name"],
                "icon": poi_config["icon"],
                "zone_radius": radius,
                "radius_source": "fixed",
            }
            if street := tags.get("addr:street"):
                poi["street"] = street
            if addr := self._build_address(tags):
                poi["address"] = addr
            if phone := tags.get("phone") or tags.get("contact:phone"):
                poi["phone"] = phone
            if website := tags.get("website") or tags.get("contact:website"):
                poi["website"] = website
            for tag in ("opening_hours", "brand", "operator"):
                if value := tags.get(tag):
                    poi[tag] = value
            if self.poi_type in ("hospital", "urgent_care"):
                poi["emergency"] = tags.get("emergency") == "yes"
            if self.poi_type == "ev_charging":
                poi["capacity"] = tags.get("capacity")
            if self.poi_type in ("restaurant", "fast_food", "cafe"):
                poi["cuisine"] = tags.get("cuisine")
            if self.poi_type == "gas_station":
                poi["fuel_types"] = self._get_fuel_types(tags)
            pois.append(poi)

        pois = self._dedupe(pois)
        pois.sort(key=lambda p: (p["name"].casefold(), p["key"]))
        return pois

    @staticmethod
    def _dedupe(pois: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the same facility mapped twice (e.g. a node inside its own way).

        The original rounded coordinates to ~11 m and kept the first hit, which
        also discarded *different* businesses that sit side by side. Here two
        POIs only collapse if they have the same name and are within 75 m;
        the area (way/relation) wins over the point.
        """
        by_name: dict[str, list[dict[str, Any]]] = {}
        for poi in sorted(pois, key=lambda p: p["osm_type"] == "node"):
            kept = by_name.setdefault(poi["name"].casefold(), [])
            if any(
                haversine_m((k["latitude"], k["longitude"]), (poi["latitude"], poi["longitude"])) < 75
                for k in kept
            ):
                continue
            kept.append(poi)
        return [p for group in by_name.values() for p in group]

    @staticmethod
    def _build_address(tags: dict[str, str]) -> str | None:
        parts = []
        if street := tags.get("addr:street"):
            number = tags.get("addr:housenumber")
            parts.append(f"{number} {street}" if number else street)
        for key in ("addr:city", "addr:state", "addr:postcode"):
            if value := tags.get(key):
                parts.append(value)
        return ", ".join(parts) if parts else None

    @staticmethod
    def _get_fuel_types(tags: dict[str, str]) -> list[str]:
        mapping = {
            "fuel:diesel": "Diesel",
            "fuel:octane_87": "Regular",
            "fuel:octane_89": "Mid-Grade",
            "fuel:octane_91": "Premium",
            "fuel:octane_93": "Premium",
            "fuel:e85": "E85",
        }
        return [name for tag, name in mapping.items() if tags.get(tag) == "yes"]

    def _calculate_hospital_radius(self, element: dict[str, Any], tags: dict[str, str]) -> int:
        """Heuristic size for hospitals/urgent care when no footprint is known.

        Unchanged from the original integration; it is now only the fallback
        for when OSM has no building outline for the facility.
        """
        base_radius = self.fallback_radius
        buffer = 30
        if beds_str := tags.get("beds"):
            try:
                beds = int(beds_str)
            except ValueError:
                beds = None
            if beds is not None:
                if beds < 50:
                    return 100 + buffer
                if beds < 150:
                    return 150 + buffer
                if beds < 300:
                    return 200 + buffer
                return 250 + buffer
        if tags.get("healthcare", "") in ("clinic", "doctor", "dentist"):
            return 75 + buffer
        kind = element.get("type")
        if kind == "node":
            return 100 + buffer
        if kind == "relation":
            return min(250 + buffer, base_radius + buffer)
        if kind == "way":
            return (200 if tags.get("emergency") == "yes" else 150) + buffer
        return base_radius + buffer
