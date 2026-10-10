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
import os
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
    # Both seasons come from the same v3 detector. Each file was clustered across its own
    # passes; locations are not merged across seasons (5 months apart).
    "gulf_of_mannar": {
        "name": "Gulf of Mannar / Palk Strait (Jan + Jun–Sep 2026)",
        "files": ["scored_gulf.geojson", "scored_recent.geojson"],
        "center": [79.0, 9.2],
        "zoom": 8,
        "overlay": None,
    },
}

# Alert priority: lower sorts first. Confirmed fixed objects and clutter are never
# alerts; PERSISTENT_UNIDENTIFIED is a reviewable alert (unresolved stationary target).
# Review queue (most suspicious first). SUSPECTED_FIXED is heuristic, so it stays
# reviewable; only charted FIXED_OBJECT and CLUTTER are excluded.
ALERT_PRIORITY = {"DARK_CANDIDATE": 0, "UNVERIFIED_TARGET": 1, "PERSISTENT_UNIDENTIFIED": 2,
                  "SUSPECTED_FIXED": 3, "VESSEL_CANDIDATE": 4, "ANCHORED_VESSEL": 5, "LOW_CONFIDENCE": 6}

PROVENANCE = {
    "tuticorin": "Real detection (Sentinel-1 + AIS presence)",
    "gulf_of_mannar": "Real detection (Sentinel-1); AIS status from GFW, unverified",
}

sys.path.insert(0, str(ROOT / "src"))
import confidence  # noqa: E402  (same scoring code the offline pipeline uses)

app = FastAPI(title="Erébus API")
# Allowed browser origins: local dev by default; in production set ALLOWED_ORIGINS
# (comma-separated) to the deployed frontend origin(s), e.g. the Vercel URL.
_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:4173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)

_lock = threading.Lock()


