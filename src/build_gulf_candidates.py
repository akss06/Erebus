"""
Round 2: turn the GFW validation tiles into scored detections for the app
(Gulf of Mannar / Palk Strait area).

For every GFW SAR detection that our v3 detector also finds within the match
radius on the cached validation tile, keep OUR nearest detection, mark it matched/unmatched using
GFW's AIS flag for that detection, score it with confidence.py and save a radar
crop. A detection that both detectors see and that GFW could not match to an AIS
vessel is the only thing that can become a DARK_CANDIDATE.

GFW detections our detector did NOT find are reported in the summary but not
added to the app: there is no radar blob of ours to show or score.

Run from the project root (after validate_vs_gfw.py has cached its tiles):
    python src/build_gulf_candidates.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confidence  # noqa: E402
import crops as crops_mod  # noqa: E402
import detect_v3  # noqa: E402
import match  # noqa: E402
import validate_vs_gfw as vgfw  # noqa: E402

VAL = vgfw.VAL_DIR
CROP_DIR = VAL / "crops"
OUT = vgfw.DATA_DIR / "scored_gulf.geojson"
# Crop rendering (fixed [-23,+3] dB window) lives in src/crops.py, shared by every dataset.
DEDUPE_M = 60       # two GFW cells can point at the same ship
TUTICORIN_BOX = (78.20, 8.70, 78.34, 8.87)  # already its own area in the app; avoid double counting


def gfw_index(date: str, rec: dict) -> int | None:
    """Tile files are named by the record's position in that date's GFW list; recover it from lon/lat."""
    for i, g in enumerate(json.loads((VAL / f"gfw_sar_{date}.json").read_text())):
        if abs(g["lon"] - rec["lon"]) < 1e-6 and abs(g["lat"] - rec["lat"]) < 1e-6:
            return i
    return None


def save_crop(tif: Path, row: float, col: float, out: Path) -> None:
    # Canonical fixed [-23, +3] dB window (see crops.py), shared across all datasets,
    # instead of a per-crop percentile stretch that rescales each crop independently.
    crops_mod.save_crop(tif, row, col, out)


def main() -> None:
    vgfw.fetch_sar.authenticate_and_init(vgfw.PROJECT_ID)   # JRC water mask for detect_v3 (cached per tile)
    # validation_rows.json is the frozen CA-CFAR validation (§6); here we only reuse its
    # GFW record list and re-detect each tile with v3 -- "found" is decided by v3 below.
    rows = json.loads((VAL / "validation_rows.json").read_text())
    picked: dict[str, list[dict]] = {}
    not_found = 0
    for r in rows:
        if TUTICORIN_BOX[0] <= r["lon"] <= TUTICORIN_BOX[2] and TUTICORIN_BOX[1] <= r["lat"] <= TUTICORIN_BOX[3]:
            continue
        i = gfw_index(r["date"], r)
        if i is None:
            continue
        tif = VAL / f"{r['date']}_{i:03d}.tif"
        gfw = json.loads((VAL / f"gfw_sar_{r['date']}.json").read_text())[i]
        dets, _ = detect_v3.run_detector_v3(tif)
        near = min(dets, key=lambda d: match.haversine_m(r["lon"], r["lat"], d["lon"], d["lat"]), default=None)
        dist = match.haversine_m(r["lon"], r["lat"], near["lon"], near["lat"]) if near else float("inf")
        if dist > vgfw.MATCH_RADIUS_M:
            not_found += 1
            continue
        has_ais = bool(gfw["mmsi"])
        near.update({
            "match_status": "MATCHED" if has_ais else "UNMATCHED",
            "match_distance_m": round(dist, 1),
            "matched_name": gfw.get("shipName") or None,
            "matched_mmsi": gfw["mmsi"] or None,
            "matched_flag": gfw.get("flag") or None,
            "matched_type": gfw.get("vesselType") or None,
            # every kept detection sits at a GFW SAR-presence record (this script
            # only processes found_by_us GFW detections); AIS here is GFW-reported.
            "gfw_association": "associated",
            "tile": tif, "gfw_lon": gfw["lon"], "gfw_lat": gfw["lat"],
        })
        day = picked.setdefault(r["date"], [])
        if any(match.haversine_m(near["lon"], near["lat"], p["lon"], p["lat"]) < DEDUPE_M for p in day):
            continue
        day.append(near)

    clusters = confidence.cluster_across_passes(picked)
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    features, per_date = [], Counter()
    for cid, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            res = confidence.score_detection(det, cluster, len(vgfw.PASS_DATES),
                                             corroborated_by="GFW's SAR-presence algorithm", ais_source="gfw_reported")
            per_date[date] += 1
            det_id = f"{date}-G{per_date[date]:02d}"
            save_crop(det.pop("tile"), det["row"], det["col"], CROP_DIR / f"{det_id}.png")
            props = {k: v for k, v in det.items() if k not in ("lon", "lat")}
            props.update(id=det_id, date=date, cluster_id=cid, cluster_size=len(cluster), crop=f"{det_id}.png", **res)
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [det["lon"], det["lat"]]}, "properties": props})

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=1), encoding="utf-8")
    classes = Counter(f["properties"]["confidence_class"] for f in features)
    print(f"{len(features)} scored detections written to {OUT.name} ({not_found} GFW detections our detector missed were left out)")
    print("Classes:", dict(classes))
    print("\nNo-AIS detections seen by both detectors:")
    for f in features:
        p = f["properties"]
        if p["match_status"] == "UNMATCHED":
            print(f"  {p['id']} {p['confidence_class']:16} conf={p['confidence']:3} {p['contrast_db']:.1f} dB {p['area_px']:3} px "
                  f"passes={p['cluster_size']}  @ {f['geometry']['coordinates'][0]:.3f},{f['geometry']['coordinates'][1]:.3f}")


if __name__ == "__main__":
    main()
