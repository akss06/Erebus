"""
Dark Vessel Detection - Milestone 1.

Pulls one real Sentinel-1 GRD VV scene over a small AOI in the Palk Strait,
masks land, detects ship-like bright blobs, and plots them on a folium map.
Nothing beyond this: no AIS, no matching, no Streamlit app (see PROJECT_SPEC.md
for the full multi-milestone plan - those come later).

Usage (two passes, because you can't pick a date that doesn't exist):
  1. Run with SCENE_PRODUCT_ID = None. This authenticates, queries Earth Engine
     for real available passes over the AOI, and prints them. Nothing is
     downloaded yet.
  2. Copy one printed "id" value into SCENE_PRODUCT_ID below and re-run. This
     downloads the scene to data/ and runs mask+detect+map.

After that first download, the scene is cached on disk - re-running this
script (e.g. while tuning the DETECT_* constants) skips Earth Engine entirely
and only redoes the fast, local mask/detect/map steps. Set FORCE_REDOWNLOAD =
True if you want to fetch a different scene.
"""
from __future__ import annotations

import json
from pathlib import Path

import folium
import numpy as np
import rasterio
from rasterio.transform import array_bounds as rio_array_bounds
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from rasterio.windows import from_bounds as rio_window_from_bounds
from rasterio.windows import transform as rio_window_transform
from scipy.ndimage import binary_dilation, distance_transform_edt

from src import detect, fetch_sar, land_mask

# ---------------------------------------------------------------------------
# CONSTANTS - edit these
# ---------------------------------------------------------------------------

# --- Google Earth Engine ---
PROJECT_ID = "dark-vessel-detection-504204"  # TODO: your GEE cloud project id/number - edit if this isn't it

# --- Area of interest: outer Tuticorin anchorage, pushed east of the port
# complex/breakwaters entirely (those sit roughly west of 78.20E and turned
# out to dominate the previous run's detections). ---
AOI_LON_MIN = 78.22
AOI_LAT_MIN = 8.72
AOI_LON_MAX = 78.32
AOI_LAT_MAX = 8.85

# --- Detection sub-box: same as the AOI - the whole downloaded box is the
# analysis region, no further clipping needed. ---
DETECT_LON_MIN = AOI_LON_MIN
DETECT_LAT_MIN = AOI_LAT_MIN
DETECT_LON_MAX = AOI_LON_MAX
DETECT_LAT_MAX = AOI_LAT_MAX

# --- Which scene to download ---
# Leave as None to just print available dates and stop (pass 1, see module
# docstring). Copy a printed "id" here and re-run to download it (pass 2).
SCENE_PRODUCT_ID = "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20260118T003257_20260118T003322_062814_07E0F8_F7E0"

MONTHS_BACK = 12  # how far back to search for available scenes
DOWNLOAD_SCALE_M = 10  # meters/pixel - small 10x10km box, should fit the 48MiB cap at full resolution
MAX_ALLOWED_RESOLUTION_M = 15.0  # if the actual download comes back coarser than this, stop before detecting
FORCE_REDOWNLOAD = False  # True to re-fetch even if a local GeoTIFF already exists

# --- Detection tuning - CA-CFAR (replaces the v1 global percentile threshold) ---
CFAR_GUARD_PX = 5  # guard band half-width around the cell under test
CFAR_TRAINING_PX = 15  # training ring width beyond the guard band, used for local mean/std
CFAR_K = 5.0  # threshold = local_mean + k * local_std; higher k -> stricter
OPENING_ITERATIONS = 1  # morphological opening iterations (speckle removal)
MIN_BLOB_PX = 2  # drop blobs smaller than this (likely speckle)
MAX_BLOB_PX = 60  # drop blobs bigger than this (likely not a single ship)
COAST_BUFFER_PX = 30  # dilate the land mask by this many pixels (300m @ 10m/px) to catch near-shore clutter

