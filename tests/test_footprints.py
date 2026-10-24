"""Footprint sizing on synthetic OSM data (no Home Assistant needed)."""
import pytest

from custom_components.poi_zones.geometry import haversine_m
from custom_components.poi_zones.osm import (
    build_geometry_query,
    compute_footprints,
    max_radius_for,
)

from .osm_data import LAT0, LON0, node, rect, relation, way

RESTAURANT_DEFAULT = 75


def run(elements, key, default=RESTAURANT_DEFAULT):
    return compute_footprints(elements, [key], default)[key]


def test_node_in_building_with_adjacent_parking():
    # Building 40x30 m centred on the node; lot 50x40 m, 10 m to the east.
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(30, -20, 80, 20), amenity="parking"),
    ]
    res = run(elements, "node/1")
    assert res["parking"] == 1
    # Farthest corners: (-20,-15) and (80,+-20) -> circle must cover both.
    assert res["radius"] >= 50 + 20 - 5  # half of ~102 m diagonal-ish span + padding
    assert res["radius"] % 5 == 0
    # Centre moves east, toward the lot, away from the node.
    assert res["longitude"] > LON0
    assert haversine_m((LAT0, LON0), (res["latitude"], res["longitude"])) > 15


def test_every_vertex_is_inside_the_zone():
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(30, -20, 80, 20), amenity="parking"),
    ]
    res = run(elements, "node/1")
    for el in elements[1:]:
        for p in el["geometry"]:
            d = haversine_m((res["latitude"], res["longitude"]), (p["lat"], p["lon"]))
            assert d <= res["radius"] - 15  # padding left over


@pytest.mark.parametrize(
    "tags",
    [
        {"access": "private"},
        {"access": "no"},
        {"parking": "underground"},
        {"parking": "street_side"},
    ],
)
def test_excluded_parking_is_ignored(tags):
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(30, -20, 80, 20), amenity="parking", **tags),
    ]
    res = run(elements, "node/1")
    assert res["parking"] == 0
    assert res["radius"] <= 50  # 25 m half-diagonal + 20 padding, rounded up


def test_customer_parking_is_used():
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(30, -20, 80, 20), amenity="parking", access="customers"),
    ]
    assert run(elements, "node/1")["parking"] == 1


def test_far_parking_is_not_attached():
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(120, -20, 170, 20), amenity="parking"),  # 100 m gap
    ]
    assert run(elements, "node/1")["parking"] == 0


def test_node_without_building_has_no_footprint():
    elements = [node(1, 0, 0, name="Kiosk"), way(20, rect(30, -20, 80, 20), amenity="parking")]
    assert run(elements, "node/1") is None


def test_smallest_containing_building_wins():
    elements = [
        node(1, 0, 0, name="Shop"),
        way(10, rect(-100, -100, 100, 100), building="retail"),  # whole mall
        way(11, rect(-10, -10, 10, 10), building="yes"),  # the unit
    ]
    res = run(elements, "node/1")
    assert res["radius"] <= 50


def test_way_poi_uses_its_own_outline():
    elements = [way(5, rect(-100, -50, 100, 50), amenity="school", name="School")]
    res = run(elements, "way/5", default=200)
    assert 120 <= res["radius"] <= 140  # ~112 m half-diagonal + 20 padding


def test_relation_poi_uses_outer_members():
    elements = [
        relation(7, [rect(-30, -30, 30, 30), rect(100, -30, 160, 30)], amenity="hospital", name="H")
    ]
    res = run(elements, "relation/7", default=200)
    assert res["radius"] > 90


def test_huge_shared_lot_falls_back_to_building_only():
    # Restaurant max radius is 225 m. A 600 m long lot would need ~300 m.
    elements = [
        node(1, 0, 0, name="Cafe"),
        way(10, rect(-20, -15, 20, 15), building="yes"),
        way(20, rect(25, -20, 625, 20), amenity="parking"),
    ]
    res = run(elements, "node/1")
    assert res["parking"] == 0
    assert res["radius"] <= 50


def test_radius_is_capped_and_floored():
    # Giant site is capped...
    elements = [way(5, rect(-1000, -1000, 1000, 1000), leisure="park", name="Park")]
    res = run(elements, "way/5", default=200)
    assert res["radius"] == max_radius_for(200) == 600 and res["capped"]
    # ...a tiny one is raised to the floor.
    elements = [node(1, 0, 0, name="Booth"), way(10, rect(-3, -3, 3, 3), building="yes")]
    assert run(elements, "node/1")["radius"] == 50


def test_missing_element_returns_none():
    assert compute_footprints([], ["node/99"], 75)["node/99"] is None


def test_open_way_poi_is_not_a_polygon():
    line = [{"lat": LAT0, "lon": LON0}, {"lat": LAT0 + 0.001, "lon": LON0}]
    assert run([way(5, line, railway="station")], "way/5") is None


def test_query_contains_ids_and_skips_empty_kinds():
    q = build_geometry_query(["node/1", "node/2", "way/9"])
    assert "node(id:1,2);" in q and "way(id:9);" in q and "relation(id:" not in q
    assert "out body geom;" in q and "is_in" in q
