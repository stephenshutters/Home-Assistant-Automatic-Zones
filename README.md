# Automatic Zones for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

Automatically create Home Assistant zones for Points of Interest (POIs) near your location using [OpenStreetMap](https://www.openstreetmap.org/about) data.

## Features

- 🏥 **40+ POI Types**: Hospitals, urgent care, schools, restaurants, gas stations, and more
- 📍 **Automatic Zone Creation**: Creates Home Assistant zones for all discovered POIs
- 🎯 **Footprint-Based Zone Sizing**: Each zone is fitted around the OpenStreetMap outline of the building plus its attached parking lot
- 🧭 **Unambiguous Setup**: city + state + country, or a ZIP / postal code + country
- 🔀 **Active or Passive Zones**: One switch per entry, changeable any time
- 🔄 **Auto-Updates**: Refreshes POI data weekly
- 🌍 **OpenStreetMap**: Uses free OpenStreetMap data via Overpass API
- 🔔 **Automation Ready**: Easily create automations when people enter POI zones

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the 3 dots in the top right
3. Select "Custom repositories"
4. Add this repository URL: `https://github.com/stephenshutters/Home-Assistant-Automatic-Zones`
5. Category: Integration
6. Click "Install"
7. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/poi_zones` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration** and search for "POI Zones"
3. Choose the **POI type**
4. Choose how to identify the place:
   - **City, state and country** (state is optional but recommended - there are dozens of Springfields), or
   - **ZIP / postal code and country** (a postal code is only unique within a country)
5. If more than one place matches you are asked to pick the right one
6. Zone settings:

| Setting | What it does |
|---|---|
| **Zone name prefix** (optional) | Empty: zones are named after the place (`Starbucks`). With a prefix: `prefix_Starbucks`. |
| **Make all zones passive** | See below. |
| **Size zones from OpenStreetMap footprints** | On by default. See [Zone sizing](#zone-sizing). |
| **Search radius** | How far from the place to look (default 25 km for a city, 10 km for a ZIP). |
| **Fallback zone radius** | Used when OpenStreetMap has no outline for a place (or sizing is off). |

Every setting except the location can be changed later under **Configure**. Zones are renamed, resized and switched between active/passive **in place** - they are not recreated, so entity ids and automations keep working.

### Active vs. passive zones

- **Active** zones replace a person's state with the zone's name (e.g. `Starbucks` instead of `not_home`) while they are inside, and the person's location in the UI follows. Good when you want place names as states.
- **Passive** zones never change a person's state, but zone triggers, zone conditions and the zone's `persons` attribute all still work. Good when you create many zones or want `home`/`not_home` untouched.

With active zones, names are shown verbatim as the person's state, so choose a prefix (or none) deliberately.

### Duplicate names

By default places that share a name keep **exactly that name**: twenty Starbucks are twenty zones all called `Starbucks` (Home Assistant gives them distinct entity ids - `zone.starbucks`, `zone.starbucks_2`, ... - and `zone_entity_ids` on the sensor lists the real ones). With active zones a person standing in any of them has the state `Starbucks`.

Turn on **Tell same-named places apart** to get `Starbucks (Holcomb Bridge Rd)` instead - the street, or the OpenStreetMap id when streets are missing or identical. Uniquely named places are never suffixed. The setting works the same with or without a prefix and can be switched any time; zones are renamed in place.

## Available POI Types

### Healthcare
- Hospitals (with dynamic sizing)
- Urgent Care (with dynamic sizing)
- Clinics
- Pharmacies
- Doctor's Offices
- Dentists
- Veterinary Clinics
- Nursing Homes

### Education
- Schools (K-12)
- Universities & Colleges
- Childcare & Daycares

### Transportation
- Airports
- Train Stations
- Gas Stations
- EV Charging Stations
- Car Washes
- Auto Repair Shops

### Food & Drink
- Restaurants
- Fast Food
- Cafes & Coffee Shops
- Bars & Pubs
- Grocery Stores

### Services
- Banks
- Post Offices
- Libraries
- Hotels

### Public Safety
- Police Stations
- Fire Stations

### Recreation
- Parks
- Movie Theaters
- Gyms & Fitness Centers

### Religious
- Places of Worship

## Zone sizing

HA zones are circles, so for each place the integration computes the **smallest circle that contains the building outline and every attached parking lot**, adds a 20 m margin for GPS jitter, and centres the zone on that circle (so the zone can sit a little off the OSM point, toward the car park).

- The building is the place's own outline, or - if it is mapped as a point - the smallest `building` polygon that contains it.
- A parking lot (`amenity=parking`) counts as attached if it is within 30 m of that outline. Private/resident/delivery lots and street, underground and garage-box parking are ignored.
- Radius is rounded up to 5 m, never below 50 m, and never above `max(150 m, 3 x the type's default radius)`. If adding the parking would exceed that cap (shared mall lots) the zone is sized from the building alone.
- A place with no usable outline (e.g. a point not inside any building) uses the fallback radius. Hospitals and urgent care keep the original bed-count/type heuristic as that fallback.
- Sizing runs in the **background** after setup (a request per 40 places, at most one every 10 s), so zones first appear with the fallback radius and are resized as results arrive. Results are cached for 30 days. The sensor attributes `zones_sized_from_osm` / `zones_pending_size` show progress; each location lists its `radius_source`.

## Sensors

Each entry creates one sensor.

- **State**: number of places found
- **Attributes**: `zone_entity_ids`, `zone_names`, `locations` (name, `zone_entity_id`, position, `zone_radius_m`, `radius_source`, address, phone, website, ...), `passive`, `auto_radius`, `zones_sized_from_osm`, `zones_pending_size`, plus `city`/`place`, `poi_type`, `search_radius_km`.
- `zone_entity_ids` comes from the entity registry, so it is correct even when Home Assistant had to add `_2` to avoid a clash with one of your own zones.
- The large attributes are excluded from the recorder database (they would exceed its 16 KB limit and cause *all* attributes to be dropped).

## Services

`poi_zones.refresh` - re-query OpenStreetMap now, ignoring caches, and resize zones again.

## Zones you create yourself

The integration tracks the zones it creates by id. It never edits or deletes a zone you made, even if the names match (yours keeps `zone.walmart`, ours becomes `zone.walmart_2`). Unloading, reloading or disabling the entry leaves its zones in place; **deleting the entry removes them**.

## Example Automations

### Hospital Visit Notification

(This example relies on **active** zones: it matches a person's state against the zone names. With passive zones use a zone trigger or the zone's `persons` attribute instead.)

Get notified when someone has been at a hospital for over 30 minutes:

```yaml
automation:
  - alias: "Hospital Visit Alert"
    trigger:
      - platform: template
        value_template: >
          {% set poi_zones = namespace(ids=[]) %}
          {% for sensor in states.sensor | selectattr('attributes.poi_type', 'defined') %}
            {% if sensor.attributes.poi_type in ['hospital', 'urgent_care'] %}
              {% set poi_zones.ids = poi_zones.ids + sensor.attributes.zone_names %}
            {% endif %}
          {% endfor %}
          {{ expand(states.person)
             | selectattr('state', 'in', poi_zones.ids)
             | list | count > 0 }}
        for:
          minutes: 30
    action:
      - service: notify.notify
        data:
          title: "🏥 Healthcare Visit Alert"
          message: >
            {% set poi_zones = namespace(ids=[]) %}
            {% for sensor in states.sensor | selectattr('attributes.poi_type', 'defined') %}
              {% if sensor.attributes.poi_type in ['hospital', 'urgent_care'] %}
                {% set poi_zones.ids = poi_zones.ids + sensor.attributes.zone_names %}
              {% endif %}
            {% endfor %}
            {% set people_at_facility = expand(states.person)
               | selectattr('state', 'in', poi_zones.ids)
               | list %}
            {% for person in people_at_facility %}
              {{ person.name }} has been at {{ state_attr('zone.' + person.state, 'friendly_name') }} for over 30 minutes.
            {% endfor %}
```

### School Arrival Notification

Get notified when kids arrive at school:

```yaml
automation:
  - alias: "School Arrival"
    trigger:
      - platform: zone
        entity_id: person.child
        zone: zone.school_name
        event: enter
    action:
      - service: notify.mobile_app
        data:
          message: "{{ trigger.to_state.attributes.friendly_name }} arrived at school"
```

## Data Source

This integration uses:
- **Nominatim** (structured search) for geocoding the place you enter
- **Overpass API** for querying OpenStreetMap POI data

Data is refreshed weekly by default; zone footprints are looked up in the background. All data comes from OpenStreetMap contributors and is available under the ODbL license.

## Troubleshooting

### No POIs Found

- Check the spelling, or switch to the ZIP / postal-code option
- Some POI types may not be well-tagged in OpenStreetMap for your area
- Try increasing the search radius

### Zones Not Created

- Ensure the "Zone" integration is enabled in Home Assistant
- Check Home Assistant logs for errors
- Zones are created on setup and re-synced after every refresh

### Zones Never Get Resized

- Sizing needs the Overpass API; check the log for `Footprint lookup failed`. It retries on the next refresh, or run `poi_zones.refresh`.
- Places with no building outline in OSM keep the fallback radius by design.

### Wrong Zone Locations

- OpenStreetMap data quality varies by location
- Building entrances vs centers may cause slight offsets
- Zones are centred on the building+parking footprint, not the OSM point

## Contributing

Contributions are welcome! Please open an issue or pull request on GitHub.

## License

This project is licensed under the MIT License.

## Credits

- OpenStreetMap contributors for POI data
- Nominatim for geocoding
- Overpass API for OSM queries

## Upgrading from the original integration

Existing entries are migrated automatically and keep their behaviour (active zones, fixed radius, same names). Their zones are adopted by name + position rather than duplicated. Turn on footprint sizing or passive zones under **Configure**. Note that the original deleted its zones whenever an entry was unloaded or reloaded; this version keeps them.

## Development

`pip install -r requirements-test.txt && pytest`
