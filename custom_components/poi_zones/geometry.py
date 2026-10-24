"""Small, dependency-free geometry helpers for sizing zones.

Everything here works on plain tuples so it can be unit-tested without Home
Assistant. Coordinates are WGS84 ``(lat, lon)`` unless a function says it uses
local metres ``(x, y)``.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]
XY = tuple[float, float]
Circle = tuple[float, float, float]  # (x, y, r)

_EPS = 1e-7


# ---------------------------------------------------------------------------
# Projection (local equirectangular -- fine for sites a few km across)
# ---------------------------------------------------------------------------
def project(points: Iterable[LatLon], lat0: float, lon0: float) -> list[XY]:
    """Project lat/lon to metres east/north of ``(lat0, lon0)``."""
    cos_lat0 = math.cos(math.radians(lat0))
    return [
        (
            math.radians(lon - lon0) * cos_lat0 * EARTH_RADIUS_M,
            math.radians(lat - lat0) * EARTH_RADIUS_M,
        )
        for lat, lon in points
    ]


def unproject(x: float, y: float, lat0: float, lon0: float) -> LatLon:
    """Inverse of :func:`project` for a single point."""
    cos_lat0 = math.cos(math.radians(lat0))
    return (
        lat0 + math.degrees(y / EARTH_RADIUS_M),
        lon0 + math.degrees(x / (EARTH_RADIUS_M * cos_lat0)),
    )


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# Minimum enclosing circle (randomised incremental / Welzl, iterative)
# ---------------------------------------------------------------------------
def _in_circle(c: Circle, p: XY) -> bool:
    return math.hypot(p[0] - c[0], p[1] - c[1]) <= c[2] + _EPS


def _circle_two(a: XY, b: XY) -> Circle:
    cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    return (cx, cy, math.hypot(a[0] - b[0], a[1] - b[1]) / 2)


def _circle_three(a: XY, b: XY, c: XY) -> Circle:
    """Circumscribed circle; falls back to the widest pair if collinear."""
    bx, by = b[0] - a[0], b[1] - a[1]
    cx, cy = c[0] - a[0], c[1] - a[1]
    d = 2 * (bx * cy - by * cx)
    if abs(d) < 1e-12:
        return max(
            (_circle_two(a, b), _circle_two(a, c), _circle_two(b, c)),
            key=lambda circ: circ[2],
        )
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    ux = (cy * b2 - by * c2) / d
    uy = (bx * c2 - cx * b2) / d
    return (a[0] + ux, a[1] + uy, math.hypot(ux, uy))


def min_enclosing_circle(points: Sequence[XY]) -> Circle | None:
    """Smallest circle containing every point, or ``None`` if empty."""
    pts = list(dict.fromkeys(points))  # de-duplicate, keep order
    if not pts:
        return None
    random.Random(0).shuffle(pts)  # deterministic -> reproducible zones
    circle: Circle = (pts[0][0], pts[0][1], 0.0)
    for i, p in enumerate(pts):
        if _in_circle(circle, p):
            continue
        circle = (p[0], p[1], 0.0)
        for j in range(i):
            q = pts[j]
            if _in_circle(circle, q):
                continue
            circle = _circle_two(p, q)
            for k in range(j):
                r = pts[k]
                if not _in_circle(circle, r):
                    circle = _circle_three(p, q, r)
    return circle


# ---------------------------------------------------------------------------
# Polygon helpers (rings are lists of XY; closed or open both accepted)
# ---------------------------------------------------------------------------
def is_closed_ring(ring: Sequence) -> bool:
    """True for a closed way with at least a triangle."""
    return len(ring) >= 4 and ring[0] == ring[-1]


def ring_area(ring: Sequence[XY]) -> float:
    """Absolute area (m^2) of a ring by the shoelace formula."""
    n = len(ring)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def point_in_ring(pt: XY, ring: Sequence[XY]) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _seg_point_dist(p: XY, a: XY, b: XY) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _orient(a: XY, b: XY, c: XY) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(a: XY, b: XY, c: XY, d: XY) -> bool:
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def _bbox(ring: Sequence[XY]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def ring_distance(a: Sequence[XY], b: Sequence[XY], limit: float = math.inf) -> float:
    """Minimum distance in metres between two polygons (0 if they overlap).

    ``limit`` lets the caller bail out early: if the bounding boxes are further
    apart than ``limit`` the function returns ``math.inf`` without any edge
    work, which keeps big campuses cheap.
    """
    if not a or not b:
        return math.inf
    ax0, ay0, ax1, ay1 = _bbox(a)
    bx0, by0, bx1, by1 = _bbox(b)
    gap_x = max(bx0 - ax1, ax0 - bx1, 0.0)
    gap_y = max(by0 - ay1, ay0 - by1, 0.0)
    if math.hypot(gap_x, gap_y) > limit:
        return math.inf

    # Containment (either way) or any vertex inside the other => overlap.
    if is_closed_ring(a) and any(point_in_ring(p, a) for p in b[:1]):
        return 0.0
    if is_closed_ring(b) and any(point_in_ring(p, b) for p in a[:1]):
        return 0.0

    # Work on consecutive vertex pairs only, so an open polyline (a fragment
    # of a multipolygon) never grows a false closing edge. Closed OSM rings
    # repeat their first vertex, so they are fully covered.
    segs_a = list(zip(a, a[1:])) or [(a[0], a[0])]
    segs_b = list(zip(b, b[1:])) or [(b[0], b[0])]

    # Crossing edges without contained vertices (e.g. a plus-shaped overlap).
    for a1, a2 in segs_a:
        for b1, b2 in segs_b:
            if _segments_cross(a1, a2, b1, b2):
                return 0.0

    best = math.inf
    for p in a:
        for b1, b2 in segs_b:
            best = min(best, _seg_point_dist(p, b1, b2))
    for p in b:
        for a1, a2 in segs_a:
            best = min(best, _seg_point_dist(p, a1, a2))
    return best
