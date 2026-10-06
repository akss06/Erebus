"""
Erébus API (Round 2 prototype backend).

Serves scored detections (data/scored_*.geojson), a cross-pass cluster view,
a ranked alert list, evidence crops and analyst reviews. Reads files produced
by src/confidence.py (and src/build_gulf_candidates.py when present); it never
calls Earth Engine or GFW at request time, so it runs offline.

The /api/analyze endpoint is the exception: it runs the detection pipeline
on-demand and streams progress via SSE.

If frontend/dist exists it is served at "/", so the whole app is one process:
    uvicorn backend.main:app --port 8000
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from functools import lru_cache
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIST = ROOT / "frontend" / "dist"
REVIEWS_PATH = DATA / "reviews.json"

AREAS = {
    "tuticorin": {
        "name": "Tuticorin outer anchorage",
        "file": "scored_detections.geojson",
        "center": [78.27, 8.785],
        "zoom": 12,
        "overlay": {"image": "/api/overlay/tuticorin.png", "bounds": None},
    },
    "gulf_of_mannar": {
        "name": "Gulf of Mannar / Palk Strait (Jan 2026)",
        "file": "scored_gulf.geojson",
        "center": [79.35, 9.3],
        "zoom": 8,
        "overlay": None,
    },
    "recent": {
        "name": "Gulf of Mannar (Jun–Sep 2026)",
        "file": "scored_recent.geojson",
        "center": [79.0, 9.2],
        "zoom": 8,
        "overlay": None,
    },
}

# Alert priority: lower sorts first. Fixed objects and clutter are never alerts.
ALERT_PRIORITY = {"DARK_CANDIDATE": 0, "VESSEL_CANDIDATE": 1, "ANCHORED_VESSEL": 2, "LOW_CONFIDENCE": 3}

PROVENANCE = {
    "tuticorin": "Real detection (Sentinel-1 + AIS presence)",
    "gulf_of_mannar": "Real detection (Sentinel-1); AIS status from GFW, unverified",
    "recent": "Real detection (Sentinel-1 Jun–Sep 2026); AIS status from GFW, unverified",
}

sys.path.insert(0, str(ROOT / "src"))
import confidence  # noqa: E402  (same scoring code the offline pipeline uses)

app = FastAPI(title="Erébus API")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

_lock = threading.Lock()


def _load_area(area_id: str) -> list[dict]:
    path = DATA / AREAS[area_id]["file"]
    if not path.exists():
        return []
    out = []
    for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
        p = dict(f["properties"])
        p["lon"], p["lat"] = f["geometry"]["coordinates"]
        p["area_id"] = area_id
        p["provenance"] = PROVENANCE[area_id]
        # evidence crop: Round 1 saved crops for the 2026-01-18 pass only
        crop = None
        if area_id == "tuticorin" and p.get("rank") and p["date"] == "2026-01-18":
            crop = f"detection_{p['rank']}_crop.png"
        elif p.get("crop"):
            crop = p["crop"]
        p["crop_url"] = f"/api/crop/{p['id']}" if crop else None
        p["_crop_file"] = crop
        out.append(p)
    return out


DETECTIONS: dict[str, dict] = {}
for _area in AREAS:
    for _d in _load_area(_area):
        DETECTIONS[_d["id"]] = _d


def _simulated(mmsi: str) -> dict[str, dict]:
    """Re-score the Tuticorin passes as if one vessel's AIS records did not exist.

    The radar detections are the real ones; only the AIS side is altered. Detections whose nearest AIS vessel was
    `mmsi` become UNMATCHED and everything (including cross-pass persistence) is scored again."""
    return _simulated_cached(mmsi)


@lru_cache(maxsize=16)
def _simulated_cached(mmsi: str) -> dict[str, dict]:
    name = None
    passes: dict[str, list[dict]] = defaultdict(list)
    for d in DETECTIONS.values():
        if d["area_id"] != "tuticorin":
            continue
        x = dict(d)
        if x.get("matched_mmsi") == mmsi:
            name = x.get("matched_name") or name
            x.update(match_status="UNMATCHED", matched_mmsi=None, matched_name=None, matched_flag=None,
                     matched_type=None, match_distance_m=None, simulated=True)
        passes[x["date"]].append(x)
    out = {d["id"]: d for d in DETECTIONS.values() if d["area_id"] != "tuticorin"}
    for x in confidence.score_all(passes):
        if x.get("simulated"):
            x["provenance"] = f"SIMULATION: the AIS record of {name} was removed. The radar detection is real."
        out[x["id"]] = x
    return out


def _store(sim: str | None) -> dict[str, dict]:
    return _simulated(sim) if sim else DETECTIONS


@app.get("/api/simulation/vessels")
def simulation_vessels() -> list[dict]:
    """AIS vessels in the Tuticorin data that could be 'switched off' for the demo."""
    seen: dict[str, dict] = {}
    for d in DETECTIONS.values():
        m = d.get("matched_mmsi")
        if d["area_id"] == "tuticorin" and m and d["confidence_class"] in ("VESSEL_CANDIDATE", "ANCHORED_VESSEL"):
            v = seen.setdefault(m, {"mmsi": m, "name": d.get("matched_name"), "detections": 0})
            v["detections"] += 1
    return sorted(seen.values(), key=lambda v: -v["detections"])


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _load_reviews() -> dict[str, dict]:
    if REVIEWS_PATH.exists():
        return json.loads(REVIEWS_PATH.read_text(encoding="utf-8"))
    return {}


class Review(BaseModel):
    detection_id: str
    verdict: Literal["confirm", "reject", "unsure"]
    note: str = Field(default="", max_length=500)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "detections": len(DETECTIONS)}


@app.get("/api/areas")
def areas() -> list[dict]:
    out = []
    for area_id, meta in AREAS.items():
        dets = [d for d in DETECTIONS.values() if d["area_id"] == area_id]
        overlay = meta["overlay"]
        if overlay:
            bounds = json.loads((DATA / "overlay_bounds.json").read_text(encoding="utf-8"))["bounds"]
            overlay = {**overlay, "bounds": bounds}
        out.append({
            "id": area_id, "name": meta["name"], "center": meta["center"], "zoom": meta["zoom"],
            "overlay": overlay, "detections": len(dets),
            "passes": sorted({d["date"] for d in dets}),
            "classes": dict(Counter(d["confidence_class"] for d in dets)),
        })
    return out


@app.get("/api/detections")
def detections(area: str | None = None, date: str | None = None, min_confidence: int = Query(0, ge=0, le=100), sim: str | None = None) -> list[dict]:
    rows = [d for d in _store(sim).values()
            if (area is None or d["area_id"] == area) and (date is None or d["date"] == date)
            and d["confidence"] >= min_confidence]
    return [_public(d) for d in sorted(rows, key=lambda d: d["id"])]


@app.get("/api/detections/{det_id}")
def detection(det_id: str, sim: str | None = None) -> dict:
    store = _store(sim)
    d = store.get(det_id)
    if not d:
        raise HTTPException(404, "unknown detection")
    siblings = [_public(x) for x in store.values()
                if x.get("cluster_id") == d.get("cluster_id") and x["area_id"] == d["area_id"] and x["id"] != det_id]
    return {**_public(d), "other_passes": sorted(siblings, key=lambda x: x["date"]),
            "review": _load_reviews().get(det_id)}


@app.get("/api/clusters")
def clusters(area: str | None = None, sim: str | None = None) -> list[dict]:
    """One entry per physical location seen across passes - the 'all passes on one screen' view."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for d in _store(sim).values():
        if area is None or d["area_id"] == area:
            groups[(d["area_id"], d.get("cluster_id", d["id"]))].append(d)
    out = []
    for (area_id, cid), members in groups.items():
        best = max(members, key=lambda m: m["confidence"])
        out.append({
            "cluster_id": f"{area_id}:{cid}", "area_id": area_id,
            "lon": sum(m["lon"] for m in members) / len(members),
            "lat": sum(m["lat"] for m in members) / len(members),
            "confidence_class": best["confidence_class"], "confidence": best["confidence"],
            "passes_seen": len(members), "dates": sorted(m["date"] for m in members),
            "best_detection_id": best["id"], "reasons": best["reasons"],
            "matched_names": sorted({m["matched_name"] for m in members if m.get("matched_name")}),
            "provenance": best["provenance"],
            "simulated": any(m.get("simulated") for m in members),
        })
    return out


