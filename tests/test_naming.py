"""Capitalization, readable duplicate suffixes, business-name queries."""
import re

import pytest

from custom_components.automatic_zones.helpers import (
    assign_zone_names,
    distinguishing_suffixes,
    pois_needing_lookup,
    unify_name_case,
)
from custom_components.automatic_zones.osm import build_business_query, business_name_regex


def poi(n, name="Aldi", **extra):
    return {"key": f"node/{n}", "osm_type": "node", "osm_id": n, "name": name, **extra}


# -- capitalization ---------------------------------------------------------
def test_majority_spelling_wins():
    pois = [poi(1, "ALDI"), poi(2, "ALDI"), poi(3, "Aldi"), poi(4, "ALDI")]
    unify_name_case(pois)
    assert {p["name"] for p in pois} == {"ALDI"}


def test_tie_prefers_mixed_case_over_all_caps_or_lower():
    pois = [poi(1, "ALDI"), poi(2, "Aldi")]
    unify_name_case(pois)
    assert {p["name"] for p in pois} == {"Aldi"}
    pois = [poi(1, "aldi"), poi(2, "ALDI"), poi(3, "AlDi")]
    unify_name_case(pois)
    assert {p["name"] for p in pois} == {"AlDi"}


def test_result_does_not_depend_on_input_order():
    a = [poi(1, "Foo Bar"), poi(2, "FOO BAR"), poi(3, "foo bar")]
    b = list(reversed([dict(p) for p in a]))
    unify_name_case(a)
    unify_name_case(b)
    assert {p["name"] for p in a} == {p["name"] for p in b}


def test_different_names_and_spacing():
    pois = [poi(1, "Aldi"), poi(2, "Aldi  Foods"), poi(3, "aldi foods")]
    unify_name_case(pois)
    assert pois[0]["name"] == "Aldi" and {p["name"] for p in pois[1:]} == {"Aldi Foods"}


def test_zone_names_are_identical_across_case_variants_even_without_suffixes():
    pois = [poi(1, "ALDI"), poi(2, "Aldi"), poi(3, "ALDI")]
    assign_zone_names(pois, "Groceries", False)
    assert {p["zone_name"] for p in pois} == {"Groceries_ALDI"}


# -- readable suffixes ------------------------------------------------------
def test_street_is_used_when_distinct():
    pois = [poi(1, street="Holcomb Bridge Rd"), poi(2, street="Alpharetta Hwy")]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == ["Aldi (Holcomb Bridge Rd)", "Aldi (Alpharetta Hwy)"]


def test_same_street_uses_house_number():
    pois = [poi(1, street="Main St", housenumber="10"), poi(2, street="Main St", housenumber="200")]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == ["Aldi (10 Main St)", "Aldi (200 Main St)"]


def test_falls_back_to_neighbourhood_then_city():
    pois = [poi(1, suburb="Midtown", city="Atlanta"), poi(2, suburb="Buckhead", city="Atlanta")]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == ["Aldi (Midtown)", "Aldi (Buckhead)"]
    pois = [poi(1, city="Roswell"), poi(2, city="Alpharetta")]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == ["Aldi (Roswell)", "Aldi (Alpharetta)"]


def test_street_and_city_combined_only_when_neither_alone_is_enough():
    pois = [
        poi(1, street="Main St", city="Roswell"),
        poi(2, street="Main St", city="Alpharetta"),
        poi(3, street="Oak Ave", city="Roswell"),
    ]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == [
        "Aldi (Main St, Roswell)", "Aldi (Main St, Alpharetta)", "Aldi (Oak Ave, Roswell)",
    ]


def test_never_shows_osm_ids():
    # No address data at all: numbered, never an OSM id.
    pois = [poi(4643857694), poi(8715919171), poi(8715967950)]
    assign_zone_names(pois, "", True)
    names = [p["zone_name"] for p in pois]
    assert names == ["Aldi (#1)", "Aldi (#2)", "Aldi (#3)"]
    assert not any(re.search(r"\d{6,}|\bn\d|\bw\d", n) for n in names)


def test_partial_data_mixes_labels_and_numbers_only_collisions():
    pois = [poi(1, street="Main St"), poi(2, street="Oak Ave"), poi(3), poi(4)]
    assign_zone_names(pois, "", True)
    assert [p["zone_name"] for p in pois] == ["Aldi (Main St)", "Aldi (Oak Ave)", "Aldi (#1)", "Aldi (#2)"]


def test_unresolved_street_collision_is_numbered_after_its_label():
    pois = [poi(1, street="Main St"), poi(2, street="Main St")]
    suffixes, resolved = distinguishing_suffixes(pois)
    assert not resolved and set(suffixes.values()) == {"Main St, #1", "Main St, #2"}


def test_unique_names_are_never_suffixed_and_off_means_off():
    pois = [poi(1, "Cafe", street="X"), poi(2, "Aldi", street="A"), poi(3, "Aldi", street="B")]
    assign_zone_names(pois, "", True)
    assert pois[0]["zone_name"] == "Cafe"
    assign_zone_names(pois, "", False)
    assert [p["zone_name"] for p in pois] == ["Cafe", "Aldi", "Aldi"]


def test_prefix_combines_with_suffix():
    pois = [poi(1, street="A St"), poi(2, street="B St")]
    assign_zone_names(pois, "Groceries", True)
    assert [p["zone_name"] for p in pois] == ["Groceries_Aldi (A St)", "Groceries_Aldi (B St)"]


def test_lookup_is_requested_only_for_groups_tags_cannot_resolve():
    pois = [
        poi(1, "Aldi", street="A St"), poi(2, "Aldi", street="B St"),  # resolved by tags
        poi(3, "Kroger"), poi(4, "Kroger"),  # not resolvable
        poi(5, "Target"),  # alone
    ]
    assert sorted(pois_needing_lookup(pois, "")) == ["node/3", "node/4"]


# -- business-name queries --------------------------------------------------
def test_business_regex_has_no_special_characters():
    assert business_name_regex("Trader Joe's") == "Trader Joe.s"
    assert business_name_regex("  Dick's   Sporting (Goods) ") == "Dick.s Sporting .Goods."
    assert business_name_regex('A"B\\C') == "A.B.C"


def test_business_query_is_case_insensitive_name_or_brand_and_only_real_places():
    q = build_business_query("Walmart", 34.0, -84.0, 25000)
    assert '["name"~"(^|[^[:alnum:]])Walmart([^[:alnum:]]|$)",i]' in q and '["brand"~' in q
    assert '["shop"]' in q and '["amenity"]' in q and "(33.77" in q
    assert "parking" in q  # parking lots are excluded
    assert q.endswith("out tags center;")


def test_business_pattern_matches_whole_words_only():
    import re
    q = build_business_query("Aldi", 34.0, -84.0, 5000)
    pat = re.search(r'\["name"~"(.*?)",i\]', q).group(1).replace("[:alnum:]", "a-zA-Z0-9")
    rx = re.compile(pat, re.I)
    assert rx.search("ALDI") and rx.search("Aldi Foods") and rx.search("The Aldi")
    assert not rx.search("Hughes Spalding Stadium") and not rx.search("Aldington")
