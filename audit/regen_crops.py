"""Regenerate the saved Tuticorin and Gulf-of-Mannar January crops from source
imagery using the canonical fixed [-23, +3] dB window (src/crops.py).

The original saved crops used a per-crop percentile stretch, so brightness was not
comparable across crops or with the recent / Run Analysis crops. This re-renders
them from the source GeoTIFFs. Any detection whose source imagery is missing is
reported, not silently skipped.

  Tuticorin (2026-01-18): data/sar_vv_clip.tif        -> data/detection_{rank}_crop.png
  Gulf (Jan 2026):        data/validation/{date}_*.tif -> data/validation/crops/{crop}

Run from the project root:  python audit/regen_crops.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import rasterio
from rasterio.transform import rowcol
from rasterio.warp import transform as warp_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import crops  # noqa: E402

DATA = ROOT / "data"


def _rowcol(src_crs, src_transform, lon: float, lat: float) -> tuple[float, float]:
    xs, ys = warp_transform("EPSG:4326", src_crs, [lon], [lat])
    r, c = rowcol(src_transform, xs[0], ys[0], op=float)
    return r, c


def regen_tuticorin() -> tuple[int, list[str]]:
    clip = DATA / "sar_vv_clip.tif"
    if not clip.exists():
        return 0, [f"MISSING source: {clip} (Tuticorin January crops cannot be regenerated)"]
    feats = json.loads((DATA / "scored_detections.geojson").read_text(encoding="utf-8"))["features"]
    with rasterio.open(clip) as src:
        crs, transform, shape = src.crs, src.transform, (src.height, src.width)
    done, missing = 0, []
    for f in feats:
        p = f["properties"]
        if p.get("date") != "2026-01-18" or not p.get("rank"):
            continue
        lon, lat = f["geometry"]["coordinates"]
        r, c = _rowcol(crs, transform, lon, lat)
        if not (0 <= r < shape[0] and 0 <= c < shape[1]):
            missing.append(f"rank {p['rank']}: point {lon:.4f},{lat:.4f} is outside sar_vv_clip.tif")
            continue
        if crops.save_crop(clip, r, c, DATA / f"detection_{p['rank']}_crop.png"):
            done += 1
        else:
            missing.append(f"rank {p['rank']}: crop window all no-data")
    return done, missing


def _tile_index(date: str):
    """(path, crs, transform, shape) for every validation tile of one date."""
    out = []
    for tif in sorted(DATA.glob(f"validation/{date}_*.tif")):
        with rasterio.open(tif) as src:
            out.append((tif, src.crs, src.transform, (src.height, src.width)))
    return out


def regen_gulf() -> tuple[int, list[str]]:
    gulf = DATA / "scored_gulf.geojson"
    if not gulf.exists():
        return 0, [f"MISSING {gulf}"]
    feats = json.loads(gulf.read_text(encoding="utf-8"))["features"]
    by_date = defaultdict(list)
    for f in feats:
        if f["properties"].get("crop"):
            by_date[f["properties"]["date"]].append(f)

    crop_dir = DATA / "validation" / "crops"
    done, missing = 0, []
    for date, items in sorted(by_date.items()):
        tiles = _tile_index(date)
        if not tiles:
            missing.append(f"{date}: no validation tiles (validation/{date}_*.tif) -> {len(items)} crops cannot be regenerated")
            continue
        for f in items:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            want_r, want_c = p.get("row"), p.get("col")
            best = None  # (dist_to_stored, path, r, c)
            for tif, crs, transform, shape in tiles:
                r, c = _rowcol(crs, transform, lon, lat)
                if not (0 <= r < shape[0] and 0 <= c < shape[1]):
                    continue
                d = abs(r - want_r) + abs(c - want_c) if want_r is not None else 0.0
                if best is None or d < best[0]:
                    best = (d, tif, r, c)
            if best is None:
                missing.append(f"{p['crop']}: no validation tile on {date} covers {lon:.4f},{lat:.4f}")
                continue
            # a sane match should land within a pixel or two of the stored row/col
            if want_r is not None and best[0] > 4:
                missing.append(f"{p['crop']}: nearest tile pixel off stored row/col by {best[0]:.0f}px (kept, verify)")
            if crops.save_crop(best[1], best[2], best[3], crop_dir / p["crop"]):
                done += 1
            else:
                missing.append(f"{p['crop']}: crop window all no-data")
    return done, missing


def main() -> None:
    t_done, t_missing = regen_tuticorin()
    g_done, g_missing = regen_gulf()
    print(f"Tuticorin (2026-01-18): regenerated {t_done} crops at fixed [-23,+3] dB")
    for m in t_missing:
        print(f"  MISSING/NOTE: {m}")
    print(f"Gulf (Jan 2026): regenerated {g_done} crops at fixed [-23,+3] dB")
    for m in g_missing:
        print(f"  MISSING/NOTE: {m}")
    print("\nNot touched (already fixed-window via src/crops.py): recent crops "
          "(data/recent/crops) and live Run Analysis crops.")


if __name__ == "__main__":
    main()