# --- Data-quality gates ---
MIN_FULL_AOI_COVERAGE_FRAC = 0.60  # full downloaded AOI must have at least this much non-nodata data
# Tuticorin AOI deliberately includes the port/coast, so a much larger masked
# fraction is expected and fine here - only stop if almost nothing is left.
MAX_SUBBOX_MASKED_FRAC = 0.90
EDGE_ARTIFACT_DISTANCE_PX = 5  # detections this close to a nodata pixel are flagged as suspected edge artifacts
HIGH_INTENSITY_DB_THRESHOLD = -10.0  # detections brighter than this are flagged as possible land clutter
MAX_DETECTIONS_FOR_CROPS = 100  # above this, print count + top 20 by contrast instead of generating crops

# --- Paths ---
DATA_DIR = Path(__file__).resolve().parent / "data"
SAR_TIF_PATH = DATA_DIR / "sar_vv_clip.tif"
MAP_HTML_PATH = DATA_DIR / "detections_map.html"
OVERLAY_PNG_PATH = DATA_DIR / "sar_overlay.png"
DETECTIONS_GEOJSON_PATH = DATA_DIR / "detections.geojson"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    aoi_bounds = (AOI_LON_MIN, AOI_LAT_MIN, AOI_LON_MAX, AOI_LAT_MAX)

    # ---- Steps 1-3: get a local GeoTIFF (cached after the first successful run) ----
    if SAR_TIF_PATH.exists() and not FORCE_REDOWNLOAD:
        print(f"[fetch] Using cached scene at {SAR_TIF_PATH} "
              f"(set FORCE_REDOWNLOAD=True to re-fetch).")
    else:
        print("[fetch] Authenticating with Earth Engine...")
        fetch_sar.authenticate_and_init(PROJECT_ID)
        aoi = fetch_sar.build_aoi(*aoi_bounds)

        print(f"[fetch] Querying COPERNICUS/S1_GRD (IW, VV) over AOI, "
              f"last {MONTHS_BACK} months...")
        scenes = fetch_sar.list_available_scenes(aoi, months_back=MONTHS_BACK)

        if not scenes:
            print("[fetch] No scenes found for this AOI/window. Widen MONTHS_BACK "
                  "or double-check the AOI coordinates.")
            return

        print(f"[fetch] {len(scenes)} scene(s) available:")
        for s in scenes:
            print(f"    {s['date']}  orbit={s['orbit_direction']:<10}  "
                  f"rel_orbit={s['relative_orbit']}  id={s['product_id']}")

        if SCENE_PRODUCT_ID is None:
            print("\n[fetch] SCENE_PRODUCT_ID is not set. Copy one 'id' from the "
                  "list above into SCENE_PRODUCT_ID and re-run this script.")
            return

        print(f"[fetch] Downloading {SCENE_PRODUCT_ID} at {DOWNLOAD_SCALE_M} m/px...")
        fetch_sar.download_scene(aoi, SCENE_PRODUCT_ID, SAR_TIF_PATH, scale=DOWNLOAD_SCALE_M)
        print(f"[fetch] Saved to {SAR_TIF_PATH}")

    # ---- Full-AOI coverage gate (checked BEFORE clipping to the sub-box) ----
    with rasterio.open(SAR_TIF_PATH) as src:
        full_db = src.read(1).astype("float64")
        full_transform = src.transform
        nodata = src.nodata
        crs = src.crs

    actual_resolution_m = abs(full_transform.a)
    print(f"[fetch] Actual downloaded resolution: {actual_resolution_m:.1f} m/px "
          f"(requested {DOWNLOAD_SCALE_M} m/px).")
    if actual_resolution_m > MAX_ALLOWED_RESOLUTION_M:
        print(f"[fetch] Coarser than the {MAX_ALLOWED_RESOLUTION_M:.0f}m/px floor - stopping before "
              f"detection. Shrink the AOI/sub-box further and re-download.")
        return

    full_valid = np.isfinite(full_db) if nodata is None else (full_db != nodata) & np.isfinite(full_db)
    full_coverage_frac = full_valid.sum() / full_valid.size
    print(f"[coverage] Full AOI coverage: {full_coverage_frac:.1%} valid (non-nodata) SAR data "
          f"({full_db.shape[1]}x{full_db.shape[0]} px).")
    if full_coverage_frac < MIN_FULL_AOI_COVERAGE_FRAC:
        print(f"[coverage] Below the {MIN_FULL_AOI_COVERAGE_FRAC:.0%} threshold - stopping. "
              f"This scene doesn't usefully cover the AOI; try a different orbit/date.")
        return

    # ---- Clip to the open-water detection sub-box (slices the already-downloaded
    # raster in-memory; no re-download) ----
    sub_bounds_native = transform_bounds(
        "EPSG:4326", crs, DETECT_LON_MIN, DETECT_LAT_MIN, DETECT_LON_MAX, DETECT_LAT_MAX
    )
    window = rio_window_from_bounds(*sub_bounds_native, transform=full_transform)
    window = window.round_offsets().round_lengths()
    row_off = max(0, int(window.row_off))
    col_off = max(0, int(window.col_off))
    row_stop = min(full_db.shape[0], row_off + int(window.height))
    col_stop = min(full_db.shape[1], col_off + int(window.width))

    db = full_db[row_off:row_stop, col_off:col_stop]
    shape = db.shape
    transform = rio_window_transform(
        Window(col_off, row_off, col_stop - col_off, row_stop - row_off), full_transform
    )
    bounds = rio_array_bounds(shape[0], shape[1], transform)  # (west, south, east, north), native CRS

    print(f"[clip] Detection sub-box (lon {DETECT_LON_MIN}-{DETECT_LON_MAX}, "
          f"lat {DETECT_LAT_MIN}-{DETECT_LAT_MAX}) -> {shape[1]}x{shape[0]} px, sliced from the "
          f"{full_db.shape[1]}x{full_db.shape[0]} px downloaded scene. No re-download.")

    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)

    # ---- Land mask, dilated by a coastal buffer ----
    print(f"[mask] Raster CRS is {crs} (native units, not necessarily lon/lat) - "
          f"reprojecting land polygons into it, and detections/bounds out of it.")
    print("[mask] Building sea mask from Natural Earth 10m land polygons...")
    sea_mask = land_mask.get_sea_mask(
        bounds=bounds,
        transform=transform,
        out_shape=shape,
        crs=crs,
        cache_dir=DATA_DIR,
    )
    if COAST_BUFFER_PX > 0:
        land_bool = binary_dilation(~sea_mask, iterations=COAST_BUFFER_PX)
        sea_mask = ~land_bool
    sea_mask &= valid
    masked_frac = 1.0 - sea_mask.sum() / valid.sum()
    print(f"[mask] Sub-box land mask (with {COAST_BUFFER_PX}px / {COAST_BUFFER_PX * DOWNLOAD_SCALE_M}m "
          f"buffer): {masked_frac:.1%} masked.")

    if masked_frac > MAX_SUBBOX_MASKED_FRAC:
        print(f"[mask] Above the {MAX_SUBBOX_MASKED_FRAC:.0%} threshold - stopping before detection. "
              f"Sub-box bounds: lon {DETECT_LON_MIN}-{DETECT_LON_MAX}, lat {DETECT_LAT_MIN}-{DETECT_LAT_MAX}.")
        return

    # ---- Steps 5-6: detect blobs via CA-CFAR ----
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

        # Distance (in pixels) from every valid pixel to the nearest nodata pixel,
        # in one pass - flags detections that are really nodata-boundary artifacts
        # rather than genuine bright blobs out in open water.
        edge_dist_map = distance_transform_edt(valid)
        for d in detections:
            r, c = int(round(d["row"])), int(round(d["col"]))
            d["edge_dist_px"] = float(edge_dist_map[r, c])
            d["suspected_edge_artifact"] = d["edge_dist_px"] <= EDGE_ARTIFACT_DISTANCE_PX
            d["high_intensity_flag"] = d["mean_intensity_db"] > HIGH_INTENSITY_DB_THRESHOLD

    print(f"[detect] {len(detections)} candidate detection(s) "
          f"(CA-CFAR guard_px={CFAR_GUARD_PX}, training_px={CFAR_TRAINING_PX}, k={CFAR_K}, "
          f"blob area {MIN_BLOB_PX}-{MAX_BLOB_PX} px, opening_iterations={OPENING_ITERATIONS}, "
          f"coast_buffer_px={COAST_BUFFER_PX}).")

    n_flagged_high_intensity = sum(1 for d in detections if d["high_intensity_flag"])
    n_clean = len(detections) - n_flagged_high_intensity
    print(f"[detect] {n_clean} clean (<={HIGH_INTENSITY_DB_THRESHOLD:.0f}dB) vs "
          f"{n_flagged_high_intensity} flagged high-intensity (>{HIGH_INTENSITY_DB_THRESHOLD:.0f}dB).")

    if detections:
        detections_sorted = sorted(detections, key=lambda d: d["contrast_db"], reverse=True)
        if len(detections) > MAX_DETECTIONS_FOR_CROPS:
            print(f"[detect] {len(detections)} detections exceeds {MAX_DETECTIONS_FOR_CROPS} - "
                  f"printing top 20 by contrast_db instead of full listing/crops:")
            for i, d in enumerate(detections_sorted[:20], start=1):
                print(f"    #{i}  lon={d['lon']:.5f}  lat={d['lat']:.5f}  area_px={d['area_px']}  "
                      f"mean_intensity_db={d['mean_intensity_db']:.1f}  contrast_db={d['contrast_db']:.1f}  "
                      f"aspect={d['aspect_ratio']:.1f}  fill={d['fill_ratio']:.2f}  {d['shape_label']}")
        else:
            for i, d in enumerate(detections_sorted, start=1):
                print(f"    #{i}  lon={d['lon']:.5f}  lat={d['lat']:.5f}  area_px={d['area_px']}  "
                      f"mean_intensity_db={d['mean_intensity_db']:.1f}  contrast_db={d['contrast_db']:.1f}  "
                      f"aspect={d['aspect_ratio']:.1f}  fill={d['fill_ratio']:.2f}  {d['shape_label']}")

        n_ship = sum(1 for d in detections if d["shape_label"] == "SHIP-LIKE")
        n_linear = sum(1 for d in detections if d["shape_label"] == "LINEAR-STRUCTURE")
        n_blocky = sum(1 for d in detections if d["shape_label"] == "BLOCKY")
        n_ambiguous = sum(1 for d in detections if d["shape_label"] == "AMBIGUOUS")
        print(f"[shape] SHIP-LIKE={n_ship}  LINEAR-STRUCTURE={n_linear}  BLOCKY={n_blocky}  AMBIGUOUS={n_ambiguous}")
    else:
        # ---- Step 2 (only reached if Step 1 found nothing): sea-pixel intensity
        # stats, to tell whether the scene has any bright targets at all or is
        # just uniformly quiet in this sub-box. ----
        sea_db = db[sea_mask]
        pcts = np.percentile(sea_db, [95, 99, 99.5])
        print("[stats] Zero detections - sea-pixel (masked, valid) intensity distribution in this sub-box:")
        print(f"    mean={sea_db.mean():.2f}dB  median={np.median(sea_db):.2f}dB  std={sea_db.std():.2f}dB")
        print(f"    p95={pcts[0]:.2f}dB  p99={pcts[1]:.2f}dB  p99.5={pcts[2]:.2f}dB")
        print(f"    min={sea_db.min():.2f}dB  max={sea_db.max():.2f}dB")

    save_detections_geojson(detections, DETECTIONS_GEOJSON_PATH)
    print(f"[detect] Saved {DETECTIONS_GEOJSON_PATH}")

    # ---- Step 7: map ----
    overlay_bounds_wgs84 = transform_bounds(crs, "EPSG:4326", *bounds)
    build_overlay_png(db, valid, OVERLAY_PNG_PATH)
    build_map(overlay_bounds_wgs84, detections, OVERLAY_PNG_PATH, MAP_HTML_PATH)
    print(f"[map] Saved {MAP_HTML_PATH}")


