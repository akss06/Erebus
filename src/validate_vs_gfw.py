"""
Round 2 validation: how well does our detector agree with GFW's independent
Sentinel-1 ship detections (public-global-sar-presence)?

For each GFW detection we download a small Sentinel-1 tile centred on it, run
the *unmodified* land mask + CA-CFAR detector on that tile, and record:

  - RECALL: does our detector produce a blob within MATCH_RADIUS_M of the GFW
    detection? (GFW positions are snapped to a ~0.01 deg grid, so the radius
    must allow for ~0.8 km of snap error.)
  - EXTRAS: our detections in the tile that sit within no GFW detection's
    radius. Reported per 100 km^2 of sea so tiles of different size compare.

GFW is a reference, not perfect ground truth - it also misses ships and has
false alarms. "Extra" therefore means "GFW did not report it", not "wrong".

Run from the project root:  python src/validate_vs_gfw.py
Caches GFW responses and tiles under data/validation/ (tifs are gitignored).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ee
import numpy as np
import rasterio
import requests
from rasterio.transform import array_bounds
from rasterio.warp import transform as warp_transform
from scipy.ndimage import binary_dilation

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect  # noqa: E402
import fetch_ais  # noqa: E402
import fetch_sar  # noqa: E402
import land_mask  # noqa: E402
import match  # noqa: E402

PROJECT_ID = "dark-vessel-detection-504204"

# Same detector settings as the validated Round 1 run (config/milestone1_confirmed.json).
CFAR = dict(guard_px=5, training_px=15, k=5.0, opening_iterations=1, min_blob_px=2, max_blob_px=60)
COAST_BUFFER_PX = 30
SCALE_M = 10

# Gulf of Mannar + Palk Strait, wide enough to catch most GFW detections there.
REGION = (78.0, 8.2, 80.0, 10.3)  # lon_min, lat_min, lon_max, lat_max
# Orbit-92 passes (~00:33 UTC) that exist for both areas. Add dates here to widen the test.
PASS_DATES = ["2026-01-06", "2026-01-18", "2026-01-30"]

TILE_HALF_DEG = 0.04      # ~9 km square tile around each GFW detection
MAX_PER_DATE = 10         # each tile costs ~1-2 min (scene lookup + download + land mask); sample, don't test all
MATCH_RADIUS_M = 1500     # GFW grid snap (~0.8 km) + detector centroid error

ROOT = Path(__file__).resolve().parent.parent
VAL_DIR = ROOT / "data" / "validation"
DATA_DIR = ROOT / "data"


def fetch_gfw_sar(token: str, date: str) -> list[dict]:
    """GFW SAR detections for one pass date. A one-day range returns nothing
    (same API quirk as the AIS presence product), so query date +/- 1 day and
    keep records dated exactly `date`."""
    cache = VAL_DIR / f"gfw_sar_{date}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    d = dt.date.fromisoformat(date)
    lo, hi = (d - dt.timedelta(days=1)).isoformat(), (d + dt.timedelta(days=1)).isoformat()
    lon0, lat0, lon1, lat1 = REGION
    geo = {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}
    params = {
        "datasets[0]": "public-global-sar-presence:latest",
        "date-range": f"{lo},{hi}",
        "temporal-resolution": "DAILY",
        "spatial-resolution": "HIGH",
        "format": "JSON",
        "group-by": "VESSEL_ID",
    }
    resp = requests.post(f"{fetch_ais.GFW_API_BASE}/4wings/report", headers=fetch_ais._headers(token),
                         params=params, json={"geojson": geo}, timeout=180)
    resp.raise_for_status()
    entries = resp.json().get("entries", [{}])[0].get("public-global-sar-presence:v4.0") or []
    records = [e for e in entries if e.get("date") == date]
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(records))
    return records


def find_scene(lon: float, lat: float, date: str) -> str | None:
    d = dt.date.fromisoformat(date)
    pt = ee.Geometry.Point([lon, lat])
    coll = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(pt)
            .filterDate(date, (d + dt.timedelta(days=1)).isoformat())
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV")))
    feats = coll.getInfo()["features"]
    return feats[0]["id"] if feats else None


def download_with_retry(aoi, scene: str, tif: Path, attempts: int = 3) -> bool:
    """Earth Engine's download endpoint occasionally drops the connection; one bad tile should not kill the batch."""
    for n in range(attempts):
        try:
            fetch_sar.download_scene(aoi, scene, tif, scale=SCALE_M)
            return True
        except (requests.RequestException, ee.EEException) as e:
            print(f"  [retry {n + 1}/{attempts}] {type(e).__name__}")
            time.sleep(3)
    return False


