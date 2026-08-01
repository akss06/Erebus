"""
Milestone 1 step: mask out land pixels using a Natural Earth 10 m coastline polygon.

Mandatory per PROJECT_SPEC.md's gotcha #1 - land is a strong radar reflector
and floods a naive threshold detector with false positives along the coast.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import requests
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
from shapely.geometry import box

NATURAL_EARTH_LAND_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip"


def _download_natural_earth_land(cache_dir: Path) -> Path:
    """Download (once) and cache the global Natural Earth 10m land polygon zip."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / "ne_10m_land.zip"
    if not zip_path.exists():
        resp = requests.get(NATURAL_EARTH_LAND_URL, timeout=120)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    return zip_path


def get_sea_mask(
    bounds: tuple[float, float, float, float],
    transform,
    out_shape: tuple[int, int],
    crs,
    cache_dir: Path,
) -> np.ndarray:
    """Boolean array matching the raster grid: True = sea/water, False = land.

    bounds: (minx, miny, maxx, maxy) in the raster's own CRS.
    transform: the rasterio Affine transform of the raster (for rasterizing
        land polygons onto the same pixel grid).
    crs: the raster's CRS (e.g. rasterio's src.crs). Natural Earth land ships
        in EPSG:4326 (lon/lat degrees); Sentinel-1 GeoTIFFs from GEE come back
        in their scene's native UTM zone (meters), so land polygons need to be
        reprojected before rasterizing onto the raster's pixel grid.
    """
    zip_path = _download_natural_earth_land(cache_dir)
    land = gpd.read_file(f"zip://{zip_path}")

    # Actually clip (not just bbox-filter) to the AOI in EPSG:4326 BEFORE
    # reprojecting. Natural Earth merges whole contiguous landmasses into a
    # single feature (e.g. all of Eurasia+Africa is one ~268k-vertex
    # multipolygon) - a bbox-only filter (.cx[]) would keep that entire
    # feature intact just because its bounding box overlaps our tiny AOI, and
    # reprojecting hundreds of thousands of antipodal vertices into a small
    # local UTM zone sends coordinates to near-infinity and grinds to a halt.
    # gpd.clip() performs a true geometric intersection, so what's left is
    # just the local coastline sliver near the AOI.
    minx, miny, maxx, maxy = transform_bounds(crs, "EPSG:4326", *bounds)
    pad = 0.05  # degrees, safety margin so edge-of-AOI land isn't clipped off
    aoi_box = box(minx - pad, miny - pad, maxx + pad, maxy + pad)
    land_clip = gpd.clip(land, aoi_box)

    if land_clip.empty:
        # No land polygons intersect the AOI at all -> fully sea.
        return np.ones(out_shape, dtype=bool)

    epsg = crs.to_epsg()
    land_clip = land_clip.to_crs(epsg if epsg is not None else crs.to_wkt())

    land_raster = rasterize(
        [(geom, 1) for geom in land_clip.geometry],
        out_shape=out_shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    )
    return land_raster == 0
