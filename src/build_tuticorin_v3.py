"""
Re-detect the four Tuticorin anchorage passes with the v3 detector and rebuild
data/scored_detections.geojson (the app's Tuticorin area).

Same clips, same own-AIS match (GFW presence, 1500 m) and same cross-pass scoring
(confidence.score_all, ais_source="our_ais") as before -- only the detector changes
from the Round-1 CA-CFAR (detect.py) to detect_v3. The Round-1 per-pass files
(matched_detections.geojson, multi_date/*_detections.geojson, with their hand labels)
are left untouched as the historical baseline; v3 per-pass output goes to
data/tuticorin/.

Needs Earth Engine (JRC water mask, cached after the first run) and GFW_API_TOKEN
(AIS presence for the three non-Jan-18 passes).

Run from the project root:  python src/build_tuticorin_v3.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confidence
import crops
import detect_v3
import fetch_ais
import fetch_sar
import match
import run_multi_date as rmd

DATA = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = DATA / "tuticorin"
CROP_DIR = OUT_DIR / "crops"

PASSES = {"2026-01-18": DATA / "sar_vv_clip.tif"}
PASSES.update({d["date"]: DATA / "multi_date" / f"{d['date']}_sar_vv_clip.tif" for d in rmd.DATES})


def ais_for(date: str, token: str) -> list[dict]:
    if date == "2026-01-18":   # Round-1 cached snapshot, as before
        return json.loads(rmd.JAN18_AIS_PATH.read_text(encoding="utf-8"))
    start, end = rmd._month_bounds(date)
    data = fetch_ais.fetch_presence_report(token, rmd.AOI_LON_MIN, rmd.AOI_LAT_MIN,
                                           rmd.AOI_LON_MAX, rmd.AOI_LAT_MAX, start, end)
    entries = data.get("entries", [{}])[0].get("public-global-presence:v4.0") or []
    return [e for e in entries if e.get("date") == date]


def main() -> None:
    token = fetch_ais.get_token()
    fetch_sar.authenticate_and_init(rmd.PROJECT_ID)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CROP_DIR.mkdir(parents=True, exist_ok=True)

    passes: dict[str, list[dict]] = {}
    tifs: dict[str, Path] = {}
    for date, tif in sorted(PASSES.items()):
        dets, diag = detect_v3.run_detector_v3(tif)
        ais = ais_for(date, token)
        matched = match.match_detections(dets, ais, rmd.MATCH_RADIUS_M)
        for d in matched:
            d["detector_version"] = diag.get("detector_version")
            d["detector_config"] = diag.get("config_hash")
        passes[date], tifs[date] = matched, tif
        rmd.save_matched_geojson(matched, OUT_DIR / f"{date}_detections.geojson")
        n_m = sum(d["match_status"] == "MATCHED" for d in matched)
        print(f"[{date}] status={diag.get('status')} {len(matched)} detections, {n_m} AIS-matched, "
              f"{len(ais)} AIS records", flush=True)

    scored = confidence.score_all(passes)
    scored.sort(key=lambda d: d["id"])
    for d in scored:
        d["crop"] = f"{d['id']}.png"
        crops.save_crop(tifs[d["date"]], d["row"], d["col"], CROP_DIR / d["crop"])

    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
                 "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")}} for d in scored]
    provenance = {
        "generated_by": "src/build_tuticorin_v3.py",
        "kind": "Tuticorin cross-pass scoring (our own AIS match), v3 detector",
        "ais_source": "our_ais",
        "scoring_semantics": "PERSISTENT_UNIDENTIFIED, GFW!=AIS, heuristic score",
        "status": "ok",
        "n_features": len(features),
    }
    (DATA / "scored_detections.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "provenance": provenance, "features": features}, indent=1), encoding="utf-8")

    print(f"\n{len(scored)} detections over {len(passes)} passes -> scored_detections.geojson")
    print("Classes:", dict(Counter(d["confidence_class"] for d in scored)))
    for name in ("DMC JUPITER", "NEREUS PROGRESS"):
        hits = [f"{d['id']}:{d['confidence_class']}" for d in scored if d.get("matched_name") == name]
        print(f"  {name}: {hits or 'not matched on any pass'}")


if __name__ == "__main__":
    main()
