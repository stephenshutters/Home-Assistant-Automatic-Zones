"""Constants for POI Zones integration."""
from typing import Final

DOMAIN: Final = "poi_zones"

# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------
CONF_POI_TYPE: Final = "poi_type"

# Location (setup) -- either city/state/country or postal code/country.
CONF_CITY: Final = "city"
CONF_STATE: Final = "state"
CONF_COUNTRY: Final = "country"
CONF_POSTAL_CODE: Final = "postal_code"
# Resolved by Nominatim during setup and stored so we never re-geocode.
CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"
CONF_PLACE_NAME: Final = "place_name"  # short human label, e.g. "Roswell, Georgia"

# Zone behaviour
CONF_ZONE_PREFIX: Final = "zone_prefix"  # optional
CONF_PASSIVE: Final = "passive"
CONF_AUTO_RADIUS: Final = "auto_radius"
CONF_DISTINGUISH_DUPLICATES: Final = "distinguish_duplicates"
CONF_SEARCH_RADIUS: Final = "search_radius"  # km around the resolved location
CONF_ZONE_RADIUS: Final = "zone_radius"  # m, fallback when no OSM footprint

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SEARCH_RADIUS: Final = 25  # km (city search)
DEFAULT_SEARCH_RADIUS_POSTAL: Final = 10  # km (postal-code search)
DEFAULT_ZONE_RADIUS: Final = 150  # m, only used if a type has no radius
DEFAULT_PASSIVE: Final = False  # False == active zones, same as upstream
DEFAULT_AUTO_RADIUS: Final = True
DEFAULT_DISTINGUISH_DUPLICATES: Final = False  # same name -> same zone name

# ---------------------------------------------------------------------------
# External APIs
# ---------------------------------------------------------------------------
# Nominatim and Overpass both require an identifying User-Agent. Replace the
# URL with the home page of YOUR fork before publishing it.
USER_AGENT: Final = (
    "HomeAssistant-POIZones/2.0 (+https://github.com/stephenshutters/Home-Assistant-Automatic-Zones)"
)
NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/search"
# Tried in order; the next one is used if a request fails.
OVERPASS_URLS: Final = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
REQUEST_TIMEOUT: Final = 150  # seconds, per Overpass request

# ---------------------------------------------------------------------------
# Refresh / caching
# ---------------------------------------------------------------------------
UPDATE_INTERVAL: Final = 7 * 24 * 60 * 60  # POIs rarely change: weekly
POI_CACHE_TTL: Final = 24 * 60 * 60  # reuse the raw POI list for 24 h
RADIUS_CACHE_TTL: Final = 30 * 24 * 60 * 60  # footprints change rarely: 30 d
REQUEST_STAGGER_DELAY: Final = 10  # s between consecutive Overpass requests
MAX_RETRIES: Final = 2
RETRY_DELAY: Final = 30  # s

# ---------------------------------------------------------------------------
# Footprint-based zone sizing
# ---------------------------------------------------------------------------
# Bump when the sizing algorithm changes so cached radii are recomputed.
RADIUS_ALGORITHM_VERSION: Final = 1
GEOMETRY_BATCH_SIZE: Final = 40  # POIs per Overpass geometry request
PARKING_ATTACH_DISTANCE: Final = 30  # m: lot counts as part of the site
RADIUS_PADDING: Final = 20  # m added to the enclosing circle (GPS jitter)
MIN_ZONE_RADIUS: Final = 50  # m, phone GPS is rarely better than this
RADIUS_ROUNDING: Final = 5  # round radius up to a multiple of this (m)
# Max radius = max(MAX_RADIUS_FLOOR, MAX_RADIUS_FACTOR * type default radius).
MAX_RADIUS_FLOOR: Final = 150
MAX_RADIUS_FACTOR: Final = 3
# Parking tags that are not an off-street lot belonging to the building.
EXCLUDED_PARKING_ACCESS: Final = frozenset({"private", "no", "residents", "delivery"})
EXCLUDED_PARKING_TYPES: Final = frozenset(
    {"street_side", "lane", "on_kerb", "half_on_kerb", "shoulder", "underground", "garage_boxes"}
)

# Signals / storage
SIGNAL_ZONES_SYNCED: Final = f"{DOMAIN}_zones_synced_{{}}"
STORAGE_VERSION: Final = 1

