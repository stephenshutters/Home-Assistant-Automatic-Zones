"""Builders for synthetic Overpass `out geom` data (metres around an origin)."""
from custom_components.automatic_zones.geometry import unproject

LAT0, LON0 = 34.0, -84.0


def ll(x, y):
    lat, lon = unproject(x, y, LAT0, LON0)
    return {"lat": lat, "lon": lon}


def rect(x0, y0, x1, y1):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [ll(x, y) for x, y in pts]


def way(wid, geometry, **tags):
    return {"type": "way", "id": wid, "geometry": geometry, "tags": tags}


def node(nid, x, y, **tags):
    p = ll(x, y)
    return {"type": "node", "id": nid, "lat": p["lat"], "lon": p["lon"], "tags": tags}


def relation(rid, outer_rects, **tags):
    return {
        "type": "relation", "id": rid, "tags": tags,
        "members": [{"type": "way", "ref": i, "role": "outer", "geometry": g} for i, g in enumerate(outer_rects)],
    }
