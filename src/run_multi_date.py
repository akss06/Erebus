"""
Multi-date extension: run the full validated Milestone 1+2 pipeline (SAR
fetch -> land mask -> CA-CFAR detect -> shape filter -> AIS match) on 3
additional dates at the same confirmed-good Tuticorin anchorage sub-box
that already produced detections #3/#4 -> DMC JUPITER on 2026-01-18.

Does not modify fetch_sar.py, land_mask.py, detect.py, match.py, or
app.py - this script only calls their existing public functions and adds
its own orchestration glue (the coverage/resolution gates and AIS-fetch
wiring live in run_milestone1.py's and match.py's main()/module bodies,
which aren't importable as reusable functions, so that glue is
re-implemented here rather than duplicated by editing those files).

Note on AIS fetching: match.py's load_ais_records_for_date() hardcodes a
January date range (its module-level MONTH_START/MONTH_END), so it can't
be reused as-is for the 2026-02-11 date. This script instead calls
fetch_ais.fetch_presence_report() directly (the actual Milestone 2
function meant to be reused) with a date range computed per-date's own
calendar month, then applies the same one-line date filter match.py uses.

Run from the project root: `python src/run_multi_date.py`
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import array_bounds
from rasterio.warp import transform as warp_transform
from scipy.ndimage import binary_dilation

import detect
import fetch_ais
import fetch_sar
import land_mask
import match

# ---------------------------------------------------------------------------
# CONSTANTS - same values as the validated Milestone 1/2 run, tunable here
# ---------------------------------------------------------------------------

PROJECT_ID = "dark-vessel-detection-504204"  # same GEE cloud project as run_milestone1.py

AOI_LON_MIN, AOI_LAT_MIN, AOI_LON_MAX, AOI_LAT_MAX = 78.22, 8.72, 78.32, 8.85  # confirmed-good sub-box

DOWNLOAD_SCALE_M = 10
MAX_ALLOWED_RESOLUTION_M = 15.0  # skip the date if the actual download comes back coarser than this
MIN_SUBBOX_COVERAGE_FRAC = 0.90  # skip the date if valid (non-nodata) coverage of the sub-box is under this

CFAR_GUARD_PX = 5
CFAR_TRAINING_PX = 15
CFAR_K = 5.0
OPENING_ITERATIONS = 1
MIN_BLOB_PX = 2
MAX_BLOB_PX = 60
COAST_BUFFER_PX = 30

MATCH_RADIUS_M = 1500  # same primary radius as match.py
CONFIRMED_VESSEL_CONTRAST_DB = 15.0  # proxy for "likely real" among MATCHED detections - NOT visual confirmation
TOP_N_FLAG_FOR_REVIEW = 3
CROP_HALF_WINDOW_PX = 40

DATES = [
    {"date": "2026-01-06", "scene_id": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20260106T003258_20260106T003323_062639_07DA3D_1DC5"},
    {"date": "2026-01-30", "scene_id": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20260130T003256_20260130T003321_062989_07E743_2D77"},
    {"date": "2026-02-11", "scene_id": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20260211T003256_20260211T003321_063164_07EDCA_964B"},
]

DMC_JUPITER_MMSI = "511101582"  # the one vessel Milestone 2 independently visually + AIS confirmed on 2026-01-18

# ---------------------------------------------------------------------------
# Paths - Jan 18 baseline reused from Milestone 1/2/3, never written to
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MULTI_DATE_DIR = DATA_DIR / "multi_date"
RESULTS_DIR = PROJECT_ROOT / "results"

JAN18_MATCHED_PATH = DATA_DIR / "matched_detections.geojson"
JAN18_AIS_PATH = DATA_DIR / "ais_positions.json"
FINDINGS_PATH = RESULTS_DIR / "MULTI_DATE_FINDINGS.md"


def _month_bounds(date_str: str) -> tuple[str, str]:
    """First and last day of date_str's calendar month, as ISO strings -
    the wide date-range 4wings/report needs to return anything at all
    (see fetch_ais.py's docstring)."""
    d = dt.date.fromisoformat(date_str)
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=1).isoformat(), d.replace(day=last_day).isoformat()


