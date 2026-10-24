"""Config flow for Automatic Zones.

Setup steps:  POI type -> (menu) city/state/country OR ZIP/postal code
              -> pick the right place if several match -> zone settings.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    CountrySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_AUTO_RADIUS,
    CONF_CITY,
    CONF_COUNTRY,
    CONF_DISTINGUISH_DUPLICATES,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PASSIVE,
    CONF_PLACE_NAME,
    CONF_POI_TYPE,
    CONF_POSTAL_CODE,
    CONF_SEARCH_RADIUS,
    CONF_STATE,
    CONF_ZONE_PREFIX,
    CONF_ZONE_RADIUS,
    DEFAULT_AUTO_RADIUS,
    DEFAULT_DISTINGUISH_DUPLICATES,
    DEFAULT_PASSIVE,
    DEFAULT_SEARCH_RADIUS,
    DEFAULT_SEARCH_RADIUS_POSTAL,
    DOMAIN,
    POI_TYPES,
)
from .helpers import entry_value
from .osm import OSMConnectionError, Place, async_geocode

_LOGGER = logging.getLogger(__name__)


def _search_radius_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=1, max=100, step=1, unit_of_measurement="km", mode=NumberSelectorMode.BOX)
    )


def _zone_radius_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=25, max=1000, step=25, unit_of_measurement="m", mode=NumberSelectorMode.BOX)
    )


class POIZonesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for POI Zones."""

    VERSION = 2

    def __init__(self) -> None:
        self._poi_type: str | None = None
        self._postal_mode = False
        self._location_fields: dict[str, str] = {}
        self._places: list[Place] = []
        self._place: Place | None = None

    # -- 1. what kind of place ----------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._poi_type = user_input[CONF_POI_TYPE]
            return await self.async_step_location_method()

        options = [
            {"value": key, "label": cfg["name"]}
            for key, cfg in sorted(POI_TYPES.items(), key=lambda kv: kv[1]["name"])
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POI_TYPE): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    # -- 2. where: city/state/country or postal code -------------------------
    async def async_step_location_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="location_method", menu_options=["city", "postal"])

    async def async_step_city(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._postal_mode = False
        errors: dict[str, str] = {}
        if user_input is not None:
            self._location_fields = {
                CONF_CITY: user_input[CONF_CITY].strip(),
                CONF_STATE: (user_input.get(CONF_STATE) or "").strip(),
                CONF_COUNTRY: user_input[CONF_COUNTRY],
            }
            if not self._location_fields[CONF_CITY]:
                errors[CONF_CITY] = "city_required"
            elif not (errors := await self._async_lookup()):
                return await self._async_after_lookup()

        schema = vol.Schema(
            {
                vol.Required(CONF_CITY): TextSelector(),
                vol.Optional(CONF_STATE): TextSelector(),
                vol.Required(CONF_COUNTRY): CountrySelector(),
            }
        )
        suggested = user_input or {CONF_COUNTRY: self.hass.config.country}
        return self.async_show_form(
            step_id="city",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_postal(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._postal_mode = True
        errors: dict[str, str] = {}
        if user_input is not None:
            self._location_fields = {
                CONF_POSTAL_CODE: user_input[CONF_POSTAL_CODE].strip(),
                CONF_COUNTRY: user_input[CONF_COUNTRY],
            }
            if not self._location_fields[CONF_POSTAL_CODE]:
                errors[CONF_POSTAL_CODE] = "postal_required"
            elif not (errors := await self._async_lookup()):
                return await self._async_after_lookup()

        schema = vol.Schema(
            {
                vol.Required(CONF_POSTAL_CODE): TextSelector(),
                vol.Required(CONF_COUNTRY): CountrySelector(),
            }
        )
        suggested = user_input or {CONF_COUNTRY: self.hass.config.country}
        return self.async_show_form(
            step_id="postal",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def _async_lookup(self) -> dict[str, str]:
        """Geocode the entered location. Returns form errors (empty = ok)."""
        fields = self._location_fields
        try:
            self._places = await async_geocode(
                async_get_clientsession(self.hass),
                country_code=fields[CONF_COUNTRY],
                city=fields.get(CONF_CITY),
                state=fields.get(CONF_STATE) or None,
                postal_code=fields.get(CONF_POSTAL_CODE),
                language=self.hass.config.language,
            )
        except OSMConnectionError as err:
            _LOGGER.warning("Nominatim lookup failed: %s", err)
            return {"base": "cannot_connect"}
        if not self._places:
            return {"base": "location_not_found"}
        return {}

    async def _async_after_lookup(self) -> ConfigFlowResult:
        if len(self._places) == 1:
            return await self._async_select_place(self._places[0])
        return await self.async_step_pick()

    # -- 3. several matches: let the user choose ----------------------------
    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_select_place(self._places[int(user_input["place"])])
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required("place", default="0"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": str(i), "label": p.display_name}
                                for i, p in enumerate(self._places)
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def _async_select_place(self, place: Place) -> ConfigFlowResult:
        self._place = place
        await self.async_set_unique_id(f"{self._poi_type}_{place.unique_key}")
        self._abort_if_unique_id_configured()
        return await self.async_step_settings()

    # -- 4. zone settings ----------------------------------------------------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._poi_type and self._place
        poi_config = POI_TYPES[self._poi_type]

        if user_input is not None:
            data = {
                CONF_POI_TYPE: self._poi_type,
                CONF_LATITUDE: self._place.latitude,
                CONF_LONGITUDE: self._place.longitude,
                CONF_PLACE_NAME: self._place.short_name,
                **{k: v for k, v in self._location_fields.items() if v},
                CONF_ZONE_PREFIX: (user_input.get(CONF_ZONE_PREFIX) or "").strip(),
                CONF_PASSIVE: user_input[CONF_PASSIVE],
                CONF_DISTINGUISH_DUPLICATES: user_input[CONF_DISTINGUISH_DUPLICATES],
                CONF_AUTO_RADIUS: user_input[CONF_AUTO_RADIUS],
                CONF_SEARCH_RADIUS: int(user_input[CONF_SEARCH_RADIUS]),
                CONF_ZONE_RADIUS: int(user_input[CONF_ZONE_RADIUS]),
            }
            return self.async_create_entry(
                title=f"{poi_config['name']} - {self._place.short_name}", data=data
            )

        default_search = DEFAULT_SEARCH_RADIUS_POSTAL if self._postal_mode else DEFAULT_SEARCH_RADIUS
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ZONE_PREFIX): TextSelector(),
                    vol.Required(CONF_PASSIVE, default=DEFAULT_PASSIVE): BooleanSelector(),
                    vol.Required(
                        CONF_DISTINGUISH_DUPLICATES, default=DEFAULT_DISTINGUISH_DUPLICATES
                    ): BooleanSelector(),
                    vol.Required(CONF_AUTO_RADIUS, default=DEFAULT_AUTO_RADIUS): BooleanSelector(),
                    vol.Required(CONF_SEARCH_RADIUS, default=default_search): _search_radius_selector(),
                    vol.Required(CONF_ZONE_RADIUS, default=poi_config["radius"]): _zone_radius_selector(),
                }
            ),
            description_placeholders={
                "poi_type": poi_config["name"],
                "place": self._place.display_name,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return POIZonesOptionsFlow()


class POIZonesOptionsFlow(OptionsFlow):
    """Change zone behaviour after setup (renames/updates zones in place)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            # An emptied optional field is simply absent from the submission;
            # store "" so it overrides the prefix chosen at setup.
            user_input[CONF_ZONE_PREFIX] = (user_input.get(CONF_ZONE_PREFIX) or "").strip()
            user_input[CONF_SEARCH_RADIUS] = int(user_input[CONF_SEARCH_RADIUS])
            user_input[CONF_ZONE_RADIUS] = int(user_input[CONF_ZONE_RADIUS])
            return self.async_create_entry(title="", data=user_input)

        poi_config = POI_TYPES[entry.data[CONF_POI_TYPE]]
        schema = vol.Schema(
            {
                vol.Optional(CONF_ZONE_PREFIX): TextSelector(),
                vol.Required(
                    CONF_PASSIVE, default=entry_value(entry, CONF_PASSIVE, DEFAULT_PASSIVE)
                ): BooleanSelector(),
                vol.Required(
                    CONF_DISTINGUISH_DUPLICATES,
                    default=entry_value(
                        entry, CONF_DISTINGUISH_DUPLICATES, DEFAULT_DISTINGUISH_DUPLICATES
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_AUTO_RADIUS, default=entry_value(entry, CONF_AUTO_RADIUS, DEFAULT_AUTO_RADIUS)
                ): BooleanSelector(),
                vol.Required(
                    CONF_SEARCH_RADIUS,
                    default=entry_value(entry, CONF_SEARCH_RADIUS, DEFAULT_SEARCH_RADIUS),
                ): _search_radius_selector(),
                vol.Required(
                    CONF_ZONE_RADIUS,
                    default=entry_value(entry, CONF_ZONE_RADIUS, poi_config["radius"]),
                ): _zone_radius_selector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_ZONE_PREFIX: entry_value(entry, CONF_ZONE_PREFIX, "")}
            ),
            description_placeholders={"place": entry.data.get(CONF_PLACE_NAME, "")},
        )