def save_detections_geojson(detections: list[dict], out_path: Path) -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {
                "lon": d["lon"],
                "lat": d["lat"],
                "area_px": d["area_px"],
                "mean_intensity_db": d["mean_intensity_db"],
                "max_intensity_db": d["max_intensity_db"],
                "local_background_db": d["local_background_db"],
                "contrast_db": d["contrast_db"],
                "aspect_ratio": d["aspect_ratio"],
                "fill_ratio": d["fill_ratio"],
                "shape_label": d["shape_label"],
                "edge_dist_px": d["edge_dist_px"],
                "suspected_edge_artifact": d["suspected_edge_artifact"],
                "high_intensity_flag": d["high_intensity_flag"],
            },
        }
        for d in detections
    ]
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))


def build_overlay_png(db: np.ndarray, valid: np.ndarray, out_path: Path) -> None:
    """Render the SAR dB band as a grayscale PNG: 2nd-98th percentile contrast
    stretch over valid pixels, normalized to 0-255."""
    from PIL import Image

    lo, hi = np.percentile(db[valid], [2, 98])
    stretched = np.clip((db - lo) / (hi - lo), 0, 1)
    gray = (stretched * 255).astype("uint8")
    Image.fromarray(gray, mode="L").save(out_path)


def build_map(overlay_bounds_wgs84, detections: list[dict], overlay_png_path: Path, out_path: Path) -> None:
    """overlay_bounds_wgs84: (west, south, east, north) in EPSG:4326, i.e. the
    downloaded raster's own footprint reprojected to lon/lat - NOT the same
    object as the requested AOI_* constants, though they should nearly match."""
    west, south, east, north = overlay_bounds_wgs84

    center_lat = (DETECT_LAT_MIN + DETECT_LAT_MAX) / 2
    center_lon = (DETECT_LON_MIN + DETECT_LON_MAX) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=14, tiles="OpenStreetMap")

    folium.Rectangle(
        bounds=[[AOI_LAT_MIN, AOI_LON_MIN], [AOI_LAT_MAX, AOI_LON_MAX]],
        color="blue",
        weight=1,
        fill=False,
        tooltip="Full downloaded AOI",
    ).add_to(m)

    folium.Rectangle(
        bounds=[[DETECT_LAT_MIN, DETECT_LON_MIN], [DETECT_LAT_MAX, DETECT_LON_MAX]],
        color="green",
        weight=2,
        fill=False,
        tooltip="Detection sub-box (open water only)",
    ).add_to(m)

    folium.raster_layers.ImageOverlay(
        image=str(overlay_png_path),
        bounds=[[south, west], [north, east]],
        opacity=0.85,
        name="SAR VV intensity",
    ).add_to(m)

    for i, d in enumerate(detections, start=1):
        flag_lines = []
        if d.get("suspected_edge_artifact", False):
            flag_lines.append("<b>SUSPECTED EDGE ARTIFACT</b>")
        if d.get("high_intensity_flag", False):
            flag_lines.append("<b>high intensity - possible land clutter, review manually</b>")
        color = "orange" if flag_lines else "red"
        flag_html = "<br>" + "<br>".join(flag_lines) if flag_lines else ""
        folium.CircleMarker(
            location=[d["lat"], d["lon"]],
            radius=6,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            popup=folium.Popup(
                f"Detection #{i}<br>"
                f"area: {d['area_px']} px<br>"
                f"mean: {d['mean_intensity_db']:.1f} dB<br>"
                f"max: {d['max_intensity_db']:.1f} dB<br>"
                f"edge_dist: {d['edge_dist_px']:.1f} px{flag_html}",
                max_width=220,
            ),
        ).add_to(m)

    folium.LayerControl().add_to(m)
    m.save(str(out_path))


if __name__ == "__main__":
    main()