def process_one_date(date_str: str, scene_id: str, gfw_token: str) -> dict:
    print(f"\n{'=' * 78}\n[run] Date {date_str}  scene={scene_id}\n{'=' * 78}")
    MULTI_DATE_DIR.mkdir(parents=True, exist_ok=True)
    tif_path = MULTI_DATE_DIR / f"{date_str}_sar_vv_clip.tif"

    if tif_path.exists():
        print(f"[fetch] Using cached scene at {tif_path}.")
    else:
        print("[fetch] Authenticating with Earth Engine...")
        fetch_sar.authenticate_and_init(PROJECT_ID)
        aoi = fetch_sar.build_aoi(AOI_LON_MIN, AOI_LAT_MIN, AOI_LON_MAX, AOI_LAT_MAX)
        print(f"[fetch] Downloading {scene_id} at {DOWNLOAD_SCALE_M}m/px...")
        fetch_sar.download_scene(aoi, scene_id, tif_path, scale=DOWNLOAD_SCALE_M)
        print(f"[fetch] Saved to {tif_path}")

    with rasterio.open(tif_path) as src:
        db = src.read(1).astype("float64")
        transform = src.transform
        nodata = src.nodata
        crs = src.crs

    actual_resolution_m = abs(transform.a)
    print(f"[fetch] Actual resolution: {actual_resolution_m:.1f} m/px (requested {DOWNLOAD_SCALE_M} m/px).")
    if actual_resolution_m > MAX_ALLOWED_RESOLUTION_M:
        reason = f"resolution {actual_resolution_m:.1f}m/px exceeds the {MAX_ALLOWED_RESOLUTION_M:.0f}m/px cap"
        print(f"[skip] {date_str}: {reason} - skipping.")
        return {"date": date_str, "status": "SKIPPED_RESOLUTION", "reason": reason}

    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)
    coverage_frac = valid.sum() / valid.size
    print(f"[coverage] {coverage_frac:.1%} valid (non-nodata) pixels.")
    if coverage_frac < MIN_SUBBOX_COVERAGE_FRAC:
        reason = f"coverage {coverage_frac:.1%} below the {MIN_SUBBOX_COVERAGE_FRAC:.0%} threshold"
        print(f"[skip] {date_str}: {reason} - skipping.")
        return {"date": date_str, "status": "SKIPPED_COVERAGE", "reason": reason}

    bounds = array_bounds(db.shape[0], db.shape[1], transform)

    print("[mask] Building sea mask from Natural Earth 10m land polygons...")
    sea_mask = land_mask.get_sea_mask(bounds=bounds, transform=transform, out_shape=db.shape, crs=crs, cache_dir=DATA_DIR)
    if COAST_BUFFER_PX > 0:
        land_bool = binary_dilation(~sea_mask, iterations=COAST_BUFFER_PX)
        sea_mask = ~land_bool
    sea_mask &= valid
    masked_frac = 1.0 - sea_mask.sum() / valid.sum()
    print(f"[mask] {masked_frac:.1%} masked after {COAST_BUFFER_PX}px coastal buffer.")

    intensity = detect.db_to_linear(db)
    detections = detect.detect_blobs(
        intensity, sea_mask,
        guard_px=CFAR_GUARD_PX, training_px=CFAR_TRAINING_PX, k=CFAR_K,
        opening_iterations=OPENING_ITERATIONS, min_blob_px=MIN_BLOB_PX, max_blob_px=MAX_BLOB_PX,
    )
    if detections:
        xs, ys = zip(*(detect.pixel_to_xy(d["row"], d["col"], transform) for d in detections))
        lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
        for d, lon, lat in zip(detections, lons, lats):
            d["lon"], d["lat"] = lon, lat
    print(f"[detect] {len(detections)} candidate detection(s) (CFAR k={CFAR_K}, "
          f"guard_px={CFAR_GUARD_PX}, training_px={CFAR_TRAINING_PX}).")

    month_start, month_end = _month_bounds(date_str)
    print(f"[ais] Fetching AIS presence for {month_start}..{month_end}, filtering to {date_str}...")
    ais_data = fetch_ais.fetch_presence_report(
        gfw_token, AOI_LON_MIN, AOI_LAT_MIN, AOI_LON_MAX, AOI_LAT_MAX, month_start, month_end
    )
    entries = ais_data.get("entries", [{}])[0].get("public-global-presence:v4.0") or []
    ais_records = [e for e in entries if e.get("date") == date_str]
    print(f"[ais] {len(ais_records)} AIS presence record(s) on {date_str}.")

    matched = match.match_detections(detections, ais_records, MATCH_RADIUS_M)
    n_matched = sum(1 for d in matched if d["match_status"] == "MATCHED")
    n_unmatched = len(matched) - n_matched
    n_confirmed_vessel = sum(
        1 for d in matched if d["match_status"] == "MATCHED" and d["contrast_db"] > CONFIRMED_VESSEL_CONTRAST_DB
    )
    print(f"[match] {n_matched} MATCHED, {n_unmatched} UNMATCHED, "
          f"{n_confirmed_vessel} confirmed_vessel-proxy (matched & contrast_db>{CONFIRMED_VESSEL_CONTRAST_DB:.0f}dB).")

    out_path = MULTI_DATE_DIR / f"{date_str}_detections.geojson"
    save_matched_geojson(matched, out_path)
    print(f"[save] Saved {out_path}")

    return {
        "date": date_str,
        "status": "OK",
        "n_detections": len(matched),
        "n_matched": n_matched,
        "n_unmatched": n_unmatched,
        "n_confirmed_vessel": n_confirmed_vessel,
        "coverage_frac": float(coverage_frac),
        "masked_frac": float(masked_frac),
        "ais_records": ais_records,
        "matched": matched,
    }