@app.get("/api/alerts")
def alerts(area: str | None = None, limit: int = Query(50, ge=1, le=200), sim: str | None = None) -> list[dict]:
    """Ranked review queue: one row per location, most suspicious first. Fixed objects and clutter are excluded."""
    reviews = _load_reviews()
    rows = [c for c in clusters(area, sim) if c["confidence_class"] in ALERT_PRIORITY]
    for c in rows:
        r = reviews.get(c["best_detection_id"])
        c["review"] = r
    rows.sort(key=lambda c: (ALERT_PRIORITY[c["confidence_class"]], -c["confidence"]))
    return rows[:limit]


@app.get("/api/crop/{det_id}")
def crop(det_id: str) -> FileResponse:
    d = DETECTIONS.get(det_id)
    fname = d and d.get("_crop_file")
    if not fname:
        raise HTTPException(404, "no evidence crop for this detection")
    search = [DATA, DATA / "validation" / "crops", DATA / "recent" / "crops"]
    if d.get("area_id", "").startswith("analysis_"):
        rid = d["area_id"].replace("analysis_", "", 1)
        search.insert(0, DATA / "runs" / rid / "crops")
    for base in search:
        path = base / fname
        if path.exists():
            return FileResponse(path)
    raise HTTPException(404, "crop file missing")


