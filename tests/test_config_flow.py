"""Config flow: city/state/country, ZIP, optional prefix, passive toggle."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.automatic_zones.const import DOMAIN
from custom_components.automatic_zones.osm import OSMConnectionError, Place

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ROSWELL = Place(34.0232, -84.3616, "Roswell, Fulton County, Georgia, United States", "Roswell, Georgia", "relation", 111)
ROSWELL_NM = Place(33.39, -104.52, "Roswell, Chaves County, New Mexico, United States", "Roswell, New Mexico", "relation", 222)

GEOCODE = "custom_components.automatic_zones.config_flow.async_geocode"
SETUP = "custom_components.automatic_zones.async_setup_entry"


async def _to_location_menu(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"poi_type": "grocery"})
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"city", "postal"}
    return result


async def test_city_flow_creates_entry_with_optional_prefix_empty(hass):
    result = await _to_location_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "city"})
    assert result["step_id"] == "city"
    with patch(GEOCODE, AsyncMock(return_value=[ROSWELL])) as geocode:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"city": "Roswell", "state": "Georgia", "country": "US"}
        )
    kwargs = geocode.call_args.kwargs
    assert (kwargs["city"], kwargs["state"], kwargs["country_code"]) == ("Roswell", "Georgia", "US")
    assert result["step_id"] == "settings"
    assert "Roswell" in result["description_placeholders"]["place"]

    schema_keys = {str(k): k for k in result["data_schema"].schema}
    # prefix is Optional (not Required); passive/auto_radius are present
    assert type(schema_keys["zone_prefix"]).__name__ == "Optional"
    assert "passive" in schema_keys and "auto_radius" in schema_keys
    assert schema_keys["distinguish_duplicates"].default() is False

    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"passive": True, "auto_radius": True, "search_radius": 15, "zone_radius": 150},  # no prefix
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["passive"] is True
    assert data["zone_prefix"] == ""
    assert data["distinguish_duplicates"] is False
    assert (data["latitude"], data["longitude"]) == (34.0232, -84.3616)
    assert (data["city"], data["state"], data["country"]) == ("Roswell", "Georgia", "US")
    assert result["title"] == "Grocery Stores - Roswell, Georgia"


async def test_multiple_matches_show_picker(hass):
    result = await _to_location_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "city"})
    with patch(GEOCODE, AsyncMock(return_value=[ROSWELL, ROSWELL_NM])):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"city": "Roswell", "country": "US"}
        )
    assert result["step_id"] == "pick"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "1"})
    assert result["step_id"] == "settings"
    assert "New Mexico" in result["description_placeholders"]["place"]


async def test_postal_flow(hass):
    result = await _to_location_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "postal"})
    zip_place = Place(34.0, -84.3, "30075, Roswell, Georgia, United States", "30075, Roswell, Georgia", "node", 5)
    with patch(GEOCODE, AsyncMock(return_value=[zip_place])) as geocode:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"postal_code": "30075", "country": "US"}
        )
    assert geocode.call_args.kwargs["postal_code"] == "30075"
    assert geocode.call_args.kwargs["country_code"] == "US"
    default = next(k for k in result["data_schema"].schema if str(k) == "search_radius").default()
    assert default == 10  # smaller default area for a ZIP than for a city


@pytest.mark.parametrize(
    ("side_effect", "return_value", "error"),
    [(None, [], "location_not_found"), (OSMConnectionError("down"), None, "cannot_connect")],
)
async def test_errors_keep_you_on_the_form(hass, side_effect, return_value, error):
    result = await _to_location_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "city"})
    with patch(GEOCODE, AsyncMock(side_effect=side_effect, return_value=return_value)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"city": "Nowhereville", "country": "US"}
        )
    assert result["step_id"] == "city"
    assert result["errors"] == {"base": error}


async def test_same_type_and_place_is_rejected(hass):
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await _to_location_menu(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "city"})
        with patch(GEOCODE, AsyncMock(return_value=[ROSWELL])), patch(SETUP, return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"city": "Roswell", "country": "US"}
            )
            if result["type"] is FlowResultType.FORM:
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"],
                    {"passive": False, "auto_radius": True, "search_radius": 25, "zone_radius": 150},
                )
        assert result["type"] is expected
    assert result["reason"] == "already_configured"
