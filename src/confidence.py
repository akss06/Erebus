"""
Round 2: automatic confidence score for CFAR detections, replacing the hand-labelled
`visual_class` from Round 1.

Inputs are the per-pass detections already produced by the pipeline (same GeoJSON
properties: contrast_db, area_px, match_status, matched_mmsi, ...). Detections from
different passes over the same area are clustered by position; what each cluster
does across passes is the strongest evidence we have:

  - Same spot on 3+ passes AND the same AIS vessel each time -> a ship at anchor.
  - Same spot on 3+ passes but the "matched" AIS vessel keeps changing (or there is
    none) -> a fixed object (pipeline, cable, shoal). The AIS "match" there is just
    whichever ship happened to anchor nearby that day.
  - Seen on one pass only -> judged on contrast, size and AIS match alone.

Known limit: a *dark* ship anchored in one place for weeks would look like a fixed
object (persistent, no AIS identity). Persistence cannot separate those two cases;
only shape/context can. Such objects are labelled FIXED_OBJECT, never "vessel".

Run from the project root:  python src/confidence.py
"""
from __future__ import annotations

import glob
import json
import math
from collections import Counter
from pathlib import Path

CLUSTER_RADIUS_M = 100       # NEREUS PROGRESS drifted <60 m; 75 m split real fixed-object fragments, 125 m started over-merging
MIN_PASSES_FOR_FIXED = 3     # persistence needed to call something fixed / anchored
SAME_VESSEL_SHARE = 0.75     # share of passes with the top MMSI to call it "the same vessel"
STRONG_CONTRAST_DB = 15.0    # contrast level of every confirmed ship in Round 1
WEAK_CONTRAST_DB = 12.0      # below this, returns are speckle-like (recalibrated Oct 2026 after reviewing v3 crops; was 10.0)
MIN_AREA_PX = 8

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CLASS_ORDER = ["ANCHORED_VESSEL", "VESSEL_CANDIDATE", "DARK_CANDIDATE", "LOW_CONFIDENCE", "FIXED_OBJECT", "CLUTTER"]


def _dist_m(a: dict, b: dict) -> float:
    lat0 = math.radians((a["lat"] + b["lat"]) / 2)
    return math.hypot((a["lon"] - b["lon"]) * 111320 * math.cos(lat0), (a["lat"] - b["lat"]) * 110574)


