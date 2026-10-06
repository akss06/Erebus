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
import detect_v3
import match
import validate_vs_gfw as vgfw

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RECENT_DIR = DATA / "recent"
CROP_DIR = RECENT_DIR / "crops"
OUT = DATA / "scored_recent.geojson"

RECENT_DATES = [
    "2026-06-30", "2026-07-12", "2026-07-24",
    "2026-08-05", "2026-08-17", "2026-08-29",
    "2026-09-10", "2026-09-22",
]

REGION = vgfw.REGION
MATCH_RADIUS_M = vgfw.MATCH_RADIUS_M
TUTICORIN_BOX = (78.20, 8.70, 78.34, 8.87)
DEDUPE_M = 60
CROP_HALF_PX = 60
CROP_SCALE = 4
# Fixed dB window for crops. A per-crop percentile stretch gets hijacked by any
# bright streak (azimuth ambiguity / stronger scatterer) in frame and washes the
# real target down to grey. A fixed VV window (open water ~ -20 dB -> near black,
# a vessel ~ 0 dB -> white) renders targets honestly and comparably across crops.
CROP_DB_LO = -23.0
CROP_DB_HI = 3.0


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
    """Download (once) the VV tile and its VH companion around a hotspot. VV is
    required; VH is best-effort (used only for cross-pol corroboration)."""
    tif = RECENT_DIR / f"{date}_{i:03d}.tif"
    vh = RECENT_DIR / f"{date}_{i:03d}_vh.tif"
    scene = aoi = None

    if not tif.exists():
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

    if not vh.exists():
        if scene is None:
            scene = vgfw.find_scene(lon, lat, date)
        if scene is not None:
            if aoi is None:
                aoi = vgfw.fetch_sar.build_aoi(
                    lon - vgfw.TILE_HALF_DEG, lat - vgfw.TILE_HALF_DEG,
                    lon + vgfw.TILE_HALF_DEG, lat + vgfw.TILE_HALF_DEG,
                )
            try:
                vgfw.fetch_sar.download_scene(aoi, scene, vh, scale=vgfw.SCALE_M, band="VH")
            except Exception as e:
                print(f"  [vh skip] {date}_{i:03d}: {type(e).__name__}", flush=True)
    return tif


def save_crop(tif: Path, row: float, col: float, out: Path) -> None:
    with rasterio.open(tif) as src:
        db = src.read(1).astype("float64")
    r, c = int(round(row)), int(round(col))
    r0, r1 = max(0, r - CROP_HALF_PX), min(db.shape[0], r + CROP_HALF_PX)
    c0, c1 = max(0, c - CROP_HALF_PX), min(db.shape[1], c + CROP_HALF_PX)
    crop = db[r0:r1, c0:c1]
    if not np.isfinite(crop).any():
        return
    lo, hi = CROP_DB_LO, CROP_DB_HI
    img = Image.fromarray(
        (np.clip((np.nan_to_num(crop, nan=lo) - lo) / (hi - lo), 0, 1) * 255).astype("uint8"), mode="L"
    )
    img = img.resize((img.width * CROP_SCALE, img.height * CROP_SCALE), Image.NEAREST).convert("RGB")
    cx, cy = (c - c0 + 0.5) * CROP_SCALE, (r - r0 + 0.5) * CROP_SCALE
    d = ImageDraw.Draw(img)
    d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], outline=(255, 60, 60), width=2)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    img.save(out)


HOTSPOT_CLUSTER_M = 3000  # cluster GFW locations within 3 km into one hotspot


