"""
Milestone 3: Streamlit app for the Dark Vessel Detection project.

Renders Milestone 1's SAR detections and Milestone 2's AIS matches on a map,
with a live match-radius slider and a methodology/limitations writeup.

Reads only cached files - data/matched_detections.geojson,
data/overlay_bounds.json + data/overlay_rgba.png (precomputed from
data/sar_vv_clip.tif by src/precompute_overlay_bounds.py),
data/ais_positions.json, config/milestone1_confirmed.json,
results/MILESTONE2_FINDINGS.md - so the app runs fully offline, starts
fast, and needs no geo-processing library (rasterio, GDAL, etc.) at
runtime. No live GEE or GFW calls at runtime. Does not modify any
Milestone 1 or 2 code.

Run: streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "milestone1_confirmed.json"
MATCHED_GEOJSON_PATH = PROJECT_ROOT / "data" / "matched_detections.geojson"
AIS_POSITIONS_PATH = PROJECT_ROOT / "data" / "ais_positions.json"
OVERLAY_BOUNDS_PATH = PROJECT_ROOT / "data" / "overlay_bounds.json"
FINDINGS_MD_PATH = PROJECT_ROOT / "results" / "MILESTONE2_FINDINGS.md"

VISUAL_CLASS_COLORS = {
    "CONFIRMED_VESSEL": "green",
    "LINEAR_FEATURE": "gray",
    "SPECKLE": "gray",
    "UNEXPLAINED": "orange",
}
NON_VESSEL_CLASSES = ("LINEAR_FEATURE", "SPECKLE")
DMC_JUPITER_MMSI = "511101582"

st.set_page_config(page_title="Dark Vessel Detection", layout="wide")


@st.cache_data
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


@st.cache_data
def load_detections() -> list[dict]:
    data = json.loads(MATCHED_GEOJSON_PATH.read_text())
    detections = []
    for feature in data["features"]:
        props = dict(feature["properties"])
        props["lon"], props["lat"] = feature["geometry"]["coordinates"]
        detections.append(props)
    return detections


@st.cache_data
def load_ais_positions() -> list[dict]:
    if not AIS_POSITIONS_PATH.exists():
        return []
    return json.loads(AIS_POSITIONS_PATH.read_text())


@st.cache_data
def load_findings_md() -> str:
    return FINDINGS_MD_PATH.read_text()


@st.cache_data
def load_overlay() -> tuple[str, list[list[float]]]:
    """Reads the SAR overlay image path + its [[south, west], [north, east]]
    EPSG:4326 bounds from data/overlay_bounds.json, precomputed offline by
    src/precompute_overlay_bounds.py - keeps rasterio/GDAL off the Streamlit
    runtime dependency list entirely. Same simplification Milestone 1's own
    map used: the raster's small UTM footprint is treated as an
    axis-aligned lon/lat rectangle, since folium's ImageOverlay can't
    render an arbitrarily rotated grid."""
    meta = json.loads(OVERLAY_BOUNDS_PATH.read_text(encoding="utf-8"))
    overlay_png_path = str(PROJECT_ROOT / "data" / meta["overlay_png"])
    return overlay_png_path, meta["bounds"]


def recompute_match_status(detections: list[dict], radius_m: float) -> list[dict]:
    """Recompute MATCHED/UNMATCHED live from the match_distance_m already
    stored in matched_detections.geojson - that's the nearest-AIS-record
    distance, which doesn't change with radius, only whether it counts as
    a match does. Vessel identity (name/MMSI/flag/type) was only saved for
    detections that matched at match.py's primary 1500m radius, so a
    detection whose nearest AIS record sits between 1500m and a slider
    value above that shows as matched-but-vessel-unknown - flagged rather
    than silently wrong."""
    out = []
    for det in detections:
        d = dict(det)
        dist = d.get("match_distance_m")
        is_match = dist is not None and dist <= radius_m
        d["live_match_status"] = "MATCHED" if is_match else "UNMATCHED"
        d["live_vessel_unknown"] = bool(is_match and not d.get("matched_mmsi"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.header("Match settings")
radius_m = st.sidebar.slider("Match radius (m)", min_value=250, max_value=2000, value=1500, step=50)
show_non_vessel = st.sidebar.checkbox("Show LINEAR_FEATURE / SPECKLE detections", value=True)

all_detections = load_detections()
detections_live = recompute_match_status(all_detections, radius_m)
n_matched = sum(1 for d in detections_live if d["live_match_status"] == "MATCHED")
n_unmatched = len(detections_live) - n_matched

st.sidebar.metric("Matched", n_matched)
st.sidebar.metric("Unmatched", n_unmatched)
st.sidebar.caption(
    "Distance is fixed (nearest AIS record, computed once by match.py); only "
    "the MATCHED/UNMATCHED threshold moves with the slider."
)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Dark Vessel Detection — SAR + AIS Cross-Reference")
st.caption("Gulf of Mannar / Tuticorin anchorage, Sentinel-1, 18 Jan 2026")

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

config = load_config()
ais_positions = load_ais_positions()
overlay_png_path, overlay_bounds = load_overlay()

box = config["detection_sub_box"]
center_lat = (box["lat_min"] + box["lat_max"]) / 2
center_lon = (box["lon_min"] + box["lon_max"]) / 2

m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles="OpenStreetMap")

folium.raster_layers.ImageOverlay(
    image=overlay_png_path,
    bounds=overlay_bounds,
    opacity=0.85,
    name="SAR VV intensity",
).add_to(m)

visible_detections = [
    d for d in detections_live if show_non_vessel or d["visual_class"] not in NON_VESSEL_CLASSES
]

for d in visible_detections:
    color = VISUAL_CLASS_COLORS.get(d["visual_class"], "gray")
    if d["live_vessel_unknown"]:
        vessel_str = "matched, but vessel identity wasn't cached at this radius"
    elif d["live_match_status"] == "MATCHED":
        vessel_str = f"{d['matched_name']} (MMSI {d['matched_mmsi']}, {d['match_distance_m']:.0f}m)"
    else:
        vessel_str = "none"
    popup_html = (
        f"<b>Detection #{d['rank']}</b> ({d['visual_class']})<br>"
        f"contrast: {d['contrast_db']:.1f} dB<br>"
        f"area: {d['area_px']} px<br>"
        f"AIS match @ {radius_m}m: <b>{d['live_match_status']}</b><br>"
        f"matched vessel: {vessel_str}"
    )
    folium.CircleMarker(
        location=[d["lat"], d["lon"]],
        radius=7,
        color=color,
        weight=2,
        fill=True,
        fill_color=color,
        fill_opacity=0.9,
        popup=folium.Popup(popup_html, max_width=320),
        tooltip=f"#{d['rank']} {d['visual_class']}",
    ).add_to(m)

for ais in ais_positions:
    folium.CircleMarker(
        location=[ais["lat"], ais["lon"]],
        radius=5,
        color="blue",
        weight=1,
        fill=True,
        fill_color="blue",
        fill_opacity=0.7,
        popup=folium.Popup(
            f"<b>{ais['shipName']}</b><br>MMSI {ais['mmsi']}<br>flag {ais['flag']}<br>"
            f"type {ais['vesselType']}<br>hours present: {ais['hours']}",
            max_width=250,
        ),
        tooltip=ais["shipName"],
    ).add_to(m)

jupiter = next((a for a in ais_positions if str(a.get("mmsi")) == DMC_JUPITER_MMSI), None)
if jupiter:
    for d in detections_live:
        if d["visual_class"] == "CONFIRMED_VESSEL":
            folium.PolyLine(
                locations=[[d["lat"], d["lon"]], [jupiter["lat"], jupiter["lon"]]],
                color="green",
                weight=2,
                dash_array="6,6",
                tooltip=f"#{d['rank']} -> DMC JUPITER, {d['match_distance_m']:.0f}m",
            ).add_to(m)

folium.LayerControl().add_to(m)
st_folium(m, width=None, height=560, returned_objects=[])

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_results, tab_methodology, tab_limitations = st.tabs(["Results", "Methodology", "Limitations"])

with tab_results:
    st.subheader(f"Detections ({len(detections_live)} total, radius = {radius_m}m)")
    df = pd.DataFrame(detections_live).sort_values("rank")
    display_cols = [
        "rank", "visual_class", "lon", "lat", "contrast_db", "area_px", "shape_label",
        "live_match_status", "match_distance_m", "matched_name", "matched_mmsi",
        "matched_flag", "matched_type",
    ]
    df = df[[c for c in display_cols if c in df.columns]].rename(columns={"live_match_status": "match_status"})
    st.dataframe(df, width="stretch", hide_index=True)

with tab_methodology:
    cfar = config["detection_cfar"]
    land = config["land_mask"]
    dl = config["download"]
    shp = config["shape_filter"]
    st.markdown(f"""
