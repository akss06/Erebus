"""
Pulls the SAR detection crop for NEREUS PROGRESS (MMSI 341970001) on each
of the 4 dates run_multi_date.py's summary showed it matched to a SAR
detection (2026-01-18, 2026-01-06, 2026-01-30, 2026-02-11), and checks
whether its matched lon/lat is physically consistent across dates. A real
anchored/slow-moving vessel should stay within a few hundred metres to a
couple km between passes; a jump of 10+ km would mean the pipeline is
coincidentally matching different physical SAR detections to the same
recurring AIS vessel name, not actually tracking one ship.

Does not modify any existing pipeline file - reads already-saved detection
geojsons (data/matched_detections.geojson, data/multi_date/*.geojson) and
cached SAR GeoTIFFs from Milestone 1/2/3 and run_multi_date.py.

Run from the project root: `python src/nereus_crops.py`
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

TARGET_MMSI = "341970001"
TARGET_NAME = "NEREUS PROGRESS"
CROP_HALF_WINDOW_PX = 75  # 150x150px crop window

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MULTI_DATE_DIR = DATA_DIR / "multi_date"
RESULTS_DIR = PROJECT_ROOT / "results"

DATE_SOURCES = [
    {"date": "2026-01-18", "detections_path": DATA_DIR / "matched_detections.geojson", "tif_path": DATA_DIR / "sar_vv_clip.tif"},
    {"date": "2026-01-06", "detections_path": MULTI_DATE_DIR / "2026-01-06_detections.geojson", "tif_path": MULTI_DATE_DIR / "2026-01-06_sar_vv_clip.tif"},
    {"date": "2026-01-30", "detections_path": MULTI_DATE_DIR / "2026-01-30_detections.geojson", "tif_path": MULTI_DATE_DIR / "2026-01-30_sar_vv_clip.tif"},
    {"date": "2026-02-11", "detections_path": MULTI_DATE_DIR / "2026-02-11_detections.geojson", "tif_path": MULTI_DATE_DIR / "2026-02-11_sar_vv_clip.tif"},
]

CONSISTENT_M = 2000  # below this: clearly the same vessel, normal anchorage/transit drift
BORDERLINE_M = 10000  # between CONSISTENT_M and this: larger than typical drift but not conclusive


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_nereus_detection(detections_path: Path) -> dict | None:
    data = json.loads(detections_path.read_text(encoding="utf-8"))
    for f in data["features"]:
        props = f["properties"]
        if str(props.get("matched_mmsi")) == TARGET_MMSI:
            d = dict(props)
            d["lon"], d["lat"] = f["geometry"]["coordinates"]
            return d
    return None


def lonlat_to_rowcol(tif_path: Path, lon: float, lat: float) -> tuple[float, float]:
    """Recomputed from lon/lat via the raster's own inverse affine transform,
    rather than trusting a stored row/col - Milestone 1's detections.geojson
    (and therefore Jan 18's matched_detections.geojson, derived from it)
    never carried pixel coordinates, only lon/lat."""
    with rasterio.open(tif_path) as src:
        transform = src.transform
        crs = src.crs
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    col, row = ~transform * (xs[0], ys[0])
    return row, col


def load_crop(tif_path: Path, row: float, col: float) -> tuple[np.ndarray, tuple[float, float]]:
    with rasterio.open(tif_path) as src:
        db = src.read(1).astype("float64")
        nodata = src.nodata

    r, c = int(round(row)), int(round(col))
    r0, r1 = max(0, r - CROP_HALF_WINDOW_PX), min(db.shape[0], r + CROP_HALF_WINDOW_PX)
    c0, c1 = max(0, c - CROP_HALF_WINDOW_PX), min(db.shape[1], c + CROP_HALF_WINDOW_PX)
    crop = db[r0:r1, c0:c1]

    valid = np.isfinite(crop) if nodata is None else (crop != nodata) & np.isfinite(crop)
    lo, hi = np.percentile(crop[valid], [2, 98])
    stretched = np.clip((crop - lo) / (hi - lo), 0, 1)
    center = (r - r0, c - c0)
    return stretched, center


def render_crop(ax, stretched: np.ndarray, center: tuple[float, float], date: str, contrast_db: float, area_px: int) -> None:
    center_row, center_col = center
    ax.imshow(stretched, cmap="gray", vmin=0, vmax=1)
    ax.axhline(center_row, color="red", linewidth=0.8, alpha=0.8)
    ax.axvline(center_col, color="red", linewidth=0.8, alpha=0.8)
    ax.plot(center_col, center_row, marker="+", color="red", markersize=14, markeredgewidth=1.5)
    ax.set_title(f"{date}\ncontrast={contrast_db:.1f}dB  area={area_px}px", fontsize=9)
    ax.set_xticks([])
    ax.set_yticks([])


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    print(f"[nereus] Locating {TARGET_NAME} (MMSI {TARGET_MMSI}) matches across 4 dates...\n")
    for i, src in enumerate(DATE_SOURCES, start=1):
        det = find_nereus_detection(src["detections_path"])
        if det is None:
            print(f"[nereus] {src['date']}: no detection matched to {TARGET_NAME} found in {src['detections_path'].name} - skipping.")
            continue

        row, col = lonlat_to_rowcol(src["tif_path"], det["lon"], det["lat"])
        stretched, center = load_crop(src["tif_path"], row, col)

        out_path = RESULTS_DIR / f"nereus_date{i}_crop.png"
        fig, ax = plt.subplots(figsize=(4, 4), dpi=100)
        render_crop(ax, stretched, center, src["date"], det["contrast_db"], det["area_px"])
        fig.tight_layout()
        fig.savefig(out_path)
        plt.close(fig)

        det.update(date=src["date"], stretched=stretched, center=center, crop_path=out_path)
        results.append(det)

        print(f"[nereus] {src['date']}: lon={det['lon']:.5f}  lat={det['lat']:.5f}  "
              f"contrast_db={det['contrast_db']:.1f}  area_px={det['area_px']}  "
              f"dist_to_AIS_m={det['match_distance_m']:.0f}")
        print(f"[nereus] Saved crop: {out_path}\n")

    if len(results) >= 2:
        print("=" * 78)
        print("[nereus] Position consistency across dates (does this look like one vessel?):")
        for a in range(len(results)):
            for b in range(a + 1, len(results)):
                d1, d2 = results[a], results[b]
                dist = haversine_m(d1["lon"], d1["lat"], d2["lon"], d2["lat"])
                if dist < CONSISTENT_M:
                    verdict = "consistent - normal anchorage/transit drift for one vessel"
                elif dist < BORDERLINE_M:
                    verdict = "borderline - larger than typical drift, not conclusive either way"
                else:
                    verdict = "INCONSISTENT - likely a coincidental MMSI match to a different physical detection, not one tracked vessel"
                print(f"    {d1['date']} <-> {d2['date']}: {dist / 1000:.2f} km  -> {verdict}")
        print()

    if results:
        fig, axes = plt.subplots(1, len(results), figsize=(4 * len(results), 4.5), dpi=100)
        if len(results) == 1:
            axes = [axes]
        for ax, det in zip(axes, results):
            render_crop(ax, det["stretched"], det["center"], det["date"], det["contrast_db"], det["area_px"])
        fig.suptitle(f"{TARGET_NAME} (MMSI {TARGET_MMSI}) - matched SAR detections across 4 dates", fontsize=12)
        fig.tight_layout()
        combined_path = RESULTS_DIR / "nereus_combined.png"
        fig.savefig(combined_path)
        plt.close(fig)
        print(f"[nereus] Saved combined comparison: {combined_path}")


if __name__ == "__main__":
    main()