@app.get("/api/overlay/tuticorin.png")
def overlay() -> FileResponse:
    return FileResponse(DATA / "overlay_rgba.png")


@app.get("/api/reviews")
def list_reviews() -> dict[str, dict]:
    return _load_reviews()


@app.post("/api/reviews")
def add_review(review: Review) -> dict:
    if review.detection_id not in DETECTIONS:
        raise HTTPException(404, "unknown detection")
    with _lock:
        reviews = _load_reviews()
        reviews[review.detection_id] = review.model_dump()
        REVIEWS_PATH.write_text(json.dumps(reviews, indent=2), encoding="utf-8")
    return reviews[review.detection_id]


# ---- Live analysis endpoint ----

class AnalyzeRequest(BaseModel):
    region: list[float] = Field(..., min_length=4, max_length=4, description="[lon0, lat0, lon1, lat1]")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    max_per_date: int = Field(default=10, ge=1, le=30)

_runs: dict[str, dict] = {}


def _run_analysis(run_id: str, req: AnalyzeRequest) -> None:
    """Run the detection pipeline in a background thread, updating _runs[run_id]."""
    import datetime as dt

    run = _runs[run_id]
    run["status"] = "running"

    try:
        run["log"].append("Importing pipeline modules...")
        import fetch_ais
        import fetch_sar
        import confidence as conf
        import match as match_mod
        import validate_vs_gfw as vgfw

        run["log"].append("Authenticating with Earth Engine and GFW...")
        token = fetch_ais.get_token()
        fetch_sar.authenticate_and_init(vgfw.PROJECT_ID)

        run_dir = DATA / "runs" / run_id
        crop_dir = run_dir / "crops"
        run_dir.mkdir(parents=True, exist_ok=True)
        crop_dir.mkdir(exist_ok=True)

        # Build date list: every 12 days (Sentinel-1 repeat cycle) within the range
        d0 = dt.date.fromisoformat(req.start_date)
        d1 = dt.date.fromisoformat(req.end_date)
        dates = []
        d = d0
        while d <= d1:
            dates.append(d.isoformat())
            d += dt.timedelta(days=12)
        if not dates:
            dates = [req.start_date]
        run["log"].append(f"Date range: {req.start_date} to {req.end_date} ({len(dates)} passes)")

        region = tuple(req.region)
        tuticorin_box = (78.20, 8.70, 78.34, 8.87)
        tile_half = vgfw.TILE_HALF_DEG
        match_radius = vgfw.MATCH_RADIUS_M

        # Fetch GFW SAR detections
        all_gfw: dict[str, list[dict]] = {}
        for date in dates:
            run["log"].append(f"Fetching GFW SAR detections for {date}...")
            try:
                import requests
                d_obj = dt.date.fromisoformat(date)
                lo = (d_obj - dt.timedelta(days=1)).isoformat()
                hi = (d_obj + dt.timedelta(days=1)).isoformat()
                lon0, lat0, lon1, lat1 = region
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
                all_gfw[date] = records
                no_ais = sum(1 for g in records if not g.get("mmsi"))
                run["log"].append(f"  {len(records)} SAR detections ({no_ais} without AIS)")
            except Exception as e:
                run["log"].append(f"  GFW fetch failed for {date}: {e}")
                all_gfw[date] = []

        # Download tiles and run detector
        import numpy as np
        from PIL import Image, ImageDraw
        import rasterio

        picked_dets: dict[str, list[dict]] = {}
        total_tiles = sum(min(len(gfw), req.max_per_date) for gfw in all_gfw.values())
        processed = 0

        for date in dates:
            gfw = all_gfw[date]
            if not gfw:
                continue
            step = max(1, len(gfw) // req.max_per_date)
            picked = list(enumerate(gfw))[::step][:req.max_per_date]

            for i, g in picked:
                processed += 1
                lon, lat = g["lon"], g["lat"]
                run["log"].append(f"[{processed}/{total_tiles}] Processing tile at {lon:.3f}, {lat:.3f} on {date}")
                run["progress"] = round(processed / max(total_tiles, 1) * 80)

                tif = run_dir / f"{date}_{i:03d}.tif"
                if not tif.exists():
                    scene = vgfw.find_scene(lon, lat, date)
                    if scene is None:
                        continue
                    aoi = fetch_sar.build_aoi(
                        lon - tile_half, lat - tile_half,
                        lon + tile_half, lat + tile_half,
                    )
                    if not vgfw.download_with_retry(aoi, scene, tif):
                        continue

                dets, _ = vgfw.run_detector_on_tile(tif)
                dists = [match_mod.haversine_m(lon, lat, d["lon"], d["lat"]) for d in dets]
                nearest_dist = min(dists) if dists else None
                if nearest_dist is None or nearest_dist > match_radius:
                    continue

                if tuticorin_box[0] <= lon <= tuticorin_box[2] and tuticorin_box[1] <= lat <= tuticorin_box[3]:
                    continue

                near = min(dets, key=lambda d: match_mod.haversine_m(lon, lat, d["lon"], d["lat"]))
                has_ais = bool(g.get("mmsi"))
                near.update({
                    "match_status": "MATCHED" if has_ais else "UNMATCHED",
                    "match_distance_m": round(nearest_dist, 1),
                    "matched_name": g.get("shipName") or None,
                    "matched_mmsi": g.get("mmsi") or None,
                    "matched_flag": g.get("flag") or None,
                    "matched_type": g.get("vesselType") or None,
                    "tif_path": str(tif),
                })
                day = picked_dets.setdefault(date, [])
                if any(match_mod.haversine_m(near["lon"], near["lat"], p["lon"], p["lat"]) < 60 for p in day):
                    continue
                day.append(near)

        run["log"].append("Clustering and scoring detections...")
        run["progress"] = 85

        clusters_list = conf.cluster_across_passes(picked_dets)
        clusters_list.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
        features, per_date = [], Counter()
        for cid, cluster in enumerate(clusters_list, start=1):
            for cdate, det in sorted(cluster, key=lambda m: m[0]):
                res = conf.score_detection(det, cluster, len(dates), corroborated_by="GFW SAR detection")
                per_date[cdate] += 1
                det_id = f"{cdate}-A{per_date[cdate]:02d}"
                tif_path = Path(det.pop("tif_path", ""))
                if tif_path.exists():
                    # Save crop
                    try:
                        with rasterio.open(tif_path) as src:
                            db = src.read(1).astype("float64")
                        r, c = int(round(det["row"])), int(round(det["col"]))
                        r0, r1 = max(0, r - 60), min(db.shape[0], r + 60)
                        c0, c1 = max(0, c - 60), min(db.shape[1], c + 60)
                        crop = db[r0:r1, c0:c1]
                        ok = np.isfinite(crop)
                        if ok.sum() > 0:
                            lo_v, hi_v = np.percentile(crop[ok], [2, 98])
                            if hi_v <= lo_v:
                                hi_v = lo_v + 1
                            img = Image.fromarray(
                                (np.clip((crop - lo_v) / (hi_v - lo_v), 0, 1) * 255).astype("uint8"), mode="L"
                            )
                            img = img.resize((img.width * 4, img.height * 4), Image.NEAREST).convert("RGB")
                            cx, cy = (c - c0 + 0.5) * 4, (r - r0 + 0.5) * 4
                            draw = ImageDraw.Draw(img)
                            draw.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], outline=(255, 60, 60), width=2)
                            img.save(crop_dir / f"{det_id}.png")
                    except Exception:
                        pass

                props = {k: v for k, v in det.items() if k not in ("lon", "lat", "tif_path", "tif")}
                props.update(
                    id=det_id, date=cdate, cluster_id=cid, cluster_size=len(cluster),
                    crop=f"{det_id}.png", **res,
                )
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [det["lon"], det["lat"]]},
                    "properties": props,
                })

        out_path = DATA / f"scored_analysis_{run_id}.geojson"
        out_path.write_text(json.dumps(
            {"type": "FeatureCollection", "features": features}, indent=1,
        ), encoding="utf-8")

        run["progress"] = 95
        run["log"].append(f"Saved {len(features)} detections")

        classes = Counter(f["properties"]["confidence_class"] for f in features)
        dark = sum(1 for f in features if f["properties"]["confidence_class"] == "DARK_CANDIDATE")

        # Register as a new area
        area_id = f"analysis_{run_id}"
        center_lon = (req.region[0] + req.region[2]) / 2
        center_lat = (req.region[1] + req.region[3]) / 2
        area_name = f"Analysis ({req.start_date} to {req.end_date})"
        AREAS[area_id] = {
            "name": area_name,
            "file": f"scored_analysis_{run_id}.geojson",
            "center": [center_lon, center_lat],
            "zoom": 8,
            "overlay": None,
        }
        PROVENANCE[area_id] = f"Live analysis ({req.start_date} to {req.end_date}); AIS status from GFW, unverified"

        for d in _load_area(area_id):
            DETECTIONS[d["id"]] = d

        run["progress"] = 100
        run["status"] = "done"
        run["area_id"] = area_id
        run["result"] = {
            "total": len(features),
            "dark_candidates": dark,
            "classes": dict(classes),
        }
        run["log"].append(f"Done: {dark} dark-vessel candidates out of {len(features)} detections")

    except Exception as e:
        run["status"] = "error"
        run["error"] = str(e)
        run["log"].append(f"ERROR: {e}")