def cluster_across_passes(passes: dict[str, list[dict]]) -> list[list[tuple[str, dict]]]:
    """Merge detections from different passes that sit within CLUSTER_RADIUS_M, nearest pairs first,
    never putting two detections from the same pass in one cluster."""
    items = [(d, x) for d, dets in passes.items() for x in dets]
    cluster_of = {id(x): [(d, x)] for d, x in items}
    pairs = []
    for i, (da, a) in enumerate(items):
        for db, b in items[i + 1:]:
            if da == db:
                continue
            dist = _dist_m(a, b)
            if dist <= CLUSTER_RADIUS_M:
                pairs.append((dist, da, a, db, b))
    for dist, da, a, db, b in sorted(pairs, key=lambda t: t[0]):
        ca, cb = cluster_of[id(a)], cluster_of[id(b)]
        if ca is cb or {d for d, _ in ca} & {d for d, _ in cb}:
            continue
        ca.extend(cb)
        for _, x in cb:
            cluster_of[id(x)] = ca
    seen, out = set(), []
    for c in cluster_of.values():
        if id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def score_detection(det: dict, cluster: list[tuple[str, dict]], n_passes_total: int, corroborated_by: str | None = None) -> dict:
    """Return {'confidence_class', 'confidence', 'reasons'} for one detection given its cluster.

    corroborated_by: name of an independent detector that reported the same object (e.g. GFW's SAR
    detections). Two independent detectors agreeing is stronger evidence than one, so the contrast bar
    for "vessel / dark candidate" drops from STRONG_CONTRAST_DB to WEAK_CONTRAST_DB. This lower bar was
    chosen after viewing the first Gulf of Mannar crops (compact blob + along-track streak at 10-13 dB),
    so treat it as a design decision to re-test, not a validated threshold."""
    contrast, area = det["contrast_db"], det["area_px"]
    matched = det.get("match_status") == "MATCHED"
    n_seen = len(cluster)
    mmsis = [x.get("matched_mmsi") for _, x in cluster if x.get("matched_mmsi")]
    top_mmsi, top_n = (Counter(mmsis).most_common(1)[0] if mmsis else (None, 0))
    reasons = [f"contrast {contrast:.1f} dB, {area} px"]

    # contrast term scales 0..1 between WEAK and ~20 dB; size term caps at 20 px
    strength = min(1.0, max(0.0, (contrast - 6.0) / 14.0)) * 0.8 + min(1.0, area / 20) * 0.2

    # VH/VV cross-pol corroboration: metal vessels depolarise and spike VH, sea
    # clutter does not. A positive signal only raises confidence; its absence
    # does NOT demote (small wooden/fibreglass trawlers have weak VH too).
    if det.get("vh_corroborated"):
        reasons.append("cross-pol (VH/VV) elevated vs sea: consistent with a hard/metal target")
        strength = min(1.0, strength + 0.1)

    if n_seen >= MIN_PASSES_FOR_FIXED:
        reasons.append(f"same spot on {n_seen}/{n_passes_total} passes")
        if top_n / n_seen >= SAME_VESSEL_SHARE and contrast >= STRONG_CONTRAST_DB - 3:
            reasons.append(f"same AIS vessel (MMSI {top_mmsi}) on {top_n} passes -> ship at anchor")
            return {"confidence_class": "ANCHORED_VESSEL", "confidence": round(60 + 40 * strength), "reasons": reasons}
        if len(set(mmsis)) > 1:
            reasons.append(f"'matched' AIS vessel differs between passes ({len(set(mmsis))} different ships) -> fixed object")
        else:
            reasons.append("no consistent AIS vessel across passes -> fixed object")
        return {"confidence_class": "FIXED_OBJECT", "confidence": round(10 + 20 * (1 - strength)), "reasons": reasons}

    if n_seen == 2:
        reasons.append("seen on 2 passes only; persistence inconclusive")

    if contrast < WEAK_CONTRAST_DB:
        reasons.append(f"contrast below {WEAK_CONTRAST_DB:.0f} dB: indistinguishable from sea clutter")
        return {"confidence_class": "CLUTTER", "confidence": round(30 * strength), "reasons": reasons}

    min_contrast = WEAK_CONTRAST_DB if corroborated_by else STRONG_CONTRAST_DB
    if corroborated_by:
        reasons.append(f"independently reported by {corroborated_by}")
    if contrast >= min_contrast and area >= MIN_AREA_PX:
        if matched:
            reasons.append(f"AIS vessel nearby ({det.get('matched_name') or 'unknown'}, {det.get('match_distance_m', 0):.0f} m)")
            return {"confidence_class": "VESSEL_CANDIDATE", "confidence": round(50 + 40 * strength), "reasons": reasons}
        reasons.append("no AIS vessel within match radius -> candidate dark vessel (needs review)")
        return {"confidence_class": "DARK_CANDIDATE", "confidence": round(40 + 40 * strength), "reasons": reasons}

    reasons.append("bright but small or weak: not enough evidence either way")
    return {"confidence_class": "LOW_CONFIDENCE", "confidence": round(20 + 30 * strength), "reasons": reasons}


def load_passes() -> dict[str, list[dict]]:
    def load(path: Path) -> list[dict]:
        feats = json.loads(path.read_text(encoding="utf-8"))["features"]
        return [dict(f["properties"], lon=f["geometry"]["coordinates"][0], lat=f["geometry"]["coordinates"][1]) for f in feats]

    passes = {"2026-01-18": load(DATA / "matched_detections.geojson")}
    for p in sorted(glob.glob(str(DATA / "multi_date" / "*_detections.geojson"))):
        passes[Path(p).name[:10]] = load(Path(p))
    return passes


def score_all(passes: dict[str, list[dict]]) -> list[dict]:
    clusters = cluster_across_passes(passes)
    # stable ids: clusters ordered by position, detections numbered within their pass
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    out, per_date = [], Counter()
    for cluster_id, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            res = score_detection(det, cluster, len(passes))
            per_date[date] += 1
            out.append({**det, "id": f"{date}-{per_date[date]:02d}", "date": date,
                        "cluster_id": cluster_id, "cluster_size": len(cluster), **res})
    return out


def main() -> None:
    passes = load_passes()
    scored = score_all(passes)
    scored.sort(key=lambda d: d["id"])
    features = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
         "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")}}
        for d in scored
    ]
    (DATA / "scored_detections.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=1), encoding="utf-8")

    print(f"{len(scored)} detections over {len(passes)} passes")
    print("Class counts:", dict(Counter(d["confidence_class"] for d in scored)))
    print("\n2026-01-18 vs Round 1 hand labels:")
    print(f"{'#':>3} {'hand label':17} {'auto class':17} {'conf':>4}  reasons")
    for d in scored:
        if d["date"] == "2026-01-18":
            print(f"{d['rank']:>3} {d['visual_class']:17} {d['confidence_class']:17} {d['confidence']:>4}  {'; '.join(d['reasons'][1:])}")


if __name__ == "__main__":
    main()
