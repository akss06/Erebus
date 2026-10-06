"""
Run the full detection pipeline on recent Sentinel-1 passes.

Uses the same validated CA-CFAR detector and GFW cross-reference as the January
analysis, but on Sep/Oct 2026 data to show the system works on current imagery.

Run from the project root:  python src/run_recent.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confidence
import match
import validate_vs_gfw as vgfw

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RECENT_DIR = DATA / "recent"
CROP_DIR = RECENT_DIR / "crops"
OUT = DATA / "scored_recent.geojson"

RECENT_DATES = ["2026-09-10", "2026-09-22", "2026-10-04"]

REGION = vgfw.REGION
MATCH_RADIUS_M = vgfw.MATCH_RADIUS_M
TUTICORIN_BOX = (78.20, 8.70, 78.34, 8.87)
DEDUPE_M = 60
CROP_HALF_PX = 60
CROP_SCALE = 4


def fetch_gfw_sar_recent(token: str, date: str) -> list[dict]:
    """Same as validate_vs_gfw.fetch_gfw_sar but caches in data/recent/."""
    import datetime as dt
    import requests
    import fetch_ais

    cache = RECENT_DIR / f"gfw_sar_{date}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    d = dt.date.fromisoformat(date)
    lo = (d - dt.timedelta(days=1)).isoformat()
    hi = (d + dt.timedelta(days=1)).isoformat()
    lon0, lat0, lon1, lat1 = REGION
    geo = {"type": "Polygon", "coordinates": [
        [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]
    ]}
    params = {
        "datasets[0]": "public-global-sar-presence:latest",
        "date-range": f"{lo},{hi}",
        "temporal-resolution": "DAILY",
        "spatial-resolution": "HIGH",
        "format": "JSON",
        "group-by": "VESSEL_ID",
    }
    resp = requests.post(
        f"{fetch_ais.GFW_API_BASE}/4wings/report",
        headers=fetch_ais._headers(token),
        params=params,
        json={"geojson": geo},
        timeout=180,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries", [{}])[0].get("public-global-sar-presence:v4.0") or []
    records = [e for e in entries if e.get("date") == date]
    RECENT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(records))
    return records


def ensure_tile(date: str, i: int, lon: float, lat: float) -> Path | None:
    tif = RECENT_DIR / f"{date}_{i:03d}.tif"
    if tif.exists():
        return tif
    scene = vgfw.find_scene(lon, lat, date)
    if scene is None:
        print(f"  [skip] no scene for {lon:.3f},{lat:.3f} on {date}", flush=True)
        return None
    aoi = vgfw.fetch_sar.build_aoi(
        lon - vgfw.TILE_HALF_DEG, lat - vgfw.TILE_HALF_DEG,
        lon + vgfw.TILE_HALF_DEG, lat + vgfw.TILE_HALF_DEG,
    )
    if not vgfw.download_with_retry(aoi, scene, tif):
        print(f"  [skip] download failed for {lon:.3f},{lat:.3f} on {date}", flush=True)
        return None
    return tif


def save_crop(tif: Path, row: float, col: float, out: Path) -> None:
    with rasterio.open(tif) as src:
        db = src.read(1).astype("float64")
    r, c = int(round(row)), int(round(col))
    r0, r1 = max(0, r - CROP_HALF_PX), min(db.shape[0], r + CROP_HALF_PX)
    c0, c1 = max(0, c - CROP_HALF_PX), min(db.shape[1], c + CROP_HALF_PX)
    crop = db[r0:r1, c0:c1]
    ok = np.isfinite(crop)
    if ok.sum() == 0:
        return
    lo, hi = np.percentile(crop[ok], [2, 98])
    if hi <= lo:
        hi = lo + 1
    img = Image.fromarray(
        (np.clip((crop - lo) / (hi - lo), 0, 1) * 255).astype("uint8"), mode="L"
    )
    img = img.resize((img.width * CROP_SCALE, img.height * CROP_SCALE), Image.NEAREST).convert("RGB")
    cx, cy = (c - c0 + 0.5) * CROP_SCALE, (r - r0 + 0.5) * CROP_SCALE
    d = ImageDraw.Draw(img)
    d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], outline=(255, 60, 60), width=2)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    img.save(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-date", type=int, default=15,
                        help="max GFW detections to process per date")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    import fetch_ais
    import fetch_sar
    token = fetch_ais.get_token()
    fetch_sar.authenticate_and_init(vgfw.PROJECT_ID)
    RECENT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: fetch GFW SAR detections for each date
    all_gfw: dict[str, list[dict]] = {}
    for date in RECENT_DATES:
        gfw = fetch_gfw_sar_recent(token, date)
        all_gfw[date] = gfw
        no_ais = sum(1 for g in gfw if not g.get("mmsi"))
        print(f"[gfw] {date}: {len(gfw)} SAR detections ({no_ais} without AIS)", flush=True)

    # Step 2: download tiles (sample if too many)
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for date in RECENT_DATES:
        gfw = all_gfw[date]
        step = max(1, len(gfw) // args.max_per_date)
        picked = list(enumerate(gfw))[::step][:args.max_per_date]
        for i, g in picked:
            jobs.append((date, i, g))

    print(f"[download] {len(jobs)} tiles", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tifs = list(pool.map(
            lambda j: ensure_tile(j[0], j[1], j[2]["lon"], j[2]["lat"]),
            jobs,
        ))

    # Step 3: run detector + build scored candidates
    picked_dets: dict[str, list[dict]] = {}
    found_count, miss_count = 0, 0
    for (date, i, g), tif in zip(jobs, tifs):
        if tif is None:
            miss_count += 1
            continue
        lon, lat = g["lon"], g["lat"]
        dets, _ = vgfw.run_detector_on_tile(tif)
        dists = [match.haversine_m(lon, lat, d["lon"], d["lat"]) for d in dets]
        nearest_dist = min(dists) if dists else None
        hit = nearest_dist is not None and nearest_dist <= MATCH_RADIUS_M

        if not hit:
            miss_count += 1
            continue

        found_count += 1
        if TUTICORIN_BOX[0] <= lon <= TUTICORIN_BOX[2] and TUTICORIN_BOX[1] <= lat <= TUTICORIN_BOX[3]:
            continue

        near = min(dets, key=lambda d: match.haversine_m(lon, lat, d["lon"], d["lat"]))
        has_ais = bool(g.get("mmsi"))
        near.update({
            "match_status": "MATCHED" if has_ais else "UNMATCHED",
            "match_distance_m": round(nearest_dist, 1),
            "matched_name": g.get("shipName") or None,
            "matched_mmsi": g.get("mmsi") or None,
            "matched_flag": g.get("flag") or None,
            "matched_type": g.get("vesselType") or None,
            "tile": str(tif),
            "tif_path": str(tif),
        })
        day = picked_dets.setdefault(date, [])
        if any(match.haversine_m(near["lon"], near["lat"], p["lon"], p["lat"]) < DEDUPE_M for p in day):
            continue
        day.append(near)

    print(f"\n[detect] {found_count} tiles matched, {miss_count} missed/skipped", flush=True)

    # Step 4: cluster + score + save crops
    clusters = confidence.cluster_across_passes(picked_dets)
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    features, per_date = [], Counter()
    for cid, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            res = confidence.score_detection(
                det, cluster, len(RECENT_DATES), corroborated_by="GFW SAR detection"
            )
            per_date[date] += 1
            det_id = f"{date}-R{per_date[date]:02d}"
            tif_path = Path(det.pop("tif", det.pop("tif_path", "")))
            if tif_path.exists():
                save_crop(tif_path, det["row"], det["col"], CROP_DIR / f"{det_id}.png")
            props = {k: v for k, v in det.items() if k not in ("lon", "lat", "tif_path")}
            props.update(
                id=det_id, date=date, cluster_id=cid, cluster_size=len(cluster),
                crop=f"{det_id}.png", **res,
            )
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [det["lon"], det["lat"]]},
                "properties": props,
            })

    OUT.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features}, indent=1,
    ), encoding="utf-8")

    classes = Counter(f["properties"]["confidence_class"] for f in features)
    print(f"\n{len(features)} scored detections -> {OUT.name}")
    print("Classes:", dict(classes))
    dark = [f for f in features if f["properties"]["confidence_class"] == "DARK_CANDIDATE"]
    print(f"\nDARK_CANDIDATE count: {len(dark)}")
    for f in dark:
        p = f["properties"]
        print(f"  {p['id']}  conf={p['confidence']}  {p['contrast_db']:.1f} dB  "
              f"{p['area_px']} px  @ {f['geometry']['coordinates'][0]:.3f},"
              f"{f['geometry']['coordinates'][1]:.3f}")


if __name__ == "__main__":
    main()