### Pipeline

1. **Fetch** — Sentinel-1 GRD VV scene via Google Earth Engine (`COPERNICUS/S1_GRD`),
   acquired {config['scene']['acquisition_date']} ({config['scene']['orbit_direction']} pass),
   downloaded at {dl['scale_m_per_px']}m/px (actual: {dl['actual_resolution_m_per_px']}m/px,
   {dl['full_aoi_coverage_frac']*100:.1f}% AOI coverage).
2. **Land mask** — {land['source']}, clipped to the AOI in EPSG:4326 before reprojecting
   to the scene's native UTM zone (reprojecting the full global dataset first caused a
   numerical hang), then dilated by {land['coastal_buffer_px']}px
   ({land['coastal_buffer_m']}m) to catch near-shore clutter.
3. **Detection — CA-CFAR** (Cell-Averaging Constant False Alarm Rate): a per-pixel
   adaptive threshold, `local_mean + k * local_std`, computed from a training ring
   around each pixel over sea-only pixels.
   - guard band: {cfar['guard_px']}px
   - training ring: {cfar['training_px']}px
   - k: {cfar['k']}
   - morphological opening: {cfar['opening_iterations']} iteration(s) (speckle removal)
   - blob size window: {cfar['min_blob_px']}-{cfar['max_blob_px']}px
