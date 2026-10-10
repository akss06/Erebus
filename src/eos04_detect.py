"""
Run the v3 detector on an ISRO EOS-04 (RISAT-1A) MRS L2B scene and compare it with
the Sentinel-1 detections from the same day (mentor point 4: show Indian data).

The EOS-04 scene is cut into the SAME 9 km windows the Sentinel-1 run used for that
date (data/recent/<date>_NNN.tif), so both sensors look at identical patches of sea
and v3's per-tile settings (SLIC superpixel count) keep the same ground meaning.
The same "within 2x match radius of the tile centre" filter and 60 m dedupe as
run_recent.py are applied.

EOS-04 differences handled here:
 - 18 m pixels (Sentinel-1 GEE tiles: 10 m), so v3's pixel-count constants are
   rescaled to the same ground size; metre-based ones (shore buffer) need nothing.
 - Raw 16-bit DN; sigma0 dB = 20*log10(DN) - K with K = Calibration_Constant_HH/HV
   from BAND_META.txt (NRSC L2B convention).
 - HH/HV instead of VV/VH: HH goes where v3 expects VV, HV where it expects VH.

Inputs are untrusted downloads; run with `python -P -E` from the project root:
  python -P -E src/eos04_detect.py data/eos04/scene_25/<product dir> 2026-08-29
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_v3
import fetch_sar
import match
import run_recent as rr
import validate_vs_gfw as vgfw

ROOT = Path(__file__).resolve().parent.parent
S1_PX_M = 10.0


def read_band_meta(product_dir: Path) -> dict[str, str]:
    meta = {}
    for line in (product_dir / "BAND_META.txt").read_text(encoding="ascii", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    return meta


def scale_v3_to_pixel(px_m: float) -> dict:
    """Rescale v3's pixel-count constants so they cover the same ground as at 10 m."""
    s = S1_PX_M / px_m
    detect_v3.MAX_BLOB_PX = int(round(600 * s * s))
    detect_v3.GUARD_PX = max(2, int(round(5 * s)))
    detect_v3.TRAIN_PX = max(4, int(round(20 * s)))
    return {"MAX_BLOB_PX": detect_v3.MAX_BLOB_PX, "GUARD_PX": detect_v3.GUARD_PX,
            "TRAIN_PX": detect_v3.TRAIN_PX, "config_hash": detect_v3.config_hash()}


def cut_tile(src_tif: Path, k_db: float, bounds_utm, dst: Path) -> float:
    """Write the window of an EOS-04 band as sigma0 dB (float32, NaN = no data).
    Returns the valid-pixel fraction."""
    with rasterio.open(src_tif) as src:
        win = from_bounds(*bounds_utm, transform=src.transform).round_offsets().round_lengths()
        dn = src.read(1, window=win, boundless=True, fill_value=0).astype("float64")
        tf = src.window_transform(win)
        prof = src.profile
    db = np.where(dn > 0, 20 * np.log10(np.maximum(dn, 1)) - k_db, np.nan).astype("float32")
    prof.update(dtype="float32", nodata=None, width=db.shape[1], height=db.shape[0],
                transform=tf, count=1, compress="deflate")
    with rasterio.open(dst, "w", **prof) as out:
        out.write(db, 1)
    return float(np.isfinite(db).mean())


