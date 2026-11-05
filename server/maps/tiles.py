# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiles of an area: its bounding box, which tiles cover it, how big the map will be."""

import math

EARTH_KM_PER_DEGREE = 111.32

# Web Mercator: the tile rows stop at this latitude
MAX_LAT = 85.05112878

# the scale a zoom level draws at 96 dpi, at latitude 45 (what the admin reads to choose)
SCALES = {7: 3_270_000, 8: 1_630_000, 9: 817_000, 10: 409_000, 11: 204_000, 12: 102_000}

# bytes per tile of the basemap without the heavy layers, measured on 100 km around
# Trento (z0-10: 2.3 MB in 109 tiles, z0-11: 4.9 MB in 365 tiles): a rough guide
BYTES_PER_TILE = {9: 25_000, 10: 12_500, 11: 10_000, 12: 9_000}
BYTES_PER_LOW_TILE = 35_000


def bbox(lat, lon, radius_km):
    """[min lon, min lat, max lon, max lat] of the square around a point."""
    dlat = radius_km / EARTH_KM_PER_DEGREE
    dlon = radius_km / (EARTH_KM_PER_DEGREE * max(0.05, math.cos(math.radians(lat))))
    return [
        round(max(-180.0, lon - dlon), 4), round(max(-MAX_LAT, lat - dlat), 4),
        round(min(180.0, lon + dlon), 4), round(min(MAX_LAT, lat + dlat), 4),
    ]


def _x(lon, n):
    return (lon + 180.0) / 360.0 * n


def _y(lat, n):
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    return (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def tile_range(z, box):
    """(x0, x1, y0, y1), inclusive, of the tiles of zoom z that touch the box."""
    n = 1 << z
    x0, x1 = int(math.floor(_x(box[0], n))), int(math.floor(_x(box[2], n)))
    y0, y1 = int(math.floor(_y(box[3], n))), int(math.floor(_y(box[1], n)))
    clamp = lambda v: max(0, min(n - 1, v))
    return clamp(x0), clamp(x1), clamp(y0), clamp(y1)


def tiles_of(box, max_zoom):
    """Every (z, x, y) of zoom 0 to max_zoom touching the box."""
    for z in range(max_zoom + 1):
        x0, x1, y0, y1 = tile_range(z, box)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def count(box, max_zoom):
    total = 0
    for z in range(max_zoom + 1):
        x0, x1, y0, y1 = tile_range(z, box)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def estimate(lat, lon, radius_km, max_zoom):
    """(tiles, bytes) the map will have, roughly."""
    box = bbox(lat, lon, radius_km)
    tiles = size = 0
    for z in range(max_zoom + 1):
        x0, x1, y0, y1 = tile_range(z, box)
        n = (x1 - x0 + 1) * (y1 - y0 + 1)
        tiles += n
        size += n * BYTES_PER_TILE.get(z, BYTES_PER_LOW_TILE)
    return tiles, size


def tile_id(z, x, y):
    """The Hilbert-curve id PMTiles orders its tiles by."""
    acc = sum(4 ** t for t in range(z))
    d = 0
    s = (1 << z) // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s //= 2
    return acc + d