def run_detector_on_tile(tif: Path) -> tuple[list[dict], float]:
    """Unmodified Round 1 pipeline on one tile. Returns (detections with lon/lat, sea area km^2)."""
    with rasterio.open(tif) as src:
        db = src.read(1).astype("float64")
        transform, nodata, crs = src.transform, src.nodata, src.crs
    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)
    if valid.mean() < 0.5:
        return [], 0.0
    bounds = array_bounds(db.shape[0], db.shape[1], transform)
    sea = land_mask.get_sea_mask(bounds=bounds, transform=transform, out_shape=db.shape, crs=crs, cache_dir=DATA_DIR)
    sea = ~binary_dilation(~sea, iterations=COAST_BUFFER_PX) & valid
    dets = detect.detect_blobs(detect.db_to_linear(db), sea, **CFAR)
    if dets:
        xs, ys = zip(*(detect.pixel_to_xy(d["row"], d["col"], transform) for d in dets))
        lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
        for d, lon, lat in zip(dets, lons, lats):
            d["lon"], d["lat"] = lon, lat
    sea_km2 = float(sea.sum()) * (abs(transform.a) * abs(transform.e)) / 1e6
    return dets, sea_km2


def ensure_tile(date: str, i: int, g: dict) -> Path | None:
    """Download (once) the tile around one GFW detection; None if it cannot be had."""
    lon, lat = g["lon"], g["lat"]
    tif = VAL_DIR / f"{date}_{i:03d}.tif"
    if tif.exists():
        return tif
    scene = find_scene(lon, lat, date)
    if scene is None:
        print(f"  [skip] no Sentinel-1 scene covering {lon:.3f},{lat:.3f} on {date}", flush=True)
        return None
    aoi = fetch_sar.build_aoi(lon - TILE_HALF_DEG, lat - TILE_HALF_DEG, lon + TILE_HALF_DEG, lat + TILE_HALF_DEG)
    if not download_with_retry(aoi, scene, tif):
        print(f"  [skip] download failed 3x for {lon:.3f},{lat:.3f} on {date}", flush=True)
        return None
    return tif


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="test every GFW detection, not a sample of MAX_PER_DATE per date")
    parser.add_argument("--workers", type=int, default=4, help="parallel Earth Engine downloads")
    args = parser.parse_args()

    token = fetch_ais.get_token()
    fetch_sar.authenticate_and_init(PROJECT_ID)
    VAL_DIR.mkdir(parents=True, exist_ok=True)

    jobs = []
    for date in PASS_DATES:
        gfw = fetch_gfw_sar(token, date)
        print(f"[gfw] {date}: {len(gfw)} SAR detections ({sum(1 for g in gfw if not g['mmsi'])} without AIS match)", flush=True)
        if args.all:
            picked = list(enumerate(gfw))
        else:
            # Deterministic spread: keep every Nth record so the sample covers the whole region.
            step = max(1, len(gfw) // MAX_PER_DATE)
            picked = list(enumerate(gfw))[::step][:MAX_PER_DATE]
        jobs += [(date, i, g) for i, g in picked]

    print(f"[download] {len(jobs)} tiles, {args.workers} in parallel", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tifs = list(pool.map(lambda j: ensure_tile(*j), jobs))

    rows = []
    for (date, i, g), tif in zip(jobs, tifs):
        if tif is None:
            continue
        lon, lat = g["lon"], g["lat"]
        dets, sea_km2 = run_detector_on_tile(tif)
        dists = [match.haversine_m(lon, lat, d["lon"], d["lat"]) for d in dets]
        nearest = min(dists) if dists else None
        hit = nearest is not None and nearest <= MATCH_RADIUS_M
        extras = sum(1 for dist in dists if dist > MATCH_RADIUS_M)
        rows.append({
            "date": date, "lon": lon, "lat": lat, "gfw_has_ais_match": bool(g["mmsi"]),
            "gfw_name": g.get("shipName") or None, "our_nearest_detection_m": nearest,
            "found_by_us": hit, "tile_detections": len(dets), "tile_extras": extras, "tile_sea_km2": sea_km2,
        })
        print(f"  {date} GFW {lon:.3f},{lat:.3f} ais={'Y' if g['mmsi'] else 'N'}  found={hit}  "
              f"nearest={nearest if nearest is None else round(nearest)}m  tile_dets={len(dets)}", flush=True)

    (VAL_DIR / "validation_rows.json").write_text(json.dumps(rows, indent=2))
    summarize(rows)


def summarize(rows: list[dict]) -> None:
    n = len(rows)
    if n == 0:
        print("No rows.")
        return
    found = sum(r["found_by_us"] for r in rows)
    with_ais = [r for r in rows if r["gfw_has_ais_match"]]
    no_ais = [r for r in rows if not r["gfw_has_ais_match"]]
    extras = sum(r["tile_extras"] for r in rows)
    area = sum(r["tile_sea_km2"] for r in rows)
    print("\n=== SUMMARY ===")
    print(f"GFW detections tested: {n}")
    print(f"Found by our detector within {MATCH_RADIUS_M} m: {found}/{n} ({found / n:.0%})")
    if with_ais:
        print(f"  GFW detections WITH AIS match:    {sum(r['found_by_us'] for r in with_ais)}/{len(with_ais)}")
    if no_ais:
        print(f"  GFW detections WITHOUT AIS match: {sum(r['found_by_us'] for r in no_ais)}/{len(no_ais)}")
    print(f"Extra detections (ours, not near any tested GFW detection): {extras} over {area:.0f} km^2 sea "
          f"= {100 * extras / area:.2f} per 100 km^2" if area else "No sea area")


if __name__ == "__main__":
    main()
