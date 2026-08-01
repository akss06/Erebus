"""
Milestone 1 step: pull a calibrated Sentinel-1 GRD VV clip from Google Earth Engine.

COPERNICUS/S1_GRD is already radiometrically calibrated (sigma-nought, in dB)
and terrain-corrected by GEE, so there is no SNAP/snappy preprocessing here —
per PROJECT_SPEC.md, that toolchain is out of scope entirely.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import ee
import requests


def authenticate_and_init(project_id: str) -> None:
    """Initialize the Earth Engine client, authenticating in a browser on first use."""
    try:
        ee.Initialize(project=project_id)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project_id)


def build_aoi(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> ee.Geometry:
    return ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])


def list_available_scenes(aoi: ee.Geometry, months_back: int = 12) -> list[dict]:
    """Query S1_GRD for IW/VV scenes over the AOI in the last `months_back` months.

    Returns real acquisitions (date, orbit direction, product id) sorted by
    date, rather than assuming a chosen date actually has a pass.
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=30 * months_back)

    coll = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filterDate(start.isoformat(), end.isoformat())
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    )

    info = coll.getInfo()
    scenes = []
    for feat in info["features"]:
        props = feat["properties"]
        ts = props["system:time_start"] / 1000.0
        date_str = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
        scenes.append(
            {
                "product_id": feat["id"],
                "date": date_str,
                "orbit_direction": props.get("orbitProperties_pass", "?"),
                "relative_orbit": props.get("relativeOrbitNumber_start", "?"),
            }
        )
    scenes.sort(key=lambda s: s["date"])
    return scenes


def download_scene(
    aoi: ee.Geometry,
    product_id: str,
    out_path: Path,
    scale: int = 10,
    band: str = "VV",
) -> Path:
    """Download a single-band, AOI-clipped GeoTIFF via getDownloadURL (no Drive/Cloud export)."""
    image = ee.Image(product_id).select(band).clip(aoi)
    url = image.getDownloadURL({"scale": scale, "region": aoi, "format": "GEO_TIFF"})

    resp = requests.get(url, timeout=180)
    resp.raise_for_status()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)
    return out_path
