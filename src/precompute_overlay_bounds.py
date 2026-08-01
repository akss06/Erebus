"""
One-off precompute step: builds app.py's SAR overlay image and its
EPSG:4326 bounds from data/sar_vv_clip.tif, so app.py never needs to open
a GeoTIFF - or import rasterio - at Streamlit runtime.

rasterio is only used here, in a script that never runs at deploy time.
app.py instead reads the two files this script produces:
  - data/overlay_rgba.png    2nd-98th percentile contrast-stretched RGBA
                              PNG (alpha=0 over nodata pixels) - the exact
                              image app.py used to build on the fly.
  - data/overlay_bounds.json {"overlay_png": "overlay_rgba.png",
                               "bounds": [[south, west], [north, east]]} -
                              the [[lat,lon],[lat,lon]] pair folium's
                              ImageOverlay expects.

Re-run this whenever data/sar_vv_clip.tif changes:
  python src/precompute_overlay_bounds.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform_bounds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SAR_TIF_PATH = DATA_DIR / "sar_vv_clip.tif"
OVERLAY_PNG_PATH = DATA_DIR / "overlay_rgba.png"
OVERLAY_BOUNDS_PATH = DATA_DIR / "overlay_bounds.json"


def main() -> None:
    with rasterio.open(SAR_TIF_PATH) as ds:
        db = ds.read(1).astype("float64")
        nodata = ds.nodata
        valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)
        bounds = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)

    lo, hi = np.percentile(db[valid], [2, 98])
    stretched = np.clip((db - lo) / (hi - lo), 0, 1)
    gray = (stretched * 255).astype("uint8")

    rgba = np.zeros((*gray.shape, 4), dtype="uint8")
    rgba[..., 0] = gray
    rgba[..., 1] = gray
    rgba[..., 2] = gray
    rgba[..., 3] = np.where(valid, 220, 0).astype("uint8")
    Image.fromarray(rgba, mode="RGBA").save(OVERLAY_PNG_PATH)
    print(f"[precompute] Saved {OVERLAY_PNG_PATH}")

    west, south, east, north = bounds
    folium_bounds = [[south, west], [north, east]]
    OVERLAY_BOUNDS_PATH.write_text(
        json.dumps({"overlay_png": OVERLAY_PNG_PATH.name, "bounds": folium_bounds}, indent=2),
        encoding="utf-8",
    )
    print(f"[precompute] Saved {OVERLAY_BOUNDS_PATH}  bounds={folium_bounds}")


if __name__ == "__main__":
    main()
