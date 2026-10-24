"""OpenStreetMap helpers: Nominatim geocoding, Overpass queries and sizing.

The network functions take an ``aiohttp.ClientSession`` so Home Assistant's
shared session can be used. Everything that parses or computes is a plain
function so it can be tested with canned Overpass JSON.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    EXCLUDED_PARKING_ACCESS,
    EXCLUDED_PARKING_TYPES,
    MAX_RADIUS_FACTOR,
    MAX_RADIUS_FLOOR,
    MAX_RETRIES,
    MIN_ZONE_RADIUS,
    NOMINATIM_URL,
    OVERPASS_URLS,
    PARKING_ATTACH_DISTANCE,
    RADIUS_PADDING,
    RADIUS_ROUNDING,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
    USER_AGENT,
)
from .geometry import (
    is_closed_ring,
    min_enclosing_circle,
    point_in_ring,
    project,
    ring_area,
    ring_distance,
    unproject,
)

_LOGGER = logging.getLogger(__name__)

# Overpass sometimes answers HTTP 200 with an empty element list and a
# "remark" when it ran out of time or memory. That must not be mistaken for
# "there are no POIs here".
_OVERPASS_ERROR_MARKERS = ("runtime error", "timed out", "out of memory", "too many")


class OSMError(Exception):
    """Base error for OSM requests."""


class OSMConnectionError(OSMError):
    """Could not reach (or got an error status from) an OSM service."""


# ---------------------------------------------------------------------------
# Nominatim (geocoding)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Place:
    """A geocoded place."""

    latitude: float
    longitude: float
    display_name: str
    short_name: str
    osm_type: str | None
    osm_id: int | None

    @property
    def unique_key(self) -> str:
        """Stable identity for de-duplicating config entries."""
        if self.osm_type and self.osm_id:
            return f"{self.osm_type}{self.osm_id}"
        return f"{self.latitude:.3f}_{self.longitude:.3f}"


def _short_name(result: dict[str, Any], postal: bool) -> str:
    addr = result.get("address", {}) or {}
    locality = next(
        (
            addr[k]
            for k in ("city", "town", "village", "municipality", "hamlet", "suburb", "county")
            if addr.get(k)
        ),
        None,
    )
    region = addr.get("state") or addr.get("region") or addr.get("country")
    if postal and addr.get("postcode"):
        parts = [addr["postcode"], locality, region]
    else:
        parts = [locality or result.get("name") or "", region]
    short = ", ".join(p for p in parts if p)
    if short:
        return short
    return ", ".join(result.get("display_name", "").split(",")[:2]).strip()


async def async_geocode(
    session: aiohttp.ClientSession,
    *,
    country_code: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
    query: str | None = None,
    language: str = "en",
    limit: int = 5,
) -> list[Place]:
    """Geocode with Nominatim's *structured* search.

    Structured fields (city/state/postalcode + countrycodes) are what stop
    "Springfield" from resolving to the wrong state. ``query`` is only used to
    upgrade entries created by the original integration, which stored a single
    free-text city string.
    """
    params: dict[str, Any] = {
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "accept-language": language,
    }
    if query:
        params["q"] = query
    else:
        if city:
            params["city"] = city
        if state:
            params["state"] = state
        if postal_code:
            params["postalcode"] = postal_code
        if country_code:
            params["countrycodes"] = country_code.lower()

    try:
        async with session.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status != 200:
                raise OSMConnectionError(f"Nominatim returned HTTP {response.status}")
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise OSMConnectionError(str(err)) from err

    places: list[Place] = []
    seen: set[str] = set()
    for item in data or []:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        display = item.get("display_name", "")
        if display in seen:
            continue
        seen.add(display)
        places.append(
            Place(
                latitude=lat,
                longitude=lon,
                display_name=display,
                short_name=_short_name(item, postal=bool(postal_code)),
                osm_type=item.get("osm_type"),
                osm_id=item.get("osm_id"),
            )
        )
    return places


# ---------------------------------------------------------------------------
# Overpass (queries)
# ---------------------------------------------------------------------------
def build_poi_query(filters: list[dict[str, str]], lat: float, lon: float, radius_m: int) -> str:
    """Query for every named-or-not POI of a type around a point."""
    parts: list[str] = []
    for f in filters:
        tags = "".join(f'["{k}"="{v}"]' for k, v in f.items())
        for kind in ("node", "way", "relation"):
            parts.append(f"{kind}{tags}(around:{radius_m},{lat},{lon});")
    return f"[out:json][timeout:60];({''.join(parts)});out tags center;"


def build_geometry_query(keys: list[str]) -> str:
    """Query returning, for a batch of POIs, everything needed to size them.

    * the POIs themselves, with geometry
    * the ``building`` polygons that contain any POI that is a plain point
    * ``amenity=parking`` polygons within ``PARKING_ATTACH_DISTANCE`` of any of
      the above (the exact lot-to-site association is decided client-side)
    """
    ids: dict[str, list[str]] = {"node": [], "way": [], "relation": []}
    for key in keys:
        kind, _, ident = key.partition("/")
        ids[kind].append(ident)
    selectors = "".join(
        f"{kind}(id:{','.join(idents)});" for kind, idents in ids.items() if idents
    )
    reach = PARKING_ATTACH_DISTANCE + 5  # small safety margin
    return (
        "[out:json][timeout:90];"
        f"({selectors})->.pois;"
        "node.pois->.pnodes;"
        ".pnodes is_in->.areas;"
        '(way(pivot.areas)["building"];relation(pivot.areas)["building"];)->.enclosing;'
        "(.pois;.enclosing;)->.sites;"
        f'(way(around.sites:{reach})["amenity"="parking"];'
        f'relation(around.sites:{reach})["amenity"="parking"];)->.lots;'
        "(.pois;.enclosing;.lots;);"
        "out body geom;"
    )


# ---------------------------------------------------------------------------
# Overpass (transport)
# ---------------------------------------------------------------------------
async def async_overpass(session: aiohttp.ClientSession, query: str) -> list[dict[str, Any]]:
    """POST a query to Overpass, retrying transient errors and mirrors."""
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        remark = str(payload.get("remark", "")).lower()
                        if any(m in remark for m in _OVERPASS_ERROR_MARKERS):
                            raise OSMConnectionError(f"Overpass reported: {payload['remark']}")
                        return payload.get("elements", [])
                    text = (await response.text())[:200]
                    last_error = OSMConnectionError(f"{url} returned HTTP {response.status}: {text}")
                    transient = response.status in (429, 502, 503, 504)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSMConnectionError) as err:
                last_error = err
                transient = True
            _LOGGER.warning("Overpass request failed (%s, attempt %d/%d): %s", url, attempt, MAX_RETRIES, last_error)
            if not transient:
                break
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    raise OSMConnectionError(f"All Overpass endpoints failed: {last_error}")


# ---------------------------------------------------------------------------
# Footprint-based sizing
# ---------------------------------------------------------------------------
def element_key(element: dict[str, Any]) -> str:
    """``way/123`` style identifier."""
    return f"{element['type']}/{element['id']}"


def element_rings(element: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Geometry of an ``out geom`` element as lat/lon polylines.

    Ways give one ring; multipolygon relations give their *outer* member
    ways (assembling them into closed rings is unnecessary for what we do:
    the enclosing circle only needs vertices).
    """

    def convert(geometry: list[dict[str, float] | None] | None) -> list[tuple[float, float]]:
        return [(g["lat"], g["lon"]) for g in (geometry or []) if g]

    kind = element.get("type")
    if kind == "way":
        ring = convert(element.get("geometry"))
        return [ring] if len(ring) >= 2 else []
    if kind == "relation":
        rings = []
        for member in element.get("members", []):
            if member.get("type") == "way" and member.get("role", "outer") in ("outer", ""):
                ring = convert(member.get("geometry"))
                if len(ring) >= 2:
                    rings.append(ring)
        return rings
    return []