def save_matched_geojson(matched: list[dict], out_path: Path) -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")},
        }
        for d in matched
    ]
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2), encoding="utf-8")


def save_crop_png(tif_path: Path, row: float, col: float, out_path: Path, half_window_px: int = CROP_HALF_WINDOW_PX) -> None:
    """2nd-98th percentile grayscale crop around one detection, same rough
    style as Milestone 1's evidence crops - for the handful of detections
    explicitly flagged for manual eyeball review, not generated in bulk."""
    from PIL import Image

    with rasterio.open(tif_path) as src:
        db = src.read(1).astype("float64")
        nodata = src.nodata

    r, c = int(round(row)), int(round(col))
    r0, r1 = max(0, r - half_window_px), min(db.shape[0], r + half_window_px)
    c0, c1 = max(0, c - half_window_px), min(db.shape[1], c + half_window_px)
    crop = db[r0:r1, c0:c1]

    valid = np.isfinite(crop) if nodata is None else (crop != nodata) & np.isfinite(crop)
    if not valid.any():
        return
    lo, hi = np.percentile(crop[valid], [2, 98])
    stretched = np.clip((crop - lo) / (hi - lo), 0, 1)
    gray = (stretched * 255).astype("uint8")
    Image.fromarray(gray, mode="L").save(out_path)


def load_jan18_baseline() -> dict:
    """Reuse the already-validated Jan 18 run (Milestone 2's
    matched_detections.geojson + Milestone 3's cached AIS snapshot) rather
    than re-fetching, so the 4-date summary is on a consistent footing."""
    data = json.loads(JAN18_MATCHED_PATH.read_text(encoding="utf-8"))
    dets = []
    for f in data["features"]:
        props = dict(f["properties"])
        props["lon"], props["lat"] = f["geometry"]["coordinates"]
        dets.append(props)
    ais_records = json.loads(JAN18_AIS_PATH.read_text(encoding="utf-8")) if JAN18_AIS_PATH.exists() else []

    n_matched = sum(1 for d in dets if d["match_status"] == "MATCHED")
    n_confirmed_vessel = sum(
        1 for d in dets if d["match_status"] == "MATCHED" and d["contrast_db"] > CONFIRMED_VESSEL_CONTRAST_DB
    )
    return {
        "date": "2026-01-18",
        "status": "OK",
        "n_detections": len(dets),
        "n_matched": n_matched,
        "n_unmatched": len(dets) - n_matched,
        "n_confirmed_vessel": n_confirmed_vessel,
        "ais_records": ais_records,
        "matched": dets,
    }


