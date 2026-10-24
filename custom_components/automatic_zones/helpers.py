"""Small shared helpers (no Home Assistant imports beyond typing)."""
from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_ZONE_PREFIX


def entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Option if the user changed it, else the value chosen at setup."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def zone_prefix(entry: ConfigEntry) -> str:
    """The optional name prefix; empty string when not set."""
    return str(entry_value(entry, CONF_ZONE_PREFIX, "") or "").strip()


def slugify(text: str) -> str:
    """Rough slug used only as a fallback guess for a zone's entity id."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:255]


def assign_zone_names(
    pois: list[dict[str, Any]], prefix: str, distinguish_duplicates: bool = False
) -> None:
    """Set ``poi["zone_name"]`` for every POI, making duplicates distinct.

    * With a prefix the format is ``{prefix}_{name}`` (same as the original
      integration). Without one it is just the POI's name.
    * Several POIs often share a name (every Starbucks, every Walmart). By
      default they all keep that exact name. With ``distinguish_duplicates``
      each of them gets a suffix instead: its street if that tells them
      apart, otherwise its OpenStreetMap id. POIs with a unique name are
      never suffixed, so their names (and entity ids) stay stable.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for poi in pois:
        base = f"{prefix}_{poi['name']}" if prefix else poi["name"]
        poi["zone_name"] = base
        groups.setdefault(base.casefold(), []).append(poi)

    if not distinguish_duplicates:
        return
    for members in groups.values():
        if len(members) == 1:
            continue
        streets = [(m.get("street") or "").casefold() for m in members]
        streets_distinct = all(streets) and len(set(streets)) == len(streets)
        for m in members:
            if streets_distinct:
                suffix = m["street"]
            else:
                suffix = f"{m['osm_type'][0]}{m['osm_id']}"
            m["zone_name"] = f"{m['zone_name']} ({suffix})"