def cluster_gfw_locations(all_gfw: dict[str, list[dict]], max_hotspots: int) -> list[dict]:
    """Collect GFW detections from all dates and cluster nearby ones into hotspots."""
    all_pts: list[dict] = []
    for date, records in all_gfw.items():
        for g in records:
            all_pts.append({"lon": g["lon"], "lat": g["lat"], "date": date, **g})

    # Greedy spatial clustering
    used = [False] * len(all_pts)
    hotspots: list[dict] = []
    for i, p in enumerate(all_pts):
        if used[i]:
            continue
        members = [p]
        used[i] = True
        for j in range(i + 1, len(all_pts)):
            if used[j]:
                continue
            if match.haversine_m(p["lon"], p["lat"], all_pts[j]["lon"], all_pts[j]["lat"]) <= HOTSPOT_CLUSTER_M:
                members.append(all_pts[j])
                used[j] = True
        lon = sum(m["lon"] for m in members) / len(members)
        lat = sum(m["lat"] for m in members) / len(members)
        dates_seen = set(m["date"] for m in members)
        has_ais_any = any(m.get("mmsi") for m in members)
        hotspots.append({
            "lon": lon, "lat": lat,
            "dates_seen": dates_seen, "n_dates": len(dates_seen),
            "members": members, "has_ais": has_ais_any,
        })

    # Prioritize: multi-date hotspots first, then those without AIS
    hotspots.sort(key=lambda h: (-h["n_dates"], h["has_ais"]))
    return hotspots[:max_hotspots]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-hotspots", type=int, default=20,
                        help="max GFW hotspot locations to process")
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

    # Step 2: cluster GFW locations into hotspots, then download tiles
    # at the SAME locations for EVERY date to get multi-pass coverage
    hotspots = cluster_gfw_locations(all_gfw, args.max_hotspots)
    print(f"\n[hotspots] {len(hotspots)} locations (top by multi-date presence)", flush=True)
    for h in hotspots[:5]:
        print(f"  {h['lon']:.3f},{h['lat']:.3f}  seen on {h['n_dates']} dates  ais={'Y' if h['has_ais'] else 'N'}", flush=True)

    from concurrent.futures import ThreadPoolExecutor
    jobs: list[tuple[str, int, float, float]] = []
    for hi, h in enumerate(hotspots):
        for date in RECENT_DATES:
            jobs.append((date, hi, h["lon"], h["lat"]))

    print(f"\n[download] {len(jobs)} tiles ({len(hotspots)} locations x {len(RECENT_DATES)} dates)", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tifs = list(pool.map(
            lambda j: ensure_tile(j[0], j[1] * 1000 + RECENT_DATES.index(j[0]), j[2], j[3]),
            jobs,
        ))

    # Step 3: run detector on each tile and build per-date detections
    # For each hotspot, find GFW records on each date to determine AIS status
    gfw_by_date: dict[str, list[dict]] = all_gfw
    picked_dets: dict[str, list[dict]] = {}
    tile_ok, tile_miss = 0, 0

    for (date, hi, lon, lat), tif in zip(jobs, tifs):
        if tif is None:
            tile_miss += 1
            continue

        if TUTICORIN_BOX[0] <= lon <= TUTICORIN_BOX[2] and TUTICORIN_BOX[1] <= lat <= TUTICORIN_BOX[3]:
            continue

        vh_tif = tif.with_name(tif.stem + "_vh.tif")
        dets = detect_v3.run_detector_v3(tif, vh_tif)
        if not dets:
            tile_miss += 1
            continue

        tile_ok += 1
        # Find nearest GFW record on this date for AIS status
        gfw_on_date = gfw_by_date.get(date, [])
        for det in dets:
            # Only keep detections near the hotspot center (within tile)
            if match.haversine_m(lon, lat, det["lon"], det["lat"]) > MATCH_RADIUS_M * 2:
                continue

            # Find nearest GFW record for AIS info
            gfw_dists = [(match.haversine_m(det["lon"], det["lat"], g["lon"], g["lat"]), g) for g in gfw_on_date]
            nearest_gfw = min(gfw_dists, key=lambda x: x[0]) if gfw_dists else None

            if nearest_gfw and nearest_gfw[0] <= MATCH_RADIUS_M:
                g = nearest_gfw[1]
                has_ais = bool(g.get("mmsi"))
                det.update({
                    "match_status": "MATCHED" if has_ais else "UNMATCHED",
                    "match_distance_m": round(nearest_gfw[0], 1),
                    "matched_name": g.get("shipName") or None,
                    "matched_mmsi": g.get("mmsi") or None,
                    "matched_flag": g.get("flag") or None,
                    "matched_type": g.get("vesselType") or None,
                })
            else:
                det.update(match_status="UNMATCHED", match_distance_m=None,
                           matched_name=None, matched_mmsi=None,
                           matched_flag=None, matched_type=None)

            det["tif_path"] = str(tif)
            day = picked_dets.setdefault(date, [])
            if any(match.haversine_m(det["lon"], det["lat"], p["lon"], p["lat"]) < DEDUPE_M for p in day):
                continue
            day.append(det)

    total_dets = sum(len(v) for v in picked_dets.values())
    print(f"\n[detect] {tile_ok} tiles processed, {tile_miss} missed; {total_dets} detections "
          f"(land mask, GG-CFAR, TCR + shape gates applied in detector)", flush=True)

    # Step 4: cluster + score + save crops
    clusters = confidence.cluster_across_passes(picked_dets)
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    features, per_date = [], Counter()
    for cid, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            # Corroboration only when GFW independently reported a SAR detection
            # within match radius of THIS detection (match_distance_m set), not
            # blanket across the whole hotspot tile.
            corrob = "GFW SAR detection" if det.get("match_distance_m") is not None else None
            res = confidence.score_detection(
                det, cluster, len(RECENT_DATES), corroborated_by=corrob
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
