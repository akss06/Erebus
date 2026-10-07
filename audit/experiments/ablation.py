"""
EXPERIMENT (AUDIT §11, §1, §2, §6): staged ablation on a common set of real tiles.

For a sample of cached recent tiles, on a COMMON footprint (the v3 sea mask), measure
per stage and aggregate:
  - mask coverage: NE-only sea vs v3 sea (JRC + padded 1 km buffer) -> excluded km2
  - original CA-CFAR detector (old NE+300 m mask) vs (v3 mask)
  - GG-CFAR raw pixel exceedance over sea (achieved per-pixel rate on real clutter;
    an UPPER bound -- real targets are included, no labels)
  - GG-CFAR blobs before gates, with vs without binary_opening (§2 trade-off)
  - GG-CFAR post-gate detections (the v3 default) and VH-corroborated count

NO labels are available, so this reports DETECTION DENSITY (dets/km2), pixel
exceedance and excluded area -- NOT precision/recall and NOT "false objects/km2".
Hotspot-selected tiles do not represent arbitrary ocean (§11.2).

Run:  python audit/experiments/ablation.py [n_tiles]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import array_bounds
from scipy import ndimage
from skimage.measure import label as sk_label, regionprops

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
import detect  # noqa: E402
import detect_v3 as D  # noqa: E402
import land_mask  # noqa: E402

DATA = ROOT / "data"
N_TILES = int(sys.argv[1]) if len(sys.argv) > 1 else 40


def ne_only_sea(vv_tif, db, valid, transform, crs, buffer_px):
    bounds = array_bounds(db.shape[0], db.shape[1], transform)
    ne = land_mask.get_sea_mask(bounds=bounds, transform=transform, out_shape=db.shape, crs=crs, cache_dir=DATA)
    if buffer_px > 0:
        ne = ~ndimage.binary_dilation(~ne, iterations=buffer_px)
    return ne & valid


def ca_cfar_count(intensity, sea):
    dets = detect.detect_blobs(intensity, sea, guard_px=5, training_px=15, k=5.0,
                               opening_iterations=1, min_blob_px=2, max_blob_px=60)
    return len(dets)


def v3_stages(db, sea):
    intensity = detect.db_to_linear(db)
    clutter = D._clutter_superpixels(db, sea)
    lm, enough = D._local_clutter_mean(intensity, clutter)
    fc = intensity[clutter & np.isfinite(intensity)]
    gbg = float(np.median(fc)) if fc.size else 1.0
    use_local = np.isfinite(lm) & (lm > 0) & enough
    lm = np.where(use_local, lm, gbg)
    alpha, fit = D._fit_shape_alpha(intensity[sea] / lm[sea], D.PFA)
    if not np.isfinite(alpha):
        return None
    bright = (intensity > lm * alpha) & sea & np.isfinite(intensity)
    exceed = float(bright.sum() / sea.sum())

    def gated(b):
        labels = sk_label(b)
        n = 0
        for rp in regionprops(labels):
            area = int(rp.area)
            if area < D.MIN_BLOB_PX or area > D.MAX_BLOB_PX:
                continue
            if area >= 6 and float(rp.solidity) < D.SOLIDITY_MIN:
                continue
            if area >= 12 and float(rp.eccentricity) > D.ECC_MAX and (area / (
                    (rp.bbox[2] - rp.bbox[0]) * (rp.bbox[3] - rp.bbox[1]) or 1)) < 0.35:
                continue
            n += 1
        return n

    open_b = ndimage.binary_opening(bright, iterations=1)
    return {"alpha": float(alpha), "pixel_exceedance": exceed,
            "blobs_raw_noopen": int(sk_label(bright).max()),
            "blobs_raw_open": int(sk_label(open_b).max()),
            "dets_gated_open": gated(open_b),      # v3 default
            "dets_gated_noopen": gated(bright)}


def main():
    tiles = []
    for j in sorted(DATA.glob("recent/*_jrc.tif"))[:N_TILES * 2]:
        t = j.with_name(j.stem.replace("_jrc", "") + ".tif")
        if t.exists():
            tiles.append(t)
        if len(tiles) >= N_TILES:
            break

    agg = {"n_tiles_requested": N_TILES, "n_tiles_used": 0, "tiles_no_sea": 0,
           "ne_sea_km2": 0.0, "v3_sea_km2": 0.0, "shore_excluded_km2": 0.0,
           "ca_oldmask_dets": 0, "ca_newmask_dets": 0,
           "v3_exceed_weighted_num": 0.0, "v3_sea_px": 0,
           "v3_blobs_noopen": 0, "v3_blobs_open": 0,
           "v3_dets_open": 0, "v3_dets_noopen": 0, "alphas": []}

    for t in tiles:
        with rasterio.open(t) as src:
            db = src.read(1).astype("float64")
            nodata, transform, crs = src.nodata, src.transform, src.crs
        valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)
        _, sea, _, _, mi = D.build_sea_mask(t)
        if sea.sum() == 0:
            agg["tiles_no_sea"] += 1
            continue
        py, px = mi["pixel_m"]
        km2 = (py * px) / 1e6

        ne_sea = ne_only_sea(t, db, valid, transform, crs, buffer_px=30)
        intensity = detect.db_to_linear(db)
        st = v3_stages(db, sea)
        if st is None:
            agg["tiles_no_sea"] += 1
            continue

        agg["n_tiles_used"] += 1
        agg["ne_sea_km2"] += ne_sea.sum() * km2
        agg["v3_sea_km2"] += sea.sum() * km2
        agg["shore_excluded_km2"] += mi["excluded_by_shore_buffer_km2"]
        agg["ca_oldmask_dets"] += ca_cfar_count(intensity, ne_sea)
        agg["ca_newmask_dets"] += ca_cfar_count(intensity, sea)
        agg["v3_exceed_weighted_num"] += st["pixel_exceedance"] * sea.sum()
        agg["v3_sea_px"] += int(sea.sum())
        agg["v3_blobs_noopen"] += st["blobs_raw_noopen"]
        agg["v3_blobs_open"] += st["blobs_raw_open"]
        agg["v3_dets_open"] += st["dets_gated_open"]
        agg["v3_dets_noopen"] += st["dets_gated_noopen"]
        agg["alphas"].append(st["alpha"])

    n = max(agg["n_tiles_used"], 1)
    v3km = max(agg["v3_sea_km2"], 1e-9)
    summary = {
        "tiles_used": agg["n_tiles_used"], "tiles_no_sea": agg["tiles_no_sea"],
        "ne_only_sea_km2": round(agg["ne_sea_km2"], 1),
        "v3_sea_km2": round(agg["v3_sea_km2"], 1),
        "coverage_excluded_by_jrc_and_buffer_km2": round(agg["ne_sea_km2"] - agg["v3_sea_km2"], 1),
        "shore_buffer_excluded_km2": round(agg["shore_excluded_km2"], 1),
        "mean_alpha": round(float(np.mean(agg["alphas"])), 3) if agg["alphas"] else None,
        "ca_cfar_oldmask_density_per_km2": round(agg["ca_oldmask_dets"] / v3km, 3),
        "ca_cfar_newmask_density_per_km2": round(agg["ca_newmask_dets"] / v3km, 3),
        "ggcfar_pixel_exceedance": agg["v3_exceed_weighted_num"] / max(agg["v3_sea_px"], 1),
        "ggcfar_density_with_opening_per_km2": round(agg["v3_dets_open"] / v3km, 3),
        "ggcfar_density_without_opening_per_km2": round(agg["v3_dets_noopen"] / v3km, 3),
        "opening_removes_pct_of_detections": round(100 * (1 - agg["v3_dets_open"] / max(agg["v3_dets_noopen"], 1)), 1),
        "nominal_pfa": D.PFA,
    }
    out = {"summary": summary, "note": "No labels: densities and pixel exceedance only; "
           "hotspot-selected tiles are not arbitrary ocean. Pixel exceedance is an upper "
           "bound on clutter exceedance (real targets included)."}
    (ROOT / "audit" / "results" / "ablation.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
