"""
Pulls every UNMATCHED SAR detection across all 4 dates (2026-01-18,
2026-01-06, 2026-01-30, 2026-02-11) with contrast_db above CONTRAST_THRESHOLD_DB
and reports how far the nearest AIS position actually was, even though it
didn't fall within match.py's 1500m match radius.

First pass used a 12dB threshold ("real-vessel-territory" per #3/#4 and
NEREUS PROGRESS) and found zero qualifying detections - the highest-contrast
UNMATCHED detection on any date topped out at 10.9dB. Lowered to 8dB here to
check whether that first result was a real separation or just an artifact of
where exactly the cutoff sat.

Does not modify any existing pipeline file - reads already-saved detection
geojsons and cached SAR GeoTIFFs from Milestone 1/2/3 and run_multi_date.py.

Run from the project root: `python src/unmatched_high_contrast.py`
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

CONTRAST_THRESHOLD_DB = 8.0
TOP_N_CROPS = 5
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


def load_detections(detections_path: Path) -> list[dict]:
    data = json.loads(detections_path.read_text(encoding="utf-8"))
    dets = []
    for f in data["features"]:
        props = dict(f["properties"])
        props["lon"], props["lat"] = f["geometry"]["coordinates"]
        dets.append(props)
    return dets


def lonlat_to_rowcol(tif_path: Path, lon: float, lat: float) -> tuple[float, float]:
    """Recomputed from lon/lat via the raster's own inverse affine transform -
    Milestone 1's detections.geojson (and Jan 18's matched_detections.geojson,
    derived from it) never carried pixel coordinates, only lon/lat."""
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

    candidates = []
    for src in DATE_SOURCES:
        for d in load_detections(src["detections_path"]):
            if d.get("match_status") == "UNMATCHED" and d["contrast_db"] > CONTRAST_THRESHOLD_DB:
                d = dict(d)
                d["date"] = src["date"]
                d["tif_path"] = src["tif_path"]
                candidates.append(d)

    candidates.sort(key=lambda d: d["contrast_db"], reverse=True)

    print(f"[unmatched] {len(candidates)} UNMATCHED detection(s) with contrast_db > "
          f"{CONTRAST_THRESHOLD_DB:.0f}dB across all 4 dates:\n")
    print(f"{'#':<4}{'date':<12}{'lon':<11}{'lat':<10}{'contrast_db':<12}{'area_px':<9}{'dist_to_nearest_AIS_m':<22}")
    for i, d in enumerate(candidates, start=1):
        dist_str = f"{d['match_distance_m']:.0f}" if d.get("match_distance_m") is not None else "no AIS records that date"
        print(f"{i:<4}{d['date']:<12}{d['lon']:<11.5f}{d['lat']:<10.5f}{d['contrast_db']:<12.1f}{d['area_px']:<9}{dist_str:<22}")

    if not candidates:
        print("\n[unmatched] No qualifying detections - nothing to crop.")
        return

    top = candidates[:TOP_N_CROPS]
    print(f"\n[unmatched] Generating crops for the top {len(top)} by contrast_db...\n")
    for i, d in enumerate(top, start=1):
        row, col = lonlat_to_rowcol(d["tif_path"], d["lon"], d["lat"])
        stretched, center = load_crop(d["tif_path"], row, col)

        out_path = RESULTS_DIR / f"unmatched_high_contrast_{i}_{d['date']}_crop.png"
        fig, ax = plt.subplots(figsize=(4, 4), dpi=100)
        render_crop(ax, stretched, center, d["date"], d["contrast_db"], d["area_px"])
        fig.tight_layout()
        fig.savefig(out_path)
        plt.close(fig)

        print(f"[unmatched] #{i}  date={d['date']}  lon={d['lon']:.5f}  lat={d['lat']:.5f}  "
              f"contrast_db={d['contrast_db']:.1f}  dist_to_nearest_AIS_m={d.get('match_distance_m')}  "
              f"-> saved {out_path}")


if __name__ == "__main__":
    main()
