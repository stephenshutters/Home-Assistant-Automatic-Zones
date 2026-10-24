# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-24

### Added
- **Active / passive toggle** per entry (setup and options), with an explanation of the difference. Defaults to active, as before. Changing it updates the existing zones in place.
- **Unambiguous location setup**: city + optional state + country, or ZIP / postal code + country (Nominatim structured search). A picker appears when several places match. The resolved coordinates are stored, so nothing is re-geocoded at runtime.
- **Footprint-based zone radius**: smallest circle around the OSM building outline plus attached parking lots, centred on that circle, padded 20 m, clamped to per-type limits. Runs in the background; cached 30 days. Can be turned off.
- English translations (the original shipped none, so the setup form showed raw field keys).
- `poi_zones.refresh` service description; it now bypasses all caches.
- Sensor: `zone_entity_ids` (from the entity registry), `zone_names`, per-POI `zone_radius_m` and `radius_source`, `passive`, sizing progress.

### Changed
- **Zone name prefix is optional.** Without one, zones are named after the place. Places sharing a name all keep it; the new **Tell same-named places apart** switch (off by default) adds the street or OSM id instead.
- Zones are tracked by id, not name; unload/reload no longer deletes them (entry removal does).
- Zones are only written when something changed.
- Duplicate detection now needs the same name within 75 m (previously any two POIs within ~11 m collapsed, even different businesses).
- Overpass requests are rate-limited globally with a fallback mirror; HTTP-200 error "remarks" are treated as failures.
- Large sensor attributes are excluded from the recorder.
- Requires Home Assistant 2024.11+. `zone` is now a declared dependency.

### Fixed
- A hand-made zone with the same name as a POI was overwritten, and deleted when the entry unloaded.
- The `refresh` service raised `TypeError` once an Overpass lock was stored in the same dict as the entries.
- A failed or empty Overpass answer can no longer cause zones to be deleted.
- Emptying the prefix in the options form now actually clears it.

### Migration
- Version 1 entries are upgraded to version 2 and keep active zones and the fixed radius.

## [1.1.0] - 2026-02-05

### Added
- **Zone name prefix/customization** (REQUIRED): Configure custom prefixes for zone entities to avoid cluttering the entity list
  - **Prefix is now required** for better entity organization
  - Simple default: just the POI type (e.g., `hospital`)
  - Fully customizable to your preference (e.g., `nyc_hospital`, `home_hospital`, `my_hospital`)
  - Results in clean entity names: `zone.{prefix}_{poi_name}`
  - Example: prefix "hospital" → `zone.hospital_crouse_hospital`
  - Example: prefix "syracuse_hospital" → `zone.syracuse_hospital_crouse_hospital`
  - Prevents 2000+ unprefixed entities
  - **Prioritizes user customization** over automatic naming
- GitHub Actions for automated releases
  - Automatic release creation on version tags
  - Auto-packaged integration ZIP files
  - Consistent release notes

### Changed
- Zone entity naming now includes configurable prefix by default
- Improved zone ID generation for better organization
- Updated sensor attributes to include both original and prefixed names

### Fixed
- Zone entity organization and naming consistency

## [1.0.0] - 2026-02-04

### Added
- Initial release
- 40+ POI types from OpenStreetMap
- Automatic zone creation
- Dynamic radius sizing for healthcare facilities (130m-280m)
- Generic automation templates
- Weekly auto-updates
- Hospital visit notification automation example
- Support for:
  - Healthcare (hospitals, urgent care, clinics, pharmacies, dentists, vets)
  - Education (schools, universities, daycares)
  - Transportation (airports, train stations, gas stations, EV charging)
  - Food & Drink (restaurants, fast food, cafes, bars, grocery stores)
  - Services (banks, post offices, libraries, hotels)
  - Public Safety (police, fire stations)
  - Recreation (parks, gyms, movie theaters)
  - And more!

[1.1.0]: https://github.com/DEADSEC-SECURITY/ha-poi-zones/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/DEADSEC-SECURITY/ha-poi-zones/releases/tag/v1.0.0