# POI Types - individual types only
POI_TYPES: Final = {
    "hospital": {
        "name": "Hospitals",
        "icon": "mdi:hospital-building",
        "radius": 200,
        "filters": [
            {"amenity": "hospital"},
            {"healthcare": "hospital"},
        ],
    },
    "clinic": {
        "name": "Clinics",
        "icon": "mdi:medical-bag",
        "radius": 150,
        "filters": [
            {"amenity": "clinic"},
            {"healthcare": "clinic"},
        ],
    },
    "urgent_care": {
        "name": "Urgent Care",
        "icon": "mdi:ambulance",
        "radius": 150,
        "filters": [
            {"healthcare": "urgent_care"},
            {"amenity": "clinic", "urgent_care": "yes"},
            {"amenity": "clinic", "healthcare:speciality": "urgent_care"},
        ],
    },
    "pharmacy": {
        "name": "Pharmacies",
        "icon": "mdi:pharmacy",
        "radius": 100,
        "filters": [
            {"amenity": "pharmacy"},
            {"healthcare": "pharmacy"},
        ],
    },
    "doctor": {
        "name": "Doctors Offices",
        "icon": "mdi:stethoscope",
        "radius": 100,
        "filters": [
            {"amenity": "doctors"},
            {"healthcare": "doctor"},
        ],
    },
    "school": {
        "name": "Schools (K-12)",
        "icon": "mdi:school",
        "radius": 200,
        "filters": [
            {"amenity": "school"},
        ],
    },
    "university": {
        "name": "Universities & Colleges",
        "icon": "mdi:school-outline",
        "radius": 300,
        "filters": [
            {"amenity": "university"},
            {"amenity": "college"},
        ],
    },
    "childcare": {
        "name": "Childcare & Daycares",
        "icon": "mdi:baby-carriage",
        "radius": 100,
        "filters": [
            {"amenity": "childcare"},
            {"amenity": "kindergarten"},
        ],
    },
    "grocery": {
        "name": "Grocery Stores",
        "icon": "mdi:cart",
        "radius": 150,
        "filters": [
            {"shop": "supermarket"},
            {"shop": "grocery"},
        ],
    },
    "gas_station": {
        "name": "Gas Stations",
        "icon": "mdi:gas-station",
        "radius": 100,
        "filters": [
            {"amenity": "fuel"},
        ],
    },
    "gym": {
        "name": "Gyms & Fitness Centers",
        "icon": "mdi:dumbbell",
        "radius": 150,
        "filters": [
            {"leisure": "fitness_centre"},
            {"leisure": "sports_centre"},
        ],
    },
    "restaurant": {
        "name": "Restaurants",
        "icon": "mdi:silverware-fork-knife",
        "radius": 75,
        "filters": [
            {"amenity": "restaurant"},
        ],
    },
    "fast_food": {
        "name": "Fast Food",
        "icon": "mdi:hamburger",
        "radius": 75,
        "filters": [
            {"amenity": "fast_food"},
        ],
    },
    "cafe": {
        "name": "Cafes & Coffee Shops",
        "icon": "mdi:coffee",
        "radius": 75,
        "filters": [
            {"amenity": "cafe"},
        ],
    },
    "bar": {
        "name": "Bars & Pubs",
        "icon": "mdi:glass-mug-variant",
        "radius": 75,
        "filters": [
            {"amenity": "bar"},
            {"amenity": "pub"},
        ],
    },
    "bank": {
        "name": "Banks",
        "icon": "mdi:bank",
        "radius": 100,
        "filters": [
            {"amenity": "bank"},
        ],
    },
    "police": {
        "name": "Police Stations",
        "icon": "mdi:police-badge",
        "radius": 150,
        "filters": [
            {"amenity": "police"},
        ],
    },
    "fire_station": {
        "name": "Fire Stations",
        "icon": "mdi:fire-truck",
        "radius": 150,
        "filters": [
            {"amenity": "fire_station"},
        ],
    },
    "library": {
        "name": "Libraries",
        "icon": "mdi:library",
        "radius": 150,
        "filters": [
            {"amenity": "library"},
        ],
    },
    "post_office": {
        "name": "Post Offices",
        "icon": "mdi:mailbox",
        "radius": 100,
        "filters": [
            {"amenity": "post_office"},
        ],
    },
    "place_of_worship": {
        "name": "Places of Worship",
        "icon": "mdi:church",
        "radius": 150,
        "filters": [
            {"amenity": "place_of_worship"},
        ],
    },
    "park": {
        "name": "Parks",
        "icon": "mdi:tree",
        "radius": 200,
        "filters": [
            {"leisure": "park"},
        ],
    },
    "airport": {
        "name": "Airports",
        "icon": "mdi:airplane",
        "radius": 500,
        "filters": [
            {"aeroway": "aerodrome"},
        ],
    },
    "train_station": {
        "name": "Train Stations",
        "icon": "mdi:train",
        "radius": 200,
        "filters": [
            {"railway": "station"},
        ],
    },
    "light_rail_station": {
        "name": "Light Rail Stations",
        "icon": "mdi:tram-side",
        "radius": 60,
        "filters": [
            {"station": "light-rail"},
        ],
    },
    "ev_charging": {
        "name": "EV Charging Stations",
        "icon": "mdi:ev-station",
        "radius": 100,
        "filters": [
            {"amenity": "charging_station"},
        ],
    },
    "car_wash": {
        "name": "Car Washes",
        "icon": "mdi:car-wash",
        "radius": 100,
        "filters": [
            {"amenity": "car_wash"},
        ],
    },
    "car_repair": {
        "name": "Auto Repair Shops",
        "icon": "mdi:car-wrench",
        "radius": 100,
        "filters": [
            {"shop": "car_repair"},
        ],
    },
    "hotel": {
        "name": "Hotels",
        "icon": "mdi:bed",
        "radius": 150,
        "filters": [
            {"tourism": "hotel"},
            {"tourism": "motel"},
        ],
    },
    "cinema": {
        "name": "Movie Theaters",
        "icon": "mdi:movie-open",
        "radius": 150,
        "filters": [
            {"amenity": "cinema"},
        ],
    },
    "veterinary": {
        "name": "Veterinary Clinics",
        "icon": "mdi:paw",
        "radius": 100,
        "filters": [
            {"amenity": "veterinary"},
        ],
    },
    "dentist": {
        "name": "Dentists",
        "icon": "mdi:tooth",
        "radius": 100,
        "filters": [
            {"amenity": "dentist"},
            {"healthcare": "dentist"},
        ],
    },
    "nursing_home": {
        "name": "Nursing Homes",
        "icon": "mdi:home-heart",
        "radius": 150,
        "filters": [
            {"amenity": "nursing_home"},
            {"healthcare": "nursing_home"},
        ],
    },
}

# For easy dropdown selection
POI_TYPE_OPTIONS: Final = {k: v["name"] for k, v in POI_TYPES.items()}