def print_summary_table(results: list[dict]) -> None:
    print(f"\n{'=' * 78}")
    print("[summary] Multi-date pipeline results (new dates only):")
    print(f"{'date':<12}{'detections':<12}{'matched':<10}{'unmatched':<11}{'confirmed_vessel*':<18}")
    for r in results:
        if r["status"] != "OK":
            print(f"{r['date']:<12}{'SKIPPED':<12}{'-':<10}{'-':<11}{r['reason']}")
        else:
            print(f"{r['date']:<12}{r['n_detections']:<12}{r['n_matched']:<10}{r['n_unmatched']:<11}{r['n_confirmed_vessel']:<18}")
    print(f"\n* confirmed_vessel_count = MATCHED detections with contrast_db > {CONFIRMED_VESSEL_CONTRAST_DB:.0f}dB - "
          f"a rough proxy (we haven't visually reviewed these dates), not the visual ground truth Milestone 1 used.")


def top_flagged_detections(results: list[dict]) -> list[dict]:
    candidates = []
    for r in results:
        if r["status"] != "OK":
            continue
        for d in r["matched"]:
            if d["match_status"] == "MATCHED":
                candidates.append({**d, "date": r["date"]})
    candidates.sort(key=lambda d: d["contrast_db"], reverse=True)
    top = candidates[:TOP_N_FLAG_FOR_REVIEW]

    print(f"\n{'=' * 78}")
    print(f"[flag] Top {len(top)} highest-contrast MATCHED detections across the new dates (for manual eyeball review):")
    for i, d in enumerate(top, start=1):
        print(f"    #{i}  date={d['date']}  lon={d['lon']:.5f}  lat={d['lat']:.5f}  "
              f"contrast_db={d['contrast_db']:.1f}  shape={d['shape_label']}  "
              f"-> {d['matched_name']} (MMSI {d['matched_mmsi']}, {d['match_distance_m']:.0f}m)")

    for i, d in enumerate(top, start=1):
        tif_path = MULTI_DATE_DIR / f"{d['date']}_sar_vv_clip.tif"
        crop_path = RESULTS_DIR / f"multi_date_flagged_{i}_{d['date']}_crop.png"
        save_crop_png(tif_path, d["row"], d["col"], crop_path)
        print(f"[flag] Saved crop: {crop_path}")

    return top


def _mmsi_sets(all_results: list[dict], field: str) -> dict[str, set[str]]:
    """field='ais' -> all AIS presence MMSIs seen that date. field='matched'
    -> only MMSIs actually matched to a SAR detection that date."""
    out = {}
    for r in all_results:
        if r["status"] != "OK":
            continue
        if field == "ais":
            out[r["date"]] = {str(e["mmsi"]) for e in r["ais_records"]}
        else:
            out[r["date"]] = {str(d["matched_mmsi"]) for d in r["matched"] if d.get("matched_mmsi")}
    return out


def _recurring(sets_by_date: dict[str, set[str]]) -> dict[str, list[str]]:
    counts = defaultdict(list)
    for date, mmsis in sets_by_date.items():
        for m in mmsis:
            counts[m].append(date)
    return {m: dates for m, dates in counts.items() if len(dates) >= 2}


def _mmsi_name_lookup(all_results: list[dict]) -> dict[str, str]:
    lookup = {}
    for r in all_results:
        if r["status"] != "OK":
            continue
        for e in r["ais_records"]:
            lookup[str(e["mmsi"])] = e.get("shipName", "?")
    return lookup