4. **Shape classification** — per-blob bounding-box aspect ratio and fill ratio:
   - `BLOCKY`: aspect ratio ≤ {shp['blocky_max_aspect_ratio']}
   - `SHIP-LIKE`: {shp['ship_like_aspect_ratio_range'][0]} < aspect ratio ≤ {shp['ship_like_aspect_ratio_range'][1]},
     fill ratio ≥ {shp['ship_like_min_fill_ratio']}
   - `LINEAR-STRUCTURE`: aspect ratio ≥ {shp['linear_structure_min_aspect_ratio']},
     fill ratio < {shp['linear_structure_max_fill_ratio']}
   - `AMBIGUOUS`: anything else
5. **AIS cross-reference** — GFW `4wings/report`, `public-global-presence:latest`, matched
   to each detection by nearest great-circle distance within a configurable radius
   (this app's slider; `match.py`'s default is {1500}m, since GFW's presence grid is
   ~1km per cell).

### Visual classification (Milestone 1 ground truth)

The `visual_class` field is not algorithmic — it's the outcome of manually inspecting a
crop around every one of the 17 CFAR detections (`results/MILESTONE1_FINDINGS.md`):

- **CONFIRMED_VESSEL** (#3, #4): a discrete, compact bright blob with no line/structure/edge
  nearby — the only two detections judged to plausibly be real ships from the SAR image alone.
- **LINEAR_FEATURE** (#5-12): fragments of one continuous bright line (pipeline, cable, or
  unmapped shoal edge), split into separate blobs by CFAR + morphological opening.
- **SPECKLE** (#13-17): no visible feature in the crop, indistinguishable from clutter.
- **UNEXPLAINED** (#1, #2): high contrast but no clean discrete target shape either.
""")

with tab_limitations:
    st.markdown(load_findings_md())