def main(product_dir: Path, date: str) -> None:
    meta = read_band_meta(product_dir)
    hh = next(product_dir.glob("scene_HH/imagery_HH.tif"))
    hv = next(product_dir.glob("scene_HV/imagery_HV.tif"))
    with rasterio.open(hh) as s:
        eos_crs, px_m = s.crs, abs(s.transform.a)
        eos_bounds_ll = transform_bounds(s.crs, "EPSG:4326", *s.bounds)
    settings = scale_v3_to_pixel(px_m)
    print(f"[eos04] {meta['OTSProductID']} {meta['SceneCenterTime']} UTC, {px_m} m px, v3 rescaled: {settings}")

    fetch_sar.authenticate_and_init(vgfw.PROJECT_ID)   # JRC water mask (cached per tile)
    tile_dir = product_dir.parent / "tiles"
    tile_dir.mkdir(exist_ok=True)

    dets_all: list[dict] = []
    run_bounds: list[tuple] = []
    # run_recent names tiles hotspot*1000 + date index; other files there are stale
    di = rr.RECENT_DATES.index(date)
    s1_tiles = [t for t in rr.RECENT_DIR.glob(f"{date}_*.tif")
                if len(t.stem.split("_")) == 2 and t.stem.split("_")[1].isdigit()
                and int(t.stem.split("_")[1]) % 1000 == di]
    for s1 in sorted(s1_tiles):
        with rasterio.open(s1) as s:
            b_ll = transform_bounds(s.crs, "EPSG:4326", *s.bounds)
        lon_c, lat_c = (b_ll[0] + b_ll[2]) / 2, (b_ll[1] + b_ll[3]) / 2
        if rr.TUTICORIN_BOX[0] <= lon_c <= rr.TUTICORIN_BOX[2] and rr.TUTICORIN_BOX[1] <= lat_c <= rr.TUTICORIN_BOX[3]:
            continue   # run_recent skips it too (Tuticorin is its own dataset)
        if not (eos_bounds_ll[0] < lon_c < eos_bounds_ll[2] and eos_bounds_ll[1] < lat_c < eos_bounds_ll[3]):
            continue
        b_utm = transform_bounds("EPSG:4326", eos_crs, *b_ll)
        hh_t, hv_t = tile_dir / f"{s1.stem}.tif", tile_dir / f"{s1.stem}_vh.tif"
        frac = cut_tile(hh, float(meta["Calibration_Constant_HH"]), b_utm, hh_t)
        cut_tile(hv, float(meta["Calibration_Constant_HV"]), b_utm, hv_t)
        if frac < 0.5:
            print(f"  {s1.stem}: only {frac:.0%} inside the EOS-04 swath, skipped")
            continue
        dets, diag = detect_v3.run_detector_v3(hh_t, hv_t)
        run_bounds.append(b_ll)
        kept = 0
        for d in dets:
            if match.haversine_m(lon_c, lat_c, d["lon"], d["lat"]) > rr.MATCH_RADIUS_M * 2:
                continue
            if any(match.haversine_m(d["lon"], d["lat"], p["lon"], p["lat"]) < rr.DEDUPE_M for p in dets_all):
                continue
            d["tile"] = s1.stem
            dets_all.append(d)
            kept += 1
        print(f"  {s1.stem}: status={diag.get('status')} {len(dets)} raw, {kept} kept", flush=True)

    s1_fc = json.loads((ROOT / "data" / "scored_recent.geojson").read_text(encoding="utf-8"))
    s1 = [f for f in s1_fc["features"] if f["properties"]["date"] == date
          and any(b[0] <= f["geometry"]["coordinates"][0] <= b[2] and b[1] <= f["geometry"]["coordinates"][1] <= b[3]
                  for b in run_bounds)]
    comparison = compare(s1, dets_all)

    out_dir = ROOT / "data" / "eos04"
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
              "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")}} for d in dets_all]
    (out_dir / f"eos04_{date}_scene{meta['SceneNumber']}_detections.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, default=float), encoding="utf-8")
    summary = {"product": meta["OTSProductID"], "scene_center_time_utc": meta["SceneCenterTime"],
               "pixel_m": px_m, "v3_rescaled": settings, "tiles_run": len(run_bounds),
               "eos04_detections": len(dets_all), "comparison": comparison}
    res = ROOT / "audit" / "results" / f"eos04_vs_s1_{date}_scene{meta['SceneNumber']}.json"
    res.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=float))


def compare(s1: list[dict], eos: list[dict]) -> dict:
    """Nearest-neighbour agreement between Sentinel-1 and EOS-04 detections."""
    def nearest(lon, lat, pts):
        return min((match.haversine_m(lon, lat, p[0], p[1]) for p in pts), default=float("inf"))
    eos_pts = [(d["lon"], d["lat"]) for d in eos]
    out = {}
    for name, sel in {"all": s1,
                      "not_clutter": [f for f in s1 if f["properties"]["confidence_class"] != "CLUTTER"],
                      "ais_matched": [f for f in s1 if f["properties"]["match_status"] == "MATCHED"]}.items():
        dists = [nearest(*f["geometry"]["coordinates"], eos_pts) for f in sel]
        out[name] = {"n_s1": len(sel), **{f"within_{r}m": sum(x <= r for x in dists) for r in (100, 250, 500)},
                     "rows": [{"id": f["properties"]["id"], "class": f["properties"]["confidence_class"],
                               "ais": f["properties"]["match_status"], "area_px_s1": f["properties"]["area_px"],
                               "nearest_eos04_m": round(x, 0)} for f, x in zip(sel, dists)] if name != "all" else None}
    return out


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
