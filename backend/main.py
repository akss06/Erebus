"""
Erébus API (Round 2 prototype backend).

Serves scored detections (data/scored_*.geojson), a cross-pass cluster view,
a ranked alert list, evidence crops and analyst reviews. Reads files produced
by src/confidence.py (and src/build_gulf_candidates.py when present); it never
calls Earth Engine or GFW at request time, so it runs offline.

If frontend/dist exists it is served at "/", so the whole app is one process:
    uvicorn backend.main:app --port 8000
"""
from __future__ import annotations

import json
import sys
import threading
from functools import lru_cache
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
        "name": "Gulf of Mannar (Sep-Oct 2026)",
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
    "recent": "Real detection (Sentinel-1 Sep/Oct 2026); AIS status from GFW, unverified",
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
    for base in (DATA, DATA / "validation" / "crops", DATA / "recent" / "crops"):
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


if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
