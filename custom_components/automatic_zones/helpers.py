"""Small shared helpers (no Home Assistant imports beyond typing)."""
from __future__ import annotations

import re
from collections import Counter
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


def _single_case(text: str) -> bool:
    """True for ALL CAPS or all lower case (less deliberate than Title Case)."""
    return text == text.upper() or text == text.lower()


def unify_name_case(pois: list[dict[str, Any]]) -> None:
    """Give places whose names differ only in capitalization one spelling.

    OpenStreetMap contributors spell the same chain differently (``ALDI`` /
    ``Aldi``). The most common spelling wins; on a tie, one that is not ALL CAPS
    or all lower case is preferred, then alphabetical order (so the result never
    depends on the order the places arrive in). Extra spaces are collapsed.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for poi in pois:
        poi["name"] = " ".join(str(poi["name"]).split())
        groups.setdefault(poi["name"].casefold(), []).append(poi)
    for members in groups.values():
        counts = Counter(m["name"] for m in members)
        if len(counts) == 1:
            continue
        canonical = min(counts, key=lambda v: (-counts[v], _single_case(v), v))
        for m in members:
            m["name"] = canonical


def _levels(poi: dict[str, Any]) -> list[str | None]:
    """Labels that could tell a place from its namesakes, simplest first."""
    street = poi.get("street")
    number = poi.get("housenumber")
    suburb = poi.get("suburb")
    city = poi.get("city")
    return [
        street or None,
        f"{number} {street}" if street and number else None,
        suburb or None,
        city or None,
        f"{street}, {city}" if street and city else None,
        f"{number} {street}, {city}" if street and number and city else None,
    ]


def distinguishing_suffixes(members: list[dict[str, Any]]) -> tuple[dict[str, str], bool]:
    """Readable suffix per place (keyed by POI key) for places sharing a name.

    Uses the first label (street, then house number + street, then
    neighbourhood, then city, then street + city ...) that is available for
    *every* place and different for each. If none manages that, each place gets
    its most specific label and any that still collide are numbered
    (``#1``, ``#2``). The second return value says whether a label alone was
    enough (False means more address data would help).
    """
    levels = {m["key"]: _levels(m) for m in members}
    for i in range(len(next(iter(levels.values())))):
        labels = [levels[m["key"]][i] for m in members]
        if all(labels) and len({label.casefold() for label in labels}) == len(labels):
            return {m["key"]: label for m, label in zip(members, labels)}, True

    best = {
        key: next((lv[i] for i in (5, 4, 1, 0, 3, 2) if lv[i]), "") for key, lv in levels.items()
    }
    colliding: dict[str, list[str]] = {}
    for key in sorted(best):
        colliding.setdefault(best[key].casefold(), []).append(key)
    suffixes = dict(best)
    for keys in colliding.values():
        if len(keys) > 1:
            for n, key in enumerate(keys, 1):
                suffixes[key] = f"{best[key]}, #{n}" if best[key] else f"#{n}"
    return suffixes, False


def _groups(pois: list[dict[str, Any]], prefix: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for poi in pois:
        base = f"{prefix}_{poi['name']}" if prefix else poi["name"]
        groups.setdefault(base.casefold(), []).append(poi)
    return groups


def pois_needing_lookup(pois: list[dict[str, Any]], prefix: str) -> list[str]:
    """Keys of same-named places that OSM tags alone cannot tell apart.

    Those are worth a reverse-geocoding request to learn their street / city.
    """
    unify_name_case(pois)
    keys: list[str] = []
    for members in _groups(pois, prefix).values():
        if len(members) > 1 and not distinguishing_suffixes(members)[1]:
            keys.extend(m["key"] for m in members)
    return keys


def assign_zone_names(
    pois: list[dict[str, Any]], prefix: str, distinguish_duplicates: bool = False
) -> None:
    """Set ``poi["zone_name"]`` for every POI, making duplicates distinct.

    * Names that differ only in capitalization are first made identical
      (:func:`unify_name_case`).
    * With a prefix the format is ``{prefix}_{name}`` (same as the original
      integration). Without one it is just the POI's name.
    * Several POIs often share a name (every Starbucks, every Walmart). By
      default they all keep that exact name. With ``distinguish_duplicates``
      each gets a readable suffix in brackets, such as its street, or its city
      (see :func:`distinguishing_suffixes`). Uniquely named places are never
      suffixed, so their names (and entity ids) stay stable.
    """
    unify_name_case(pois)
    groups = _groups(pois, prefix)
    for base_key, members in groups.items():
        base = f"{prefix}_{members[0]['name']}" if prefix else members[0]["name"]
        for m in members:
            m["zone_name"] = base
    if not distinguish_duplicates:
        return
    for members in groups.values():
        if len(members) == 1:
            continue
        suffixes, _ = distinguishing_suffixes(members)
        for m in members:
            if suffix := suffixes.get(m["key"]):
                m["zone_name"] = f"{m['zone_name']} ({suffix})"


# Departments and services inside a big store are mapped as places of their own
# ("Walmart Pharmacy"). A search for the store name should not return them
# unless the search itself names one.
DEPARTMENT_WORDS: tuple[str, ...] = (
    "pharmacy", "vision", "optical", "optometry", "auto care", "tire", "lube",
    "garden center", "bakery", "deli", "photo", "portrait", "money center",
    "money services", "fuel", "gas station", "gas", "pickup", "grocery pickup",
    "hearing", "wireless", "connection center", "service center", "car wash",
    "clinic", "health center", "dental",
)


def _words(text: str) -> str:
    return " " + " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split()) + " "


def is_department(name: str, query: str) -> bool:
    """True when ``name`` is a department of a store the ``query`` does not ask for."""
    padded_name, padded_query = _words(name), _words(query)
    return any(
        f" {word} " in padded_name and f" {word} " not in padded_query
        for word in DEPARTMENT_WORDS
    )


# Categories that are usually a service inside, or beside, a bigger store: a
# fuel station called just "Walmart", a pharmacy counter, an optician. They are
# dropped from a business search ONLY when the other matches are a different
# kind of place, so searching "QuikTrip" still returns every gas station.
SERVICE_CATEGORIES: frozenset[str] = frozenset(
    {"fuel", "pharmacy", "optician", "car_wash", "car_repair", "tyres", "bakery", "photo",
     "hearing_aids", "bank", "atm", "post_office", "doctors", "clinic", "dentist"}
)


def _category(tags: dict[str, Any]) -> str:
    return str(tags.get("shop") or tags.get("amenity") or tags.get("healthcare") or "")


def drop_services(elements: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Remove service-type matches when the main matches are something else."""
    counts = Counter(_category(e.get("tags", {})) for e in elements)
    counts.pop("", None)
    if not counts:
        return elements
    dominant = counts.most_common(1)[0][0]
    if dominant in SERVICE_CATEGORIES:
        return elements  # the business itself is a service (QuikTrip, Walgreens)
    asked = _words(query)
    return [
        e for e in elements
        if (c := _category(e.get("tags", {}))) not in SERVICE_CATEGORIES
        or c == dominant
        or f" {c.replace('_', ' ')} " in asked
    ]
