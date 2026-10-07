"""
Round 2: objective test that does not rely on anyone eyeballing a radar image.

Question (POPULATION-LEVEL association, not per-target): are strong, ship-sized
blobs found at a HIGHER DENSITY near GFW's SAR detections than in the surrounding
sea of the same tiles? Run separately for GFW detections WITH an AIS match (a
positive control: real ships) and WITHOUT one (the dark-vessel question).

Methodology (corrected per AUDIT_CHECKLIST §12):
  - Valid-water area only: the "inside" and "elsewhere" areas are the actual SEA
    pixels inside / outside the search circle (rasterised on the tile grid), not a
    full geometric circle and not the whole tile. Land/buffer are excluded.
  - Densities: inside_density = sum(blobs inside) / sum(inside sea km2); likewise
    outside. Enrichment = inside_density / outside_density.
  - Uncertainty: a TILE-level block bootstrap (resample whole tiles with
    replacement) gives a 95% interval. This respects tile-level spatial dependence
    instead of assuming every blob is an independent Poisson event.
  - Sensitivity: swept over search radius and contrast threshold.

Hard limits (do NOT over-read):
  - This is a population association. It is NOT candidate precision and NOT a
    probability that any particular target is a vessel. Never convert it to one.
  - GFW SAR is derived from the SAME Sentinel-1 imagery (shared sensor), so some
    agreement is expected by construction.
  - Overlapping tiles can share blobs; matched controls on coast-distance/background
    are not applied here (would strengthen it). Treat the ratio as indicative.

Run (needs data/validation tiles from validate_vs_gfw.py --all):
    python src/enrichment_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import array_bounds
from rasterio.warp import transform as warp_transform
from scipy.ndimage import binary_dilation

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect  # noqa: E402
import land_mask  # noqa: E402
import match  # noqa: E402
import validate_vs_gfw as vgfw  # noqa: E402

TUTICORIN_BOX = (78.20, 8.70, 78.34, 8.87)  # port anchorage: dense AIS, different regime
RADII_M = [1000.0, 1500.0, 2000.0]
CONTRASTS_DB = [10.0, 12.0, 15.0]
MIN_AREA_PX = 8
BOOTSTRAP = 2000
SEED = 0


def tile_scan(tif: Path, lon: float, lat: float):
    """Run the detector ONCE and return, for all radii/contrasts:
    {blobs:[(dist_m, contrast_db)], sea_inside_km2:{radius:km2}, sea_outside_km2:{radius:km2}}."""
    with rasterio.open(tif) as src:
        db = src.read(1).astype("float64")
        transform, nodata, crs = src.transform, src.nodata, src.crs
    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)
    if valid.mean() < 0.5:
        return None
    bounds = array_bounds(db.shape[0], db.shape[1], transform)
    sea = land_mask.get_sea_mask(bounds=bounds, transform=transform, out_shape=db.shape, crs=crs, cache_dir=vgfw.DATA_DIR)
    sea = (~binary_dilation(~sea, iterations=vgfw.COAST_BUFFER_PX)) & valid

    dets = detect.detect_blobs(detect.db_to_linear(db), sea, **vgfw.CFAR)
    blobs = []
    for d in dets:
        if d["area_px"] < MIN_AREA_PX:
            continue
        x, y = detect.pixel_to_xy(d["row"], d["col"], transform)
        lo, la = warp_transform(crs, "EPSG:4326", [x], [y])
        blobs.append((match.haversine_m(lon, lat, lo[0], la[0]), d["contrast_db"]))

    rows, cols = np.indices(db.shape)
    xs = transform.c + (cols + 0.5) * transform.a + (rows + 0.5) * transform.b
    ys = transform.f + (cols + 0.5) * transform.d + (rows + 0.5) * transform.e
    cx, cy = warp_transform("EPSG:4326", crs, [lon], [lat])
    dist = np.hypot(xs - cx[0], ys - cy[0])
    px_km2 = (abs(transform.a) * abs(transform.e)) / 1e6
    sea_in, sea_out = {}, {}
    for radius in RADII_M:
        inside = dist <= radius
        sea_in[radius] = float((inside & sea).sum()) * px_km2
        sea_out[radius] = float((~inside & sea).sum()) * px_km2
    return {"blobs": blobs, "sea_in": sea_in, "sea_out": sea_out}


def combo_measure(scan: dict, radius_m: float, min_contrast: float):
    strong = [b for b in scan["blobs"] if b[1] >= min_contrast]
    nin = sum(1 for dmtr, _ in strong if dmtr <= radius_m)
    return nin, len(strong) - nin, scan["sea_in"][radius_m], scan["sea_out"][radius_m]


def enrichment(tiles: list[dict]) -> float | None:
    din = sum(t["nin"] for t in tiles)
    dout = sum(t["nout"] for t in tiles)
    ain = sum(t["ain"] for t in tiles)
    aout = sum(t["aout"] for t in tiles)
    if ain <= 0 or aout <= 0 or dout == 0:
        return None
    inside_density = din / ain
    outside_density = dout / aout
    return inside_density / outside_density if outside_density > 0 else None


def bootstrap_ci(tiles: list[dict], rng) -> tuple[float, float] | None:
    if len(tiles) < 3:
        return None
    vals = []
    arr = np.array(range(len(tiles)))
    for _ in range(BOOTSTRAP):
        idx = rng.choice(arr, size=len(tiles), replace=True)
        e = enrichment([tiles[i] for i in idx])
        if e is not None:
            vals.append(e)
    if not vals:
        return None
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def main() -> None:
    rows = json.loads((vgfw.VAL_DIR / "validation_rows.json").read_text())
    rng = np.random.default_rng(SEED)
    report = {"radii_m": RADII_M, "contrasts_db": CONTRASTS_DB, "min_area_px": MIN_AREA_PX,
              "bootstrap": BOOTSTRAP, "results": []}

    # run the detector once per tile, then derive every (radius, contrast) combo
    scans = []
    for r in rows:
        if TUTICORIN_BOX[0] <= r["lon"] <= TUTICORIN_BOX[2] and TUTICORIN_BOX[1] <= r["lat"] <= TUTICORIN_BOX[3]:
            continue
        gfw = json.loads((vgfw.VAL_DIR / f"gfw_sar_{r['date']}.json").read_text())
        try:
            i = next(k for k, g in enumerate(gfw)
                     if abs(g["lon"] - r["lon"]) < 1e-6 and abs(g["lat"] - r["lat"]) < 1e-6)
        except StopIteration:
            continue
        tif = vgfw.VAL_DIR / f"{r['date']}_{i:03d}.tif"
        if not tif.exists():
            continue
        scan = tile_scan(tif, r["lon"], r["lat"])
        if scan is not None:
            scans.append((bool(r["gfw_has_ais_match"]), scan))
    print(f"[enrichment] {len(scans)} tiles scanned")

    for radius in RADII_M:
        for contrast in CONTRASTS_DB:
            groups = {True: [], False: []}
            for has_ais, scan in scans:
                nin, nout, ain, aout = combo_measure(scan, radius, contrast)
                groups[has_ais].append({"nin": nin, "nout": nout, "ain": ain, "aout": aout})

            entry = {"radius_m": radius, "contrast_db": contrast, "groups": {}}
            for has_ais, label in ((True, "with_ais_control"), (False, "without_ais")):
                g = groups[has_ais]
                e = enrichment(g)
                ci = bootstrap_ci(g, rng) if e is not None else None
                entry["groups"][label] = {
                    "tiles": len(g),
                    "blobs_inside": sum(t["nin"] for t in g),
                    "blobs_outside": sum(t["nout"] for t in g),
                    "inside_sea_km2": round(sum(t["ain"] for t in g), 1),
                    "outside_sea_km2": round(sum(t["aout"] for t in g), 1),
                    "enrichment_ratio": round(e, 2) if e is not None else None,
                    "ci95": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
                }
            report["results"].append(entry)
            w = entry["groups"]["without_ais"]
            print(f"r={radius:.0f}m c={contrast:.0f}dB  without-AIS enrichment="
                  f"{w['enrichment_ratio']} CI95={w['ci95']} ({w['tiles']} tiles)")

    out = Path(__file__).resolve().parent.parent / "audit" / "results" / "enrichment.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nSaved {out}")
    print("Population-level association only. NOT candidate precision or per-target probability.")


if __name__ == "__main__":
    main()