def _parking_is_usable(tags: dict[str, str]) -> bool:
    if tags.get("access") in EXCLUDED_PARKING_ACCESS:
        return False
    if tags.get("parking") in EXCLUDED_PARKING_TYPES:
        return False
    return True


def max_radius_for(type_default_radius: int) -> int:
    """Largest zone radius we will ever create for a POI type."""
    return max(MAX_RADIUS_FLOOR, MAX_RADIUS_FACTOR * type_default_radius)


def _enclosing_circle(rings_xy: list[list[tuple[float, float]]]):
    return min_enclosing_circle([pt for ring in rings_xy for pt in ring])


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def compute_footprints(
    elements: list[dict[str, Any]],
    poi_keys: list[str],
    type_default_radius: int,
) -> dict[str, dict[str, Any]]:
    """Size zones for ``poi_keys`` from an ``out geom`` Overpass response.

    Returns ``{key: result}`` where ``result`` is either ``None`` (no usable
    footprint, caller falls back to a default radius) or a dict with
    ``latitude``/``longitude`` (centre of the minimum enclosing circle of the
    building and its attached parking), ``radius`` (m), ``parking`` (number of
    lots included) and ``capped`` (True if the radius hit the type maximum).
    """
    by_key = {element_key(e): e for e in elements}
    buildings = [
        e
        for e in elements
        if e.get("type") in ("way", "relation") and "building" in e.get("tags", {})
    ]
    lots = [
        e
        for e in elements
        if e.get("type") in ("way", "relation")
        and e.get("tags", {}).get("amenity") == "parking"
        and _parking_is_usable(e.get("tags", {}))
    ]
    max_radius = max_radius_for(type_default_radius)
    results: dict[str, dict[str, Any] | None] = {}

    for key in poi_keys:
        poi = by_key.get(key)
        if poi is None:
            results[key] = None
            continue

        # Local projection centred on the POI keeps all maths in metres.
        if poi["type"] == "node":
            lat0, lon0 = poi.get("lat"), poi.get("lon")
        else:
            first = next((r[0] for r in element_rings(poi) if r), None)
            lat0, lon0 = first if first else (None, None)
        if lat0 is None:
            results[key] = None
            continue

        # 1. The site: the POI's own polygon, or the smallest building around it.
        site_latlon: list[list[tuple[float, float]]] = []
        if poi["type"] == "node":
            pt = project([(lat0, lon0)], lat0, lon0)[0]
            best_area = math.inf
            for b in buildings:
                if key == element_key(b):
                    continue
                for ring_ll in element_rings(b):
                    ring = project(ring_ll, lat0, lon0)
                    if is_closed_ring(ring) and point_in_ring(pt, ring):
                        area = ring_area(ring)
                        if area < best_area:
                            best_area, site_latlon = area, element_rings(b)
        else:
            rings = element_rings(poi)
            if any(is_closed_ring(r) for r in rings):
                site_latlon = rings

        if not site_latlon:
            results[key] = None
            continue
        site_xy = [project(r, lat0, lon0) for r in site_latlon]

        # 2. Parking lots attached to the site.
        attached: list[list[tuple[float, float]]] = []
        attached_count = 0
        for lot in lots:
            if element_key(lot) == key:
                continue
            lot_rings = [project(r, lat0, lon0) for r in element_rings(lot)]
            if any(
                ring_distance(lr, sr, limit=PARKING_ATTACH_DISTANCE) <= PARKING_ATTACH_DISTANCE
                for lr in lot_rings
                for sr in site_xy
            ):
                attached.extend(lot_rings)
                attached_count += 1

        # 3. Smallest circle around site (+ parking). Fall back to the site
        #    alone if adding the lots would exceed the cap (shared lots of
        #    malls and strip centres pull in the neighbours).
        with_lots = _enclosing_circle(site_xy + attached)
        site_only = _enclosing_circle(site_xy)
        chosen, used_lots = with_lots, attached_count
        if with_lots and with_lots[2] + RADIUS_PADDING > max_radius and attached:
            chosen, used_lots = site_only, 0
        if chosen is None:
            results[key] = None
            continue

        cx, cy, r = chosen
        radius = _round_up(r + RADIUS_PADDING, RADIUS_ROUNDING)
        capped = radius > max_radius
        radius = max(MIN_ZONE_RADIUS, min(radius, max_radius))
        lat, lon = unproject(cx, cy, lat0, lon0)
        results[key] = {
            "latitude": round(lat, 7),
            "longitude": round(lon, 7),
            "radius": radius,
            "parking": used_lots,
            "capped": capped,
        }
    return results