@app.post("/api/analyze")
def start_analysis(req: AnalyzeRequest) -> dict:
    run_id = uuid.uuid4().hex[:8]
    _runs[run_id] = {
        "status": "starting",
        "progress": 0,
        "log": [],
        "result": None,
        "area_id": None,
        "error": None,
    }
    t = threading.Thread(target=_run_analysis, args=(run_id, req), daemon=True)
    t.start()
    return {"run_id": run_id}


@app.get("/api/analyze/{run_id}")
def analysis_status(run_id: str) -> dict:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    return {
        "status": run["status"],
        "progress": run["progress"],
        "log": run["log"][-20:],
        "result": run["result"],
        "area_id": run["area_id"],
        "error": run["error"],
    }


@app.get("/api/analyze/{run_id}/stream")
async def analysis_stream(run_id: str, request: Request):
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, "unknown run")

    import asyncio

    async def event_generator():
        last_len = 0
        last_progress = -1
        while True:
            if await request.is_disconnected():
                break
            log = run["log"]
            progress = run["progress"]
            status = run["status"]

            if len(log) > last_len or progress != last_progress:
                new_lines = log[last_len:]
                last_len = len(log)
                last_progress = progress
                data = json.dumps({
                    "status": status,
                    "progress": progress,
                    "log": new_lines,
                    "result": run["result"],
                    "area_id": run["area_id"],
                    "error": run["error"],
                })
                yield f"data: {data}\n\n"

            if status in ("done", "error"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