def write_findings_md(all_results: list[dict], top_flagged: list[dict]) -> None:
    ok_results = [r for r in all_results if r["status"] == "OK"]
    total_detections = sum(r["n_detections"] for r in ok_results)
    total_matched = sum(r["n_matched"] for r in ok_results)
    total_confirmed_proxy = sum(r["n_confirmed_vessel"] for r in ok_results)

    ais_sets = _mmsi_sets(all_results, "ais")
    matched_sets = _mmsi_sets(all_results, "matched")
    ais_recurring = _recurring(ais_sets)
    matched_recurring = _recurring(matched_sets)
    names = _mmsi_name_lookup(all_results)

    jupiter_dates = [date for date, mmsis in ais_sets.items() if DMC_JUPITER_MMSI in mmsis]
    jupiter_matched_dates = [date for date, mmsis in matched_sets.items() if DMC_JUPITER_MMSI in mmsis]

    skipped = [r for r in all_results if r["status"] != "OK"]

    lines = []
    lines.append("# Multi-Date Findings — Same Anchorage, 4 Passes\n")
    lines.append(
        "Extends Milestone 1/2's single validated date (2026-01-18) to 3 more Sentinel-1 "
        "passes over the identical Tuticorin anchorage sub-box (lon 78.22-78.32, lat "
        "8.72-8.85, relative orbit 92), running the same fetch -> land-mask -> CA-CFAR "
        "detect -> shape filter -> AIS match pipeline unmodified. Code: "
        "[`src/run_multi_date.py`](../src/run_multi_date.py). Data: "
        "`data/multi_date/{date}_detections.geojson`.\n"
    )

    lines.append("## 1. Totals across all 4 dates\n")
    lines.append("| date | status | detections | matched | confirmed_vessel* |")
    lines.append("|---|---|---|---|---|")
    for r in all_results:
        if r["status"] == "OK":
            lines.append(f"| {r['date']} | OK | {r['n_detections']} | {r['n_matched']} | {r['n_confirmed_vessel']} |")
        else:
            lines.append(f"| {r['date']} | {r['status']} | - | - | - |")
    lines.append(f"| **total (OK dates)** | | **{total_detections}** | **{total_matched}** | **{total_confirmed_proxy}** |\n")
    lines.append(
        f"\\* confirmed_vessel_count = MATCHED detections with contrast_db > "
        f"{CONFIRMED_VESSEL_CONTRAST_DB:.0f}dB, applied uniformly (including recomputed "
        f"for 2026-01-18) as a proxy since we haven't manually eyeballed every date. This "
        f"is looser than Milestone 1's visual ground truth: on 2026-01-18 alone, this "
        f"proxy flags 6 detections (#1-#6) as confirmed_vessel, vs only 2 (#3, #4) that "
        f"visual crop review actually confirmed. Treat this column as \"worth reviewing,\" "
        f"not \"confirmed real.\"\n"
    )

    lines.append("## 2. Does the same vessel reappear across dates?\n")
    if jupiter_dates:
        lines.append(
            f"**DMC JUPITER (MMSI {DMC_JUPITER_MMSI})** — the vessel Milestone 2 matched to "
            f"detections #3/#4 on 2026-01-18 — also shows up in the AIS presence data on: "
            f"{', '.join(jupiter_dates)}. It was matched to an actual SAR detection (not just "
            f"present in the box) on: {', '.join(jupiter_matched_dates) if jupiter_matched_dates else 'none of the new dates'}.\n"
        )
    else:
        lines.append(
            f"**DMC JUPITER (MMSI {DMC_JUPITER_MMSI})** does not reappear in the AIS presence "
            f"data on any of the 3 new dates — either it didn't transit this anchorage on "
            f"those dates, or (per Milestone 2 §2) GFW's presence endpoint's known "
            f"non-determinism dropped the record.\n"
        )

    if matched_recurring:
        lines.append("Vessels matched to a SAR detection on 2 or more dates (persistent, SAR-and-AIS-confirmed traffic):\n")
        lines.append("| MMSI | name | dates |")
        lines.append("|---|---|---|")
        for mmsi, dates in sorted(matched_recurring.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"| {mmsi} | {names.get(mmsi, '?')} | {', '.join(dates)} |")
        lines.append("")
    else:
        lines.append("No vessel was matched to a SAR detection on more than one date.\n")

    if ais_recurring:
        n_recurring_ais_only = len(ais_recurring) - len(matched_recurring)
        lines.append(
            f"Looking at raw AIS presence (not just SAR-matched) in the box: "
            f"{len(ais_recurring)} vessel(s) appear on 2+ dates, of which "
            f"{len(matched_recurring)} were also matched to a SAR detection on 2+ dates. "
            f"The remaining {max(n_recurring_ais_only, 0)} are AIS-visible repeat traffic "
            f"that our detector didn't pick up on multiple passes - consistent with "
            f"Milestone 1/2's honest recall limitation, not new information.\n"
        )

    lines.append("## 3. Dates that failed or had poor coverage\n")
    if skipped:
        for r in skipped:
            lines.append(f"- **{r['date']}** — {r['status']}: {r['reason']}")
        lines.append("")
    else:
        lines.append("None - all 3 new dates cleared the resolution and coverage gates.\n")

    lines.append("## 4. Top detections flagged for manual review\n")
    if top_flagged:
        lines.append(
            f"The {len(top_flagged)} highest-contrast MATCHED detections across the 3 new "
            f"dates (not yet visually confirmed the way #3/#4 were for 2026-01-18 - crops "
            f"saved to `results/multi_date_flagged_*_crop.png` for that review):\n"
        )
        lines.append("| # | date | lon | lat | contrast_db | shape | matched vessel | dist_m |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, d in enumerate(top_flagged, start=1):
            lines.append(
                f"| {i} | {d['date']} | {d['lon']:.5f} | {d['lat']:.5f} | {d['contrast_db']:.1f} | "
                f"{d['shape_label']} | {d['matched_name']} (MMSI {d['matched_mmsi']}) | {d['match_distance_m']:.0f} |"
            )
        lines.append("")
    else:
        lines.append("No MATCHED detections on any of the 3 new dates to flag.\n")

    lines.append("## 5. Caveats carried over from Milestone 2\n")
    lines.append(
        "- `confirmed_vessel_count` here is a contrast-threshold proxy, not visual "
        "confirmation - see §1's note. Don't read it as equivalent to Milestone 1's "
        "manually-reviewed #3/#4.\n"
        "- GFW's `4wings/report` is non-deterministic across identical queries "
        "(Milestone 2 §2); the MMSI overlap in §2 is a snapshot from this run, not a "
        "stable ground truth - re-running this script could return different AIS records "
        "for the same dates.\n"
        "- Match status still doesn't distinguish real vessels from linear-feature/speckle "
        "artifacts without visual review (Milestone 2 §5); the same caution applies to "
        "every detection in this document.\n"
    )

    FINDINGS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[findings] Saved {FINDINGS_PATH}")


def main() -> None:
    MULTI_DATE_DIR.mkdir(parents=True, exist_ok=True)
    gfw_token = fetch_ais.get_token()

    results = []
    for entry in DATES:
        try:
            result = process_one_date(entry["date"], entry["scene_id"], gfw_token)
        except Exception as e:
            reason = f"unexpected error: {e!r}"
            print(f"[skip] {entry['date']}: {reason} - skipping.")
            result = {"date": entry["date"], "status": "SKIPPED_ERROR", "reason": reason}
        results.append(result)

    print_summary_table(results)
    top_flagged = top_flagged_detections(results)

    jan18 = load_jan18_baseline()
    all_results = [jan18] + results
    write_findings_md(all_results, top_flagged)


if __name__ == "__main__":
    main()