def _load_area(area_id: str) -> list[dict]:
    files = AREAS[area_id].get("files") or [AREAS[area_id]["file"]]
    feats = []
    for name in files:
        path = DATA / name
        if path.exists():
            # namespace cluster ids by file: each file numbers its clusters from 1
            feats += [(Path(name).stem if len(files) > 1 else None, f)
                      for f in json.loads(path.read_text(encoding="utf-8"))["features"]]
    out = []
    for prefix, f in feats:
        p = dict(f["properties"])
        if prefix and "cluster_id" in p:
            p["cluster_id"] = f"{prefix}:{p['cluster_id']}"
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
    # live_analysis is true only when the live pipeline could actually run: the GFW
    # token plus an Earth Engine credential -- a service account (for a server) OR
    # cached interactive credentials (local dev). The frontend uses this to show or
    # hide the Run Analysis button so it never offers a run that would just error.
    ee_service_account = bool(os.environ.get("GEE_SERVICE_ACCOUNT") and os.environ.get("GEE_SA_KEY_JSON"))
    ee_cached = (Path.home() / ".config" / "earthengine" / "credentials").exists()
    live_analysis = bool(os.environ.get("GFW_API_TOKEN")) and (ee_service_account or ee_cached)
    return {"status": "ok", "detections": len(DETECTIONS), "live_analysis": live_analysis}


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
    search = [DATA, DATA / "tuticorin" / "crops", DATA / "validation" / "crops", DATA / "recent" / "crops"]
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
        import crops
        import detect_v3
        import match as match_mod
        import validate_vs_gfw as vgfw

        run["log"].append("Authenticating with Earth Engine and GFW...")
        token = fetch_ais.get_token()
        fetch_sar.authenticate_and_init(vgfw.PROJECT_ID)

        run_dir = DATA / "runs" / run_id
        crop_dir = run_dir / "crops"
        run_dir.mkdir(parents=True, exist_ok=True)
        crop_dir.mkdir(exist_ok=True)

        # Discover the ACTUAL Sentinel-1 acquisition dates over the region in the
        # range (§10.1). We do NOT guess a cadence: if discovery fails we stop, and if
        # it returns no scenes we report that honestly rather than fabricating dates.
        d1 = dt.date.fromisoformat(req.end_date)
        dates: list[str] = []
        discovery_ok = False
        try:
            import ee
            lon0, lat0, lon1, lat1 = req.region
            coll = (ee.ImageCollection("COPERNICUS/S1_GRD")
                    .filterBounds(ee.Geometry.Rectangle([lon0, lat0, lon1, lat1]))
                    .filterDate(req.start_date, (d1 + dt.timedelta(days=1)).isoformat())
                    .filter(ee.Filter.eq("instrumentMode", "IW"))
                    .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV")))
            millis = coll.aggregate_array("system:time_start").getInfo() or []
            dates = sorted({dt.datetime.utcfromtimestamp(m / 1000).date().isoformat() for m in millis})
            discovery_ok = True
            run["log"].append(f"Discovered {len(dates)} actual Sentinel-1 acquisition date(s) in range")
        except Exception as e:
            run["log"].append(f"Acquisition-date discovery FAILED: {e}")

        if not discovery_ok:
            # No real dates -> do not guess. The analysis was not performed.
            run["status"] = "error"
            run["error"] = ("Sentinel-1 acquisition-date discovery failed, so no analysis was performed. "
                            "No acquisition dates were guessed.")
            run["log"].append(run["error"])
            return
        if not dates:
            # Discovery succeeded, but the archive holds no S1 pass here -> report, don't invent.
            run["progress"] = 100
            run["status"] = "done"
            run["result"] = {
                "total": 0, "dark_candidates": 0, "classes": {},
                "coverage": {"requested": 0, "dates": 0, "no_scene": 0, "download_failed": 0,
                             "analyzed": 0, "no_sea": 0, "degraded": 0, "gfw_failed_dates": 0, "detections_kept": 0},
                "data_unavailable": True,
                "note": "No Sentinel-1 acquisitions found in this date range over this region.",
            }
            run["log"].append("No Sentinel-1 acquisitions in range over this region -- nothing to analyse (not an empty sea).")
            return
        run["log"].append(f"Date range: {req.start_date} to {req.end_date} ({len(dates)} acquisition passes)")

        region = tuple(req.region)
        tuticorin_box = (78.20, 8.70, 78.34, 8.87)
        tile_half = vgfw.TILE_HALF_DEG
        match_radius = vgfw.MATCH_RADIUS_M

        # Fetch GFW SAR detections. A request FAILURE (no answer) is tracked
        # separately from a successful EMPTY response (genuinely no SAR detections):
        # the first means we do not know, the second means there were none.
        all_gfw: dict[str, list[dict]] = {}
        gfw_ok: dict[str, bool] = {}
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
                gfw_ok[date] = True
                no_ais = sum(1 for g in records if not g.get("mmsi"))
                run["log"].append(
                    f"  {len(records)} SAR detections ({no_ais} without AIS)"
                    + (" -- successful empty response (no SAR detections here)" if not records else "")
                )
            except Exception as e:
                run["log"].append(f"  GFW REQUEST FAILED for {date}: {e} -- this date is unperformed, not empty")
                all_gfw[date] = []
                gfw_ok[date] = False

        # Download tiles and run detector
        picked_dets: dict[str, list[dict]] = {}
        total_tiles = sum(min(len(gfw), req.max_per_date) for gfw in all_gfw.values())
        processed = 0
        gfw_failed_dates = [d for d in dates if not gfw_ok.get(d)]
        # coverage accounting (§ "distinguish unavailable data from empty sea"):
        # a failed retrieval must not be reported as an analysed-but-empty result.
        cov = {"requested": total_tiles, "dates": len(dates), "no_scene": 0, "download_failed": 0,
               "analyzed": 0, "no_sea": 0, "degraded": 0, "gfw_failed_dates": len(gfw_failed_dates)}

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
                        cov["no_scene"] += 1
                        run["log"].append(f"  unavailable: no Sentinel-1 scene covering this point on {date}")
                        continue
                    aoi = fetch_sar.build_aoi(
                        lon - tile_half, lat - tile_half,
                        lon + tile_half, lat + tile_half,
                    )
                    if not vgfw.download_with_retry(aoi, scene, tif):
                        cov["download_failed"] += 1
                        run["log"].append(f"  unavailable: tile download failed after retries on {date}")
                        continue

                # Corrected v3.1-audit detector (same code the technical write-up describes),
                # not the Round 1 CA-CFAR path.
                dets, ddiag = detect_v3.run_detector_v3(tif)
                cov["analyzed"] += 1
                status = ddiag.get("status", "ok")
                if status == "no_sea":
                    cov["no_sea"] += 1
                elif status in ("degraded", "insufficient_data"):
                    cov["degraded"] += 1
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
                    "gfw_association": "associated",
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
        chains = conf.find_chain_members(clusters_list)
        features, per_date = [], Counter()
        for cid, cluster in enumerate(clusters_list, start=1):
            for cdate, det in sorted(cluster, key=lambda m: m[0]):
                res = conf.score_detection(det, cluster, len(dates),
                                           corroborated_by="GFW's SAR-presence algorithm", ais_source="gfw_reported",
                                           chain_member=id(cluster) in chains)
                per_date[cdate] += 1
                det_id = f"{cdate}-A{per_date[cdate]:02d}"
                tif_path = Path(det.pop("tif_path", ""))
                if tif_path.exists():
                    # Canonical fixed [-23, +3] dB crop window (crops.py), same as the
                    # saved Tuticorin/Gulf/recent crops -- not a per-crop percentile stretch.
                    try:
                        crops.save_crop(tif_path, det["row"], det["col"], crop_dir / f"{det_id}.png")
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
        unavailable = cov["no_scene"] + cov["download_failed"]
        cov["detections_kept"] = sum(len(v) for v in picked_dets.values())
        # Partial: some acquisition dates could not be fetched from GFW at all, so those
        # dates are unperformed (unknown), not confirmed empty.
        partial = len(gfw_failed_dates) > 0
        # Unavailable: nothing usable was obtained -- every GFW date failed, or no tile
        # could be retrieved. Either way, NOT an empty-sea conclusion.
        data_unavailable = (len(gfw_failed_dates) == len(dates)) or (cov["analyzed"] == 0 and unavailable > 0)
        run["result"] = {
            "total": len(features),
            "dark_candidates": dark,
            "classes": dict(classes),
            "coverage": cov,
            "gfw_failed_dates": gfw_failed_dates,
            "partial": partial,
            "data_unavailable": data_unavailable,
        }
        if gfw_failed_dates:
            run["log"].append(
                f"GFW request failed for {len(gfw_failed_dates)}/{len(dates)} date(s): "
                f"{', '.join(gfw_failed_dates)} -- these dates are unperformed, not empty."
            )
        run["log"].append(
            f"Coverage: {cov['analyzed']} tiles analysed, {unavailable} unavailable "
            f"({cov['no_scene']} no scene, {cov['download_failed']} download failed); "
            f"{cov['no_sea']} had no open sea after masking, {cov['degraded']} degraded."
        )
        if data_unavailable:
            run["log"].append("No usable data could be retrieved -- this is missing data, not an empty sea.")
        elif partial:
            run["log"].append(f"PARTIAL result: {dark} dark candidates / {len(features)} detections over the dates that were successfully fetched.")
        else:
            run["log"].append(f"Done: {dark} dark-vessel candidates out of {len(features)} detections over {cov['analyzed']} analysed tiles")

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
