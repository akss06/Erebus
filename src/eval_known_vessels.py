"""
Known-vessel evaluation for the recent (Jun-Sep 2026, v3) Gulf of Mannar set.

Reference set: GFW Sentinel-1 SAR detections that GFW attributed to an AIS
identity (ship name / MMSI). These are real, broadcasting vessels, so they give a
recall measurement that needs no hand labels. Scope caveats (state them with the
numbers):
  * reference = ships that broadcast AIS AND that GFW's detector saw on the same
    image -- biased towards easy, larger targets; says nothing about non-AIS boats
  * GFW positions are snapped to a 0.01 deg (~1.1 km) grid, so "location error"
    is bounded by that grid, not a precise accuracy figure
  * sea roughness is a radar-backscatter proxy (tile median sea sigma0), not wind

Fully offline: uses the cached tiles, JRC caches and GFW records in data/recent.

Run from the project root:  python src/eval_known_vessels.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_v3
import match
import run_recent

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RECENT = DATA / "recent"
OUT = ROOT / "audit" / "results" / "known_vessel_eval.json"

RADII_M = [500, 1000, 1500]
MAIN_R = 1000          # ~ GFW grid half-diagonal (0.78 km) plus margin
# The review queue = everything the analyst sees (backend ALERT_PRIORITY).
NOT_IN_QUEUE = {"CLUTTER", "FIXED_OBJECT"}
SHORE_BINS = [(0, 1000, "<1 km (shore buffer)"), (1000, 5000, "1-5 km"),
              (5000, 20000, "5-20 km"), (20000, math.inf, ">20 km")]


def vessel_group(rec: dict) -> str:
    t = f"{rec.get('vesselType', '')} {rec.get('geartype', '')}".upper()
    if any(k in t for k in ("CARGO", "TANKER", "CONTAINER", "BULK", "CARRIER")):
        return "cargo/tanker (large)"
    if "FISH" in t or "TRAWL" in t:
        return "fishing"
    return "other/unknown type"


def load_land_utm(crs) -> object:
    """Natural Earth land clipped to the region, projected to the tile CRS."""
    import geopandas as gpd
    lon0, lat0, lon1, lat1 = run_recent.REGION
    land = gpd.read_file(DATA / "ne_10m_land.zip").clip(box(lon0 - 1, lat0 - 1, lon1 + 1, lat1 + 1))
    return land.to_crs(crs).union_all()


def main() -> None:
    dets = json.loads((DATA / "scored_recent.geojson").read_text(encoding="utf-8"))["features"]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for f in dets:
        lon, lat = f["geometry"]["coordinates"][:2]
        by_date[f["properties"]["date"]].append({**f["properties"], "lon": lon, "lat": lat})

    tiles: dict[str, list[dict]] = defaultdict(list)   # date -> tile info
    land_utm, to_utm, tile_crs = None, None, None
    for tif in sorted(RECENT.glob("2026-*_*.tif")):
        if tif.stem.endswith(("_jrc", "_vh")):
            continue
        date, idx = tif.stem.split("_")[:2]
        # run_recent names tiles hotspot*1000 + date index; anything else is a leftover
        # from an older run that never fed scored_recent.geojson.
        if date not in run_recent.RECENT_DATES or int(idx) % 1000 != run_recent.RECENT_DATES.index(date):
            continue
        if not tif.with_name(tif.stem + "_jrc.tif").exists():
            continue   # no cached JRC mask -> tile was not analysed with the v3 mask; skip (stay offline)
        db, sea, transform, crs, info = detect_v3.build_sea_mask(tif)
        if not info["jrc_used"]:
            continue   # mask would differ from the run; skip rather than guess
        if tile_crs is None:
            tile_crs = crs
            to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            land_utm = load_land_utm(crs)
        with rasterio.open(tif) as src:
            bounds = src.bounds
        tiles[date].append({"tif": tif.name, "sea": sea, "transform": transform, "crs": crs,
                            "bounds": bounds, "sea_km2": info["sea_km2"],
                            "sea_db_median": float(np.median(db[sea])) if sea.any() else None})
    print(f"[tiles] {sum(len(v) for v in tiles.values())} tiles with JRC mask across {len(tiles)} dates")

    rows = []
    for date in run_recent.RECENT_DATES:
        gfw_path = RECENT / f"gfw_sar_{date}.json"
        if not gfw_path.exists():
            continue
        for rec in json.loads(gfw_path.read_text(encoding="utf-8")):
            if not rec.get("mmsi"):
                continue   # only AIS-identified vessels are "known"
            x, y = to_utm.transform(rec["lon"], rec["lat"])
            tile = next((t for t in tiles.get(date, []) if t["bounds"].left <= x <= t["bounds"].right
                         and t["bounds"].bottom <= y <= t["bounds"].top), None)
            if tile is None:
                continue   # no tile analysed here on this date: not part of the test
            r, c = rasterio.transform.rowcol(tile["transform"], x, y)
            in_sea = bool(0 <= r < tile["sea"].shape[0] and 0 <= c < tile["sea"].shape[1] and tile["sea"][r, c])
            # run_recent keeps only detections within 2 x match radius of the hotspot (tile
            # centre); a ship up to MAIN_R beyond that can still be matched by a kept one.
            tb = tile["bounds"]
            in_keep = math.hypot(x - (tb.left + tb.right) / 2, y - (tb.bottom + tb.top) / 2) \
                <= 2 * run_recent.MATCH_RADIUS_M + MAIN_R
            shore_m = float(land_utm.distance(Point(x, y)))
            tb = tile["bounds"]
            tile_dets = [d for d in by_date.get(date, [])
                         if tb.left <= to_utm.transform(d["lon"], d["lat"])[0] <= tb.right
                         and tb.bottom <= to_utm.transform(d["lon"], d["lat"])[1] <= tb.top]
            dists = sorted((match.haversine_m(rec["lon"], rec["lat"], d["lon"], d["lat"]), d) for d in tile_dets)
            q_dists = [(m, d) for m, d in dists if d["confidence_class"] not in NOT_IN_QUEUE]
            n_q = len([d for d in tile_dets if d["confidence_class"] not in NOT_IN_QUEUE])
            rows.append({
                "date": date, "mmsi": rec["mmsi"], "name": rec.get("shipName"),
                "group": vessel_group(rec), "lon": rec["lon"], "lat": rec["lat"],
                "tile": tile["tif"], "in_detector_sea": in_sea, "in_pipeline_keep_area": in_keep, "shore_m": round(shore_m),
                "sea_db_median": tile["sea_db_median"], "tile_sea_km2": tile["sea_km2"],
                "tile_n_dets": len(tile_dets), "tile_n_queue": n_q,
                "nearest_any_m": round(dists[0][0]) if dists else None,
                "nearest_any_class": dists[0][1]["confidence_class"] if dists else None,
                "nearest_queue_m": round(q_dists[0][0]) if q_dists else None,
            })

    def chance(n: int, km2: float, r_m: float) -> float:
        if not km2:
            return 0.0
        return 1 - math.exp(-(n / km2) * math.pi * (r_m / 1000) ** 2)

    def summarise(sub: list[dict], r_m: float = MAIN_R) -> dict:
        n = len(sub)
        hit_any = sum(1 for x in sub if x["nearest_any_m"] is not None and x["nearest_any_m"] <= r_m)
        hit_q = sum(1 for x in sub if x["nearest_queue_m"] is not None and x["nearest_queue_m"] <= r_m)
        exp_any = sum(chance(x["tile_n_dets"], x["tile_sea_km2"], r_m) for x in sub)
        exp_q = sum(chance(x["tile_n_queue"], x["tile_sea_km2"], r_m) for x in sub)
        return {"n": n, "detected_any": hit_any, "detected_in_queue": hit_q,
                "chance_expected_any": round(exp_any, 1), "chance_expected_queue": round(exp_q, 1)}

    in_sea = [x for x in rows if x["in_detector_sea"]]
    out_sea = [x for x in rows if not x["in_detector_sea"]]
    result: dict = {
        "scope": "GFW SAR detections with an AIS identity, recent Jun-Sep 2026 tiles (v3 detector)",
        "match_radius_m": MAIN_R,
        "reference_total_in_tiles": len(rows),
        "reference_outside_detector_sea_mask": len(out_sea),
        "overall_in_sea": summarise(in_sea),
        "within_analysed_area": summarise([x for x in in_sea if x["in_pipeline_keep_area"]]),
        "radius_sensitivity": {r: summarise(in_sea, r) for r in RADII_M},
        "by_vessel_group": {}, "by_shore_distance": {}, "by_sea_roughness": {},
    }
    for g in sorted({x["group"] for x in in_sea}):
        result["by_vessel_group"][g] = summarise([x for x in in_sea if x["group"] == g])
    for lo, hi, label in SHORE_BINS:
        result["by_shore_distance"][label] = summarise([x for x in rows if lo <= x["shore_m"] < hi and x["in_detector_sea"]]) \
            | {"outside_mask": sum(1 for x in rows if lo <= x["shore_m"] < hi and not x["in_detector_sea"])}
    sd = sorted(x["sea_db_median"] for x in in_sea if x["sea_db_median"] is not None)
    if sd:
        t1, t2 = sd[len(sd) // 3], sd[2 * len(sd) // 3]
        for label, f in (("calm (low backscatter)", lambda v: v < t1), ("moderate", lambda v: t1 <= v < t2),
                         ("rough (high backscatter)", lambda v: v >= t2)):
            result["by_sea_roughness"][label] = summarise([x for x in in_sea if f(x["sea_db_median"])])
        result["sea_roughness_tercile_db"] = [round(t1, 1), round(t2, 1)]
    errs = [x["nearest_any_m"] for x in in_sea if x["nearest_any_m"] is not None and x["nearest_any_m"] <= MAIN_R]
    if errs:
        result["position_offset_m"] = {"median": round(float(np.median(errs))), "p90": round(float(np.percentile(errs, 90))),
                                      "note": "offset to GFW's 0.01-deg-snapped position; bounded by that grid (~0.78 km max snap)"}
    result["missed_nearest_class"] = {}
    for x in in_sea:
        if x["nearest_any_m"] is None or x["nearest_any_m"] > MAIN_R:
            result["missed_nearest_class"]["no detection within radius"] = result["missed_nearest_class"].get("no detection within radius", 0) + 1
        elif x["nearest_queue_m"] is None or x["nearest_queue_m"] > MAIN_R:
            k = f"detected but scored {x['nearest_any_class']} (not in queue)"
            result["missed_nearest_class"][k] = result["missed_nearest_class"].get(k, 0) + 1
    result["rows"] = rows

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2, default=str))
    print(f"[out] {OUT}")


if __name__ == "__main__":
    main()
