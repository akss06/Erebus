"""
Milestone 2 step: match Milestone 1 SAR detections against GFW AIS presence
records for the same date/sub-box.

Does not modify any Milestone 1 code, and reuses fetch_ais.fetch_presence_report()
exactly as built for exploration - the date-range-then-filter-to-one-day
pattern is the same one that already worked there (a single-day query
returns null; a month-wide query returns per-vessel presence records).

Run directly from the project root: `python src/match.py`
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import fetch_ais

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "milestone1_confirmed.json"
DETECTIONS_GEOJSON_PATH = PROJECT_ROOT / "data" / "detections.geojson"
MATCHED_OUTPUT_PATH = PROJECT_ROOT / "data" / "matched_detections.geojson"

PASS_DATE = "2026-01-18"
# 4wings/report needs a wide date-range to return anything (see fetch_ais.py
# docstring) - fetch the whole month, then filter down to the pass date.
MONTH_START, MONTH_END = "2026-01-01", "2026-01-31"

MATCH_RADIUS_M = 1500  # primary/default radius - what matched_detections.geojson is saved with
RADII_TO_TEST_M = [1500, 500, 250]  # sensitivity sweep - GFW's presence grid is ~1km cells

# Ground truth from Milestone 1's visual crop review (results/MILESTONE1_FINDINGS.md),
# keyed by rank when detections are sorted by contrast_db descending (the same
# numbering used throughout that review - #1 highest contrast .. #17 lowest).
VISUAL_CLASS_BY_RANK = {
    1: "UNEXPLAINED",       # highest contrast (22.5dB) but no visible feature in crop
    2: "UNEXPLAINED",       # sits on an unrelated bright streak, not compact
    3: "CONFIRMED_VESSEL",  # discrete compact bright blob, no structure/line nearby
    4: "CONFIRMED_VESSEL",  # same, ~80m from #3
    5: "LINEAR_FEATURE",
    6: "LINEAR_FEATURE",
    7: "LINEAR_FEATURE",
    8: "LINEAR_FEATURE",
    9: "LINEAR_FEATURE",
    10: "LINEAR_FEATURE",
    11: "LINEAR_FEATURE",
    12: "LINEAR_FEATURE",   # #5-12: fragments of one continuous bright line (pipeline/
                             # cable/unmapped shoal), confirmed via cluster_6_12_crop.png
    13: "SPECKLE",
    14: "SPECKLE",
    15: "SPECKLE",
    16: "SPECKLE",
    17: "SPECKLE",          # #13-17: no visible feature, indistinguishable from clutter
}


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters. Accurate enough at this ~10km scale."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_detections(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    detections = []
    for feature in data["features"]:
        props = dict(feature["properties"])
        props["lon"], props["lat"] = feature["geometry"]["coordinates"]
        detections.append(props)
    return detections


def load_ais_records_for_date(
    token: str, lon_min: float, lat_min: float, lon_max: float, lat_max: float, pass_date: str
) -> list[dict]:
    data = fetch_ais.fetch_presence_report(token, lon_min, lat_min, lon_max, lat_max, MONTH_START, MONTH_END)
    entries = data.get("entries", [{}])[0].get("public-global-presence:v4.0") or []
    return [e for e in entries if e.get("date") == pass_date]


def match_detections(detections: list[dict], ais_records: list[dict], radius_m: float) -> list[dict]:
    """For each detection, find the nearest AIS record and label MATCHED if
    within radius_m, else UNMATCHED (dark)."""
    matched = []
    for det in detections:
        nearest, nearest_dist = None, None
        for ais in ais_records:
            dist = haversine_m(det["lon"], det["lat"], ais["lon"], ais["lat"])
            if nearest_dist is None or dist < nearest_dist:
                nearest, nearest_dist = ais, dist

        result = dict(det)
        is_match = nearest is not None and nearest_dist <= radius_m
        result["match_status"] = "MATCHED" if is_match else "UNMATCHED"
        result["match_distance_m"] = round(nearest_dist, 1) if nearest_dist is not None else None
        result["matched_mmsi"] = nearest["mmsi"] if is_match else None
        result["matched_name"] = nearest["shipName"] if is_match else None
        result["matched_flag"] = nearest["flag"] if is_match else None
        result["matched_type"] = nearest["vesselType"] if is_match else None
        result["matched_hours"] = nearest["hours"] if is_match else None
        matched.append(result)
    return matched


def reverse_check(ais_records: list[dict], detections: list[dict], radius_m: float) -> list[dict]:
    """AIS presence records with no SAR detection within radius_m - vessels
    AIS saw that the detector missed. Useful for recall, not precision."""
    missed = []
    for ais in ais_records:
        nearest_dist = None
        for det in detections:
            dist = haversine_m(ais["lon"], ais["lat"], det["lon"], det["lat"])
            if nearest_dist is None or dist < nearest_dist:
                nearest_dist = dist
        if nearest_dist is None or nearest_dist > radius_m:
            entry = dict(ais)
            entry["nearest_detection_dist_m"] = round(nearest_dist, 1) if nearest_dist is not None else None
            missed.append(entry)
    return missed


def rank_and_classify(matched: list[dict]) -> list[dict]:
    """Sort by contrast_db descending (the numbering used throughout the
    Milestone 1 visual review) and attach rank + visual_class."""
    ranked = sorted(matched, key=lambda d: d["contrast_db"], reverse=True)
    for i, d in enumerate(ranked, start=1):
        d["rank"] = i
        d["visual_class"] = VISUAL_CLASS_BY_RANK.get(i, "UNKNOWN")
    return ranked


def print_summary_table(ranked: list[dict], radius_m: float) -> tuple[int, int]:
    print(f"[match] Match radius: {radius_m}m")
    print(f"{'#':<4}{'lon':<11}{'lat':<10}{'contrast_db':<12}{'visual_class':<18}"
          f"{'status':<11}{'matched vessel':<32}{'dist_m':<8}")
    n_matched = 0
    for d in ranked:
        if d["match_status"] == "MATCHED":
            n_matched += 1
            vessel_str = f"{d['matched_name']} (MMSI {d['matched_mmsi']})"
        else:
            vessel_str = "-"
        dist_str = f"{d['match_distance_m']:.0f}" if d["match_distance_m"] is not None else "-"
        print(f"{d['rank']:<4}{d['lon']:<11.5f}{d['lat']:<10.5f}{d['contrast_db']:<12.1f}"
              f"{d['visual_class']:<18}{d['match_status']:<11}{vessel_str:<32}{dist_str:<8}")
    n_unmatched = len(ranked) - n_matched
    print(f"[match] {n_matched} MATCHED, {n_unmatched} UNMATCHED out of {len(ranked)} detections.\n")
    return n_matched, n_unmatched


def save_matched_geojson(matched: list[dict], out_path: Path) -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")},
        }
        for d in matched
    ]
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))


def main() -> None:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    box = config["detection_sub_box"]
    lon_min, lat_min, lon_max, lat_max = box["lon_min"], box["lat_min"], box["lon_max"], box["lat_max"]

    token = fetch_ais.get_token()

    print(f"[match] Loading detections from {DETECTIONS_GEOJSON_PATH}...")
    detections = load_detections(DETECTIONS_GEOJSON_PATH)
    print(f"[match] {len(detections)} detections loaded.")

    print(f"[match] Fetching AIS presence for the sub-box, filtering to {PASS_DATE}...")
    ais_records = load_ais_records_for_date(token, lon_min, lat_min, lon_max, lat_max, PASS_DATE)
    print(f"[match] {len(ais_records)} AIS presence record(s) on {PASS_DATE}.\n")

    # ---- STEP 1: radius sensitivity sweep ----
    sensitivity = []
    ranked_by_radius = {}
    for radius in RADII_TO_TEST_M:
        print("=" * 78)
        matched = match_detections(detections, ais_records, radius)
        ranked = rank_and_classify(matched)
        ranked_by_radius[radius] = ranked
        n_matched, n_unmatched = print_summary_table(ranked, radius)
        sensitivity.append((radius, n_matched, n_unmatched))

    print("=" * 78)
    print("[sensitivity] Match rate collapses as radius tightens:")
    print(f"{'radius_m':<10}{'matched':<10}{'unmatched':<10}")
    for radius, n_matched, n_unmatched in sensitivity:
        print(f"{radius:<10}{n_matched:<10}{n_unmatched:<10}")
    print()

    # ---- Save matched_detections.geojson at the primary radius, with visual_class ----
    primary_ranked = ranked_by_radius[MATCH_RADIUS_M]
    save_matched_geojson(primary_ranked, MATCHED_OUTPUT_PATH)
    print(f"[match] Saved {MATCHED_OUTPUT_PATH} (radius={MATCH_RADIUS_M}m, includes visual_class)\n")

    # ---- STEP 2: CONFIRMED_VESSEL only, at each radius ----
    print("=" * 78)
    print("[confirmed] CONFIRMED_VESSEL detections only, at each radius:")
    for radius in RADII_TO_TEST_M:
        ranked = ranked_by_radius[radius]
        confirmed = [d for d in ranked if d["visual_class"] == "CONFIRMED_VESSEL"]
        print(f"\n  radius={radius}m:")
        for d in confirmed:
            vessel_str = (f"{d['matched_name']} (MMSI {d['matched_mmsi']}, {d['match_distance_m']:.0f}m)"
                          if d["match_status"] == "MATCHED" else "UNMATCHED")
            print(f"    #{d['rank']}  lon={d['lon']:.5f}  lat={d['lat']:.5f}  "
                  f"contrast_db={d['contrast_db']:.1f}  -> {vessel_str}")

    # ---- Reverse check at the primary radius ----
    print()
    print("=" * 78)
    print(f"[reverse] AIS presence records on {PASS_DATE} with NO SAR detection within "
          f"{MATCH_RADIUS_M}m (vessels AIS saw but SAR missed):")
    missed = reverse_check(ais_records, detections, MATCH_RADIUS_M)
    print(f"[reverse] {len(missed)} of {len(ais_records)} AIS presence records had no nearby detection.")
    for m in missed:
        dist_str = f"{m['nearest_detection_dist_m']:.0f}m" if m["nearest_detection_dist_m"] is not None else "n/a"
        print(f"    mmsi={m['mmsi']}  name={m['shipName']!r:25}  flag={m['flag']}  type={m['vesselType']:10}  "
              f"lat={m['lat']}  lon={m['lon']}  hours={m['hours']}  nearest_detection={dist_str}")


if __name__ == "__main__":
    main()
