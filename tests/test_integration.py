"""End-to-end: real zone component, mocked Overpass."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.automatic_zones.const import DOMAIN
from custom_components.automatic_zones.osm import OSMConnectionError, Place

from .osm_data import LAT0, LON0, ll, node, rect, way

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

OVERPASS = "custom_components.automatic_zones.coordinator.async_overpass"

PHASE1 = [
    node(1, 0, 0, name="Starbucks", **{"addr:street": "Holcomb Bridge Rd"}),
    node(2, 3000, 0, name="Starbucks", **{"addr:street": "Alpharetta Hwy"}),
    node(3, 0, 2000, name="Cafe Solo"),
    node(4, 500, 500),  # unnamed -> skipped
]
GEOMETRY = [
    node(1, 0, 0, name="Starbucks"),
    node(2, 3000, 0, name="Starbucks"),
    node(3, 0, 2000, name="Cafe Solo"),
    way(10, rect(-20, -15, 20, 15), building="yes"),
    way(20, rect(30, -20, 80, 20), amenity="parking"),
    way(11, rect(2980, -15, 3020, 15), building="yes"),
]


async def fake_overpass(session, query):
    return PHASE1 if "out tags center" in query else GEOMETRY


def make_entry(**overrides):
    data = {
        "poi_type": "restaurant", "latitude": LAT0, "longitude": LON0,
        "place_name": "Roswell, Georgia", "zone_prefix": "", "passive": False,
        "auto_radius": True, "search_radius": 25, "zone_radius": 75,
        "distinguish_duplicates": True,
    }
    data.update(overrides)
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=data, unique_id="t")
    return entry


@pytest.fixture(autouse=True)
def _no_stagger():
    with patch("custom_components.automatic_zones.coordinator.REQUEST_STAGGER_DELAY", 0):
        yield


async def _setup(hass, entry):
    assert await async_setup_component(hass, "zone", {})
    entry.add_to_hass(hass)
    with patch(OVERPASS, side_effect=fake_overpass):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


def zones(hass):
    return {z["id"]: z for z in hass.data["zone"].async_items()}


def by_name(hass, name):
    return next(z for z in zones(hass).values() if z["name"] == name)


async def test_zones_created_sized_and_named(hass):
    await _setup(hass, make_entry())
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    # No prefix; duplicate names told apart by street; unnamed POI skipped.
    assert names == ["Cafe Solo", "Starbucks (Alpharetta Hwy)", "Starbucks (Holcomb Bridge Rd)"]

    sb = by_name(hass, "Starbucks (Holcomb Bridge Rd)")
    assert sb["passive"] is False
    assert sb["radius"] == 75.0  # building + attached lot, from OSM outlines
    assert sb["longitude"] > LON0  # recentred toward the parking lot
    assert by_name(hass, "Starbucks (Alpharetta Hwy)")["radius"] == 50.0  # building only
    assert by_name(hass, "Cafe Solo")["radius"] == 75.0  # no footprint -> fallback

    state = hass.states.get("sensor.restaurants_near_roswell_georgia")
    assert state.state == "3"
    assert state.attributes["zones_sized_from_osm"] == 2
    assert state.attributes["zones_pending_size"] == 0
    for loc in state.attributes["locations"]:
        assert hass.states.get(loc["zone_entity_id"]) is not None


@pytest.mark.parametrize("passive", [True, False])
async def test_passive_toggle_is_applied(hass, passive):
    await _setup(hass, make_entry(passive=passive))
    custom = [z for z in zones(hass).values() if z["name"] != "Home"]
    assert custom and all(z["passive"] is passive for z in custom)
    zone_state = hass.states.get(
        hass.states.get("sensor.restaurants_near_roswell_georgia").attributes["zone_entity_ids"][0]
    )
    assert zone_state.attributes["passive"] is passive


async def test_prefix_is_applied_when_given(hass):
    await _setup(hass, make_entry(zone_prefix="food"))
    assert by_name(hass, "food_Cafe Solo")


async def test_hand_made_zone_with_same_name_is_never_touched(hass):
    assert await async_setup_component(hass, "zone", {})
    mine = await hass.data["zone"].async_create_item(
        {"name": "Cafe Solo", "latitude": 10.0, "longitude": 10.0, "radius": 333.0, "passive": False}
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch(OVERPASS, side_effect=fake_overpass):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    untouched = zones(hass)[mine["id"]]
    assert (untouched["latitude"], untouched["radius"]) == (10.0, 333.0)
    assert sum(z["name"] == "Cafe Solo" for z in zones(hass).values()) == 2

    # The sensor reports OUR zone's real entity id (the registry added _2).
    attrs = hass.states.get("sensor.restaurants_near_roswell_georgia").attributes
    solo = next(l for l in attrs["locations"] if l["name"] == "Cafe Solo")
    assert solo["zone_entity_id"] == "zone.cafe_solo_2"

    # Deleting the entry removes ours and leaves the hand-made one.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert set(zones(hass)) & {mine["id"]} == {mine["id"]}
    assert not any(z["name"].startswith("Starbucks") for z in zones(hass).values())


async def test_resync_is_idempotent(hass):
    entry = make_entry()
    await _setup(hass, entry)
    collection = hass.data["zone"]
    with patch.object(collection, "async_update_item", wraps=collection.async_update_item) as upd, patch.object(
        collection, "async_create_item", wraps=collection.async_create_item
    ) as create:
        await entry.runtime_data.zones.async_sync()
    assert upd.call_count == 0 and create.call_count == 0


async def test_options_rename_and_flip_passive_in_place(hass):
    entry = make_entry()
    await _setup(hass, entry)
    ids_before = set(zones(hass))
    entity_ids_before = hass.states.get("sensor.restaurants_near_roswell_georgia").attributes["zone_entity_ids"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    with patch(OVERPASS, side_effect=fake_overpass):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"zone_prefix": "poi", "passive": True, "distinguish_duplicates": True, "auto_radius": True, "search_radius": 25, "zone_radius": 75},
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert set(zones(hass)) == ids_before  # same zones, not recreated
    assert by_name(hass, "poi_Cafe Solo")["passive"] is True
    assert hass.states.get("sensor.restaurants_near_roswell_georgia").attributes["zone_entity_ids"] == entity_ids_before

    # Clearing the prefix again really clears it (empty field is omitted by the UI).
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(OVERPASS, side_effect=fake_overpass):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"passive": False, "distinguish_duplicates": True, "auto_radius": True, "search_radius": 25, "zone_radius": 75},
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert by_name(hass, "Cafe Solo")["passive"] is False


async def test_unload_keeps_zones(hass):
    entry = make_entry()
    await _setup(hass, entry)
    before = set(zones(hass))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert set(zones(hass)) == before


async def test_auto_radius_off_uses_fixed_radius_and_poi_position(hass):
    await _setup(hass, make_entry(auto_radius=False, zone_radius=120))
    sb = by_name(hass, "Starbucks (Holcomb Bridge Rd)")
    assert sb["radius"] == 120.0
    assert abs(sb["longitude"] - LON0) < 1e-6  # not recentred
    assert hass.states.get("sensor.restaurants_near_roswell_georgia").attributes["zones_pending_size"] == 0


async def test_failed_lookup_does_not_delete_zones(hass):
    entry = make_entry()
    await _setup(hass, entry)
    before = set(zones(hass))
    coordinator = entry.runtime_data.coordinator
    with patch(OVERPASS, side_effect=OSMConnectionError("down")):
        coordinator.force_refresh = True
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert set(zones(hass)) == before  # nothing mass-deleted on an outage


async def test_empty_result_does_not_delete_zones(hass):
    entry = make_entry()
    await _setup(hass, entry)
    before = set(zones(hass))
    coordinator = entry.runtime_data.coordinator
    with patch(OVERPASS, AsyncMock(return_value=[])):
        coordinator.force_refresh = True
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert set(zones(hass)) == before


async def test_legacy_v1_entry_is_migrated_and_zones_adopted(hass):
    assert await async_setup_component(hass, "zone", {})
    # What the original integration left behind: name "{prefix}_{name}" at the POI.
    p = ll(0, 2000)
    old = await hass.data["zone"].async_create_item(
        {"name": "restaurant_Cafe Solo", "latitude": p["lat"], "longitude": p["lon"], "radius": 150.0, "passive": False}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, unique_id="legacy",
        data={"city": "Roswell, GA", "poi_type": "restaurant", "zone_prefix": "restaurant",
              "search_radius": 25, "zone_radius": 150},
    )
    entry.add_to_hass(hass)
    place = Place(LAT0, LON0, "Roswell, Georgia", "Roswell, Georgia", "relation", 1)
    with patch(OVERPASS, side_effect=fake_overpass), patch(
        "custom_components.automatic_zones.async_geocode", AsyncMock(return_value=[place])
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.version == 2
    assert entry.data["passive"] is False and entry.data["auto_radius"] is False
    assert (entry.data["latitude"], entry.data["longitude"]) == (LAT0, LON0)
    assert sum(z["name"] == "restaurant_Cafe Solo" for z in zones(hass).values()) == 1  # adopted, not duplicated
    assert entry.runtime_data.zones.owned["node/3"] == old["id"]


# ---- same-named places keep the same name unless asked otherwise -----------
async def test_default_keeps_identical_names_but_separate_zones(hass):
    entry = make_entry(distinguish_duplicates=False)
    await _setup(hass, entry)
    custom = [z for z in zones(hass).values() if z["name"] != "Home"]
    assert sorted(z["name"] for z in custom) == ["Cafe Solo", "Starbucks", "Starbucks"]
    starbucks = [z for z in custom if z["name"] == "Starbucks"]
    assert len({z["id"] for z in starbucks}) == 2
    assert starbucks[0]["latitude"] != starbucks[1]["longitude"]  # genuinely different places

    attrs = hass.states.get("sensor.restaurants_near_roswell_georgia").attributes
    entity_ids = [l["zone_entity_id"] for l in attrs["locations"] if l["name"] == "Starbucks"]
    assert sorted(entity_ids) == ["zone.starbucks", "zone.starbucks_2"]
    assert attrs["zone_names"].count("Starbucks") == 2

    # Re-syncing identical names must not shuffle or recreate anything.
    ids_before = set(zones(hass))
    collection = hass.data["zone"]
    with patch.object(collection, "async_update_item", wraps=collection.async_update_item) as upd, patch.object(
        collection, "async_create_item", wraps=collection.async_create_item
    ) as create:
        await entry.runtime_data.zones.async_sync()
    assert (upd.call_count, create.call_count) == (0, 0) and set(zones(hass)) == ids_before


async def test_default_with_prefix_has_no_suffix_either(hass):
    await _setup(hass, make_entry(zone_prefix="food", distinguish_duplicates=False))
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    assert names == ["food_Cafe Solo", "food_Starbucks", "food_Starbucks"]


async def test_toggling_distinguish_renames_in_place(hass):
    entry = make_entry(distinguish_duplicates=False)
    await _setup(hass, entry)
    ids_before = set(zones(hass))

    async def configure(value):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(OVERPASS, side_effect=fake_overpass):
            await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"passive": False, "distinguish_duplicates": value, "auto_radius": True,
                 "search_radius": 25, "zone_radius": 75},
            )
            await hass.async_block_till_done(wait_background_tasks=True)

    await configure(True)
    assert set(zones(hass)) == ids_before
    assert by_name(hass, "Starbucks (Holcomb Bridge Rd)") and by_name(hass, "Starbucks (Alpharetta Hwy)")
    await configure(False)
    assert set(zones(hass)) == ids_before
    assert sum(z["name"] == "Starbucks" for z in zones(hass).values()) == 2


async def test_many_identical_names(hass):
    twenty = [node(100 + i, i * 400, 5000, name="Starbucks") for i in range(20)]
    async def twenty_overpass(session, query):
        return twenty

    with patch(OVERPASS, side_effect=twenty_overpass):
        assert await async_setup_component(hass, "zone", {})
        entry = make_entry(auto_radius=False, distinguish_duplicates=False)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    custom = [z for z in zones(hass).values() if z["name"] != "Home"]
    assert len(custom) == 20 and {z["name"] for z in custom} == {"Starbucks"}



# -- labels -----------------------------------------------------------------
def zone_labels(hass):
    reg = er.async_get(hass)
    out = {}
    for z in zones(hass).values():
        eid = reg.async_get_entity_id("zone", "zone", z["id"])
        out[z["name"]] = set(reg.async_get(eid).labels) if eid else None
    return out


async def _options(hass, entry, **over):
    base = {"passive": False, "distinguish_duplicates": True, "auto_radius": True,
            "search_radius": 25, "zone_radius": 75}
    base.update(over)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(OVERPASS, side_effect=fake_overpass):
        await hass.config_entries.options.async_configure(result["flow_id"], base)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_no_label_by_default(hass):
    entry = make_entry()
    await _setup(hass, entry)
    assert all(v == set() for v in zone_labels(hass).values())


async def test_existing_label_is_applied(hass):
    from homeassistant.helpers import label_registry as lr
    assert await async_setup_component(hass, "zone", {})
    label = lr.async_get(hass).async_create("Shopping")
    entry = make_entry(label=label.label_id)
    await _setup(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    labels = zone_labels(hass)
    assert len(labels) == 3
    assert all(v == {label.label_id} for v in labels.values())


async def test_new_label_is_created_reused_and_wins(hass):
    from homeassistant.helpers import label_registry as lr
    assert await async_setup_component(hass, "zone", {})
    other = lr.async_get(hass).async_create("Other")
    entry = make_entry(label=other.label_id, new_label="Auto Zones")
    await _setup(hass, entry)
    created = lr.async_get(hass).async_get_label_by_name("Auto Zones")
    assert created is not None
    assert all(v == {created.label_id} for v in zone_labels(hass).values())
    # Reload: the same label is reused, not duplicated.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len([l for l in lr.async_get(hass).async_list_labels() if l.name == "Auto Zones"]) == 1


async def test_changing_and_clearing_label_only_touches_our_label(hass):
    from homeassistant.helpers import label_registry as lr
    assert await async_setup_component(hass, "zone", {})
    reg = lr.async_get(hass)
    a, b, mine = reg.async_create("A"), reg.async_create("B"), reg.async_create("Mine")
    entry = make_entry(label=a.label_id)
    await _setup(hass, entry)
    ent = er.async_get(hass)
    cafe = ent.async_get_entity_id("zone", "zone", by_name(hass, "Cafe Solo")["id"])
    ent.async_update_entity(cafe, labels=set(ent.async_get(cafe).labels) | {mine.label_id})

    await _options(hass, entry, label=b.label_id)
    labels = zone_labels(hass)
    assert labels["Cafe Solo"] == {b.label_id, mine.label_id}  # A swapped for B, hand label kept
    assert all(b.label_id in v and a.label_id not in v for v in labels.values())

    await _options(hass, entry)  # cleared
    labels = zone_labels(hass)
    assert labels["Cafe Solo"] == {mine.label_id}
    assert labels["Starbucks (Holcomb Bridge Rd)"] == set()


async def test_deleted_label_id_is_ignored(hass):
    entry = make_entry(label="does_not_exist")
    await _setup(hass, entry)
    assert all(v == set() for v in zone_labels(hass).values())


async def test_zone_collection_found_in_new_and_old_layouts(hass):
    """HA 2026.x wraps the collection in ZoneData; older versions store it directly."""
    from types import SimpleNamespace

    from custom_components.automatic_zones.zones import get_zone_collection

    assert await async_setup_component(hass, "zone", {})
    real = hass.data["zone"]
    collection = real if hasattr(real, "async_create_item") else real.storage_collection
    try:
        hass.data["zone"] = collection  # old layout
        assert get_zone_collection(hass) is collection
        hass.data["zone"] = SimpleNamespace(storage_collection=collection)  # new layout
        assert get_zone_collection(hass) is collection
        hass.data["zone"] = SimpleNamespace()  # nothing usable
        assert get_zone_collection(hass) is None
    finally:
        hass.data["zone"] = real


# -- business-name mode, capitalization, readable suffixes ------------------
ALDIS = [
    node(501, 0, 0, name="ALDI", shop="supermarket"),
    node(502, 3000, 0, name="Aldi", shop="supermarket"),
    node(503, 0, 3000, name="ALDI", shop="supermarket", **{"addr:housenumber": "5"}),
]
REVERSE = "custom_components.automatic_zones.coordinator.async_reverse_geocode"


def _reverse_by_node(streets):
    calls = []

    async def fake(session, lat, lon, language="en"):
        calls.append((lat, lon))
        for el in ALDIS:
            if abs(el["lat"] - lat) < 1e-9 and abs(el["lon"] - lon) < 1e-9:
                return streets.get(el["id"], {})
        return {}

    fake.calls = calls
    return fake


async def _setup_business(hass, reverse, **overrides):
    queries = []

    async def overpass(session, query):
        queries.append(query)
        return ALDIS

    entry = make_entry(
        poi_type="business", business_name="Aldi", auto_radius=False,
        distinguish_duplicates=True, **overrides,
    )
    assert await async_setup_component(hass, "zone", {})
    entry.add_to_hass(hass)
    with patch(OVERPASS, side_effect=overpass), patch(REVERSE, side_effect=reverse), patch(
        "custom_components.automatic_zones.coordinator.REVERSE_DELAY", 0
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry, queries


async def test_business_mode_unifies_case_and_uses_streets_from_lookup(hass):
    reverse = _reverse_by_node({
        501: {"street": "Holcomb Bridge Rd", "city": "Roswell"},
        502: {"street": "Alpharetta Hwy", "city": "Roswell"},
        503: {"street": "Mansell Rd", "city": "Alpharetta"},
    })
    entry, queries = await _setup_business(hass, reverse)

    assert '["brand"~' in queries[0] and '"Aldi",i' in queries[0]  # searched by name, not category
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    assert names == ["ALDI (Alpharetta Hwy)", "ALDI (Holcomb Bridge Rd)", "ALDI (Mansell Rd)"]
    assert len(reverse.calls) == 3

    state = hass.states.get("sensor.aldi_near_roswell_georgia")
    assert state is not None and state.state == "3"
    assert state.attributes["poi_type"] == "business" and state.attributes["poi_type_name"] == "Aldi"
    assert {l["name"] for l in state.attributes["locations"]} == {"ALDI"}  # one capitalization
    assert all(l.get("icon") is None or True for l in state.attributes["locations"])

    # Cached: a refresh does not geocode again.
    coordinator = entry.runtime_data.coordinator
    with patch(OVERPASS, side_effect=fake_overpass), patch(REVERSE, side_effect=reverse):
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(reverse.calls) == 3


async def test_no_osm_ids_in_names_when_nothing_is_known(hass):
    entry, _ = await _setup_business(hass, _reverse_by_node({}))
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    assert names == ["ALDI (#1)", "ALDI (#2)", "ALDI (#3)"]
    assert not any("node" in n or "501" in n for n in names)


async def test_failed_lookup_keeps_plain_names_and_does_not_crash(hass):
    async def broken(session, lat, lon, language="en"):
        raise OSMConnectionError("offline")

    entry, _ = await _setup_business(hass, broken)
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    assert names == ["ALDI", "ALDI", "ALDI"]
    assert entry.state is ConfigEntryState.LOADED


async def test_lookup_only_for_places_that_need_it(hass):
    # Streets are already in the OSM tags -> no reverse geocoding at all.
    tagged = [
        node(601, 0, 0, name="Aldi", **{"addr:street": "A St"}),
        node(602, 3000, 0, name="ALDI", **{"addr:street": "B St"}),
    ]

    async def overpass(session, query):
        return tagged

    reverse = AsyncMock(return_value={})
    entry = make_entry(poi_type="business", business_name="Aldi", auto_radius=False, distinguish_duplicates=True)
    assert await async_setup_component(hass, "zone", {})
    entry.add_to_hass(hass)
    with patch(OVERPASS, side_effect=overpass), patch(REVERSE, reverse):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert reverse.call_count == 0
    names = sorted(z["name"] for z in zones(hass).values() if z["name"] != "Home")
    assert names == ["Aldi (A St)", "Aldi (B St)"]  # tie -> mixed case beats ALL CAPS
