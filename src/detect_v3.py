"""
ERÉBUS v3 detector: superpixel Generalized-Gamma CFAR with real water masking,
target/clutter discrimination, and optional VH cross-pol corroboration.

Each stage is grounded in the SAR false-positive-reduction literature (see the
Elicit review in docs / the README):

 - JRC Global Surface Water mask + 1 km shore buffer (GFW / Paolo et al. 2024):
   drop everything within 1 km of shore; coastlines and small reefs/islets are
   ambiguous. 30 m, observation-based, so it catches the Gulf of Mannar reefs
   that the Natural Earth 10 m coastline misses.
 - SLIC superpixels split into pure-clutter vs bright regions (Pappas 2018;
   Li M-D 2022): estimate the clutter model from pure-clutter superpixels only,
   so target and land pixels do not poison the background statistic.
 - Generalized-Gamma CFAR (Martin-de-Nicolas 2015; Li 2022): Sentinel-1 sea
   clutter is heavy-tailed; a Gaussian mean+k*std threshold over-detects speckle
   spikes. A GGD quantile for a target P_fa controls false alarms properly.
 - Peak-to-clutter ratio (TCR) + Eigen-ellipse shape gates (Ao & Xu 2018;
   Bi 2013): a real vessel is a compact blob many dB above local clutter; faint
   speckle near reefs is not. Shape gates reject ragged blobs and long reef/wake
   lines ONLY -- not an aspect-ratio band, because a 15 m boat is 1-3 px at 10 m
   and looks like a compact point, not an elongated hull.
 - VH/VV cross-pol ratio (standard dual-pol discriminator): metal vessels
   depolarise and spike VH; wind/wave clutter does not. Used as a CORROBORATING
   confidence signal only -- a low VH does not demote a candidate, because the
   small wooden/fibreglass trawlers we care about have weak VH too.

The frozen Round-1 CA-CFAR detector (detect.py) and the GFW validation
(validate_vs_gfw.py) are deliberately left untouched so the reported validation
numbers stay honest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import ee
import numpy as np
import rasterio
import requests
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, reproject, transform as warp_transform, transform_bounds
from scipy import ndimage
from scipy.stats import gamma, gengamma
from skimage.measure import label as sk_label, regionprops
from skimage.segmentation import slic

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect  # noqa: E402  (db_to_linear, _box_sum, pixel_to_xy)
import land_mask  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# --- detector parameters ---
PFA = 1e-5                 # target false-alarm rate for the GGD threshold
SLIC_SEGMENTS = 700        # superpixels per tile (~1-2k px each on a 9 km tile)
SLIC_COMPACTNESS = 0.08    # low: single-channel intensity, favour intensity over shape
MIN_BLOB_PX = 3
MAX_BLOB_PX = 80
TCR_MIN_DB = 6.0           # peak above local clutter; the bright-point-vs-speckle gate
SOLIDITY_MIN = 0.5         # reject ragged / scattered speckle clusters
ECC_MAX = 0.985            # reject near-perfect lines (reef edges, wakes)
GUARD_PX = 5               # guard band around the cell under test (local-clutter ring)
TRAIN_PX = 20              # training ring width for the local-clutter mean
SHORE_BUFFER_M = 1000.0    # GFW's 1 km shore exclusion
JRC_OCC_THRESH = 50.0      # % water occurrence at/above which a pixel is water
VH_RATIO_MARGIN_DB = 3.0   # VH/VV elevation over local sea ratio that corroborates a target


# ---------------------------------------------------------------- clutter model

def _fit_shape_alpha(norm: np.ndarray, pfa: float) -> float:
    """Generalized-Gamma CFAR multiplier for a target P_fa.

    `norm` is clutter intensity divided by its local mean (scale-free, so one
    fit is valid across the whole tile). The top 1% is censored to keep sparse
    targets out of the clutter fit. Returns alpha such that a pixel exceeding
    `local_mean * alpha` has clutter probability P_fa. Falls back to Gamma, then
    to the empirical quantile, if the MLE fails. inf = too little data to model."""
    x = norm[np.isfinite(norm) & (norm > 0)]
    if x.size < 300:
        return float("inf")
    emp = float(np.quantile(x, 1 - pfa))
    cens = x[x <= np.quantile(x, 0.99)]  # censor the brightest 1% (possible targets)
    if cens.size > 8000:
        cens = np.random.default_rng(0).choice(cens, 8000, replace=False)
    for dist in (gengamma, gamma):
        try:
            params = dist.fit(cens, floc=0)
            alpha = dist.ppf(1 - pfa, *params[:-2], loc=0, scale=params[-1])
            if np.isfinite(alpha) and alpha > 1:
                return float(alpha)
        except Exception:
            continue
    return emp


def _clutter_superpixels(db: np.ndarray, sea: np.ndarray) -> np.ndarray:
    """SLIC-segment the sea and keep every superpixel EXCEPT bright outliers
    (targets / residual land). Robust-MAD outlier test, not an Otsu half-split:
    on rough sea almost all superpixels are clutter, which is correct -- only a
    genuinely anomalous-bright superpixel is excluded."""
    vals = db[sea]
    if vals.size == 0:
        return np.zeros_like(sea)
    lo, hi = np.percentile(vals, [2, 98])
    norm = np.where(sea, np.clip((db - lo) / max(hi - lo, 1e-6), 0, 1), 0.0)
    seg = slic(norm, n_segments=SLIC_SEGMENTS, compactness=SLIC_COMPACTNESS,
               channel_axis=None, mask=sea, start_label=1)
    ids = np.unique(seg[seg > 0])
    if ids.size < 3:
        return sea.copy()
    means = ndimage.mean(db, labels=seg, index=ids)
    med = float(np.median(means))
    mad = float(np.median(np.abs(means - med))) * 1.4826 or 1.0
    clutter = np.isin(seg, ids[means <= med + 3 * mad]) & sea
    if clutter.sum() < 0.5 * sea.sum():  # degenerate -> use all sea
        return sea.copy()
    return clutter


def _local_clutter_mean(intensity: np.ndarray, clutter: np.ndarray) -> np.ndarray:
    """Local mean intensity over clutter pixels in a guard+training ring around
    each pixel (CA-CFAR topology, but background = clutter superpixels only).
    Returns linear intensity; NaN where the ring holds no clutter."""
    cf = clutter.astype("float64")
    mi = intensity * cf
    inner = 2 * GUARD_PX + 1
    outer = 2 * (GUARD_PX + TRAIN_PX) + 1
    num = detect._box_sum(mi, outer) - detect._box_sum(mi, inner)
    cnt = detect._box_sum(cf, outer) - detect._box_sum(cf, inner)
    return np.divide(num, cnt, out=np.full_like(num, np.nan), where=cnt > 0)


# ---------------------------------------------------------------- detection

def detect_blobs_v3(vv_db: np.ndarray, sea: np.ndarray, vh_db: np.ndarray | None = None) -> list[dict]:
    """Superpixel GG-CFAR detection on one tile. Returns one dict per surviving
    blob with row/col centroid and the features the confidence scorer uses
    (contrast_db, area_px) plus the new discriminators (tcr_db, shape, VH)."""
    intensity = detect.db_to_linear(vv_db)
    clutter = _clutter_superpixels(vv_db, sea)

    # Locally adaptive background: mean intensity of clutter pixels in a
    # guard+training ring, filled with the global clutter median where the ring
    # held no clutter. Rough sea raises the local background, so the threshold
    # rises with it (true CFAR adaptivity).
    local_bg = _local_clutter_mean(intensity, clutter)        # linear
    global_bg = float(np.nanmedian(intensity[clutter])) or 1.0
    lm = np.where(np.isfinite(local_bg) & (local_bg > 0), local_bg, global_bg)

    norm = intensity[sea] / lm[sea]
    alpha = _fit_shape_alpha(norm, PFA)
    if not np.isfinite(alpha):
        return []

    bright = (intensity > lm * alpha) & sea
    bright = ndimage.binary_opening(bright, iterations=1)
    labels = sk_label(bright)
    if labels.max() == 0:
        return []

    # local sea VH/VV ratio (dB) for corroboration, if VH is available
    sea_ratio_db = None
    if vh_db is not None:
        ok = clutter & np.isfinite(vh_db) & np.isfinite(vv_db)
        if ok.sum() > 100:
            sea_ratio_db = float(np.median(vh_db[ok] - vv_db[ok]))

    dets: list[dict] = []
    for rp in regionprops(labels):
        area = int(rp.area)
        if area < MIN_BLOB_PX or area > MAX_BLOB_PX:
            continue
        rows = rp.coords[:, 0]
        cols = rp.coords[:, 1]
        r, c = rp.centroid
        rr = min(max(int(round(r)), 0), intensity.shape[0] - 1)
        cc = min(max(int(round(c)), 0), intensity.shape[1] - 1)

        blob_db = vv_db[rows, cols]
        blob_db = blob_db[np.isfinite(blob_db)]
        if blob_db.size == 0:
            continue
        mean_db = float(blob_db.mean())
        max_db = float(blob_db.max())

        bg_db = 10 * np.log10(lm[rr, cc])
        contrast_db = mean_db - bg_db
        tcr_db = max_db - bg_db  # peak-to-clutter ratio

        # Eigen-ellipse shape (second central moments -> major/minor axis)
        ecc = float(rp.eccentricity)
        solidity = float(rp.solidity)
        major = float(rp.axis_major_length)
        minor = float(rp.axis_minor_length)
        aspect = major / minor if minor > 0 else 1.0
        bh = rp.bbox[2] - rp.bbox[0]
        bw = rp.bbox[3] - rp.bbox[1]
        fill = area / (bh * bw) if bh * bw else 1.0

        # --- gates ---
        if tcr_db < TCR_MIN_DB:                               # faint speckle near bright features
            continue
        if area >= 6 and solidity < SOLIDITY_MIN:            # ragged / scattered cluster
            continue
        if area >= 12 and ecc > ECC_MAX and fill < 0.35:     # long reef edge / wake line
            continue

        det = {
            "row": float(r), "col": float(c), "area_px": area,
            "mean_intensity_db": mean_db, "max_intensity_db": max_db,
            "local_background_db": bg_db, "contrast_db": contrast_db,
            "tcr_db": round(tcr_db, 1),
            "eccentricity": round(ecc, 3), "solidity": round(solidity, 3),
            "aspect_ratio": round(aspect, 2), "fill_ratio": round(fill, 2),
        }

        if vh_db is not None and sea_ratio_db is not None:
            vh_blob = vh_db[rows, cols]
            vv_blob = vv_db[rows, cols]
            ok = np.isfinite(vh_blob) & np.isfinite(vv_blob)
            if ok.any():
                ratio_db = float(np.mean(vh_blob[ok] - vv_blob[ok]))
                det["vh_vv_db"] = round(ratio_db, 1)
                det["vh_corroborated"] = bool(ratio_db >= sea_ratio_db + VH_RATIO_MARGIN_DB)

        dets.append(det)
    return dets


# ---------------------------------------------------------------- water mask

def _jrc_water_on_grid(bounds_lonlat, dst_transform, dst_shape, dst_crs, cache_tif: Path):
    """Fetch JRC Global Surface Water 'occurrence' for the AOI (cached) and
    reproject it onto the tile grid. Returns (is_water, jrc_has_data)."""
    if not cache_tif.exists():
        lon0, lat0, lon1, lat1 = bounds_lonlat
        aoi = ee.Geometry.Rectangle([lon0, lat0, lon1, lat1])
        img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").clip(aoi)
        url = img.getDownloadURL({"scale": 30, "region": aoi, "format": "GEO_TIFF"})
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        cache_tif.parent.mkdir(parents=True, exist_ok=True)
        cache_tif.write_bytes(resp.content)

    with rasterio.open(cache_tif) as src:
        occ = src.read(1).astype("float32")
        has = src.read_masks(1) > 0
        src_crs, src_transform = src.crs, src.transform
    occ = np.where(has, occ, np.nan)

    dst_occ = np.full(dst_shape, np.nan, dtype="float32")
    reproject(occ, dst_occ, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=dst_crs,
              resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
    jrc_has_data = np.isfinite(dst_occ)
    is_water = jrc_has_data & (dst_occ >= JRC_OCC_THRESH)
    return is_water, jrc_has_data


def build_sea_mask(vv_tif: Path) -> tuple[np.ndarray, np.ndarray, object, object]:
    """Sea mask for a tile: Natural Earth land UNION JRC non-water, then a 1 km
    shore buffer. Returns (vv_db, sea_mask, transform, crs)."""
    with rasterio.open(vv_tif) as src:
        db = src.read(1).astype("float64")
        transform, nodata, crs = src.transform, src.nodata, src.crs
    shape = db.shape
    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)

    bounds = array_bounds(shape[0], shape[1], transform)
    ne_sea = land_mask.get_sea_mask(bounds=bounds, transform=transform,
                                    out_shape=shape, crs=crs, cache_dir=DATA)

    bounds_ll = transform_bounds(crs, "EPSG:4326", *bounds)
    cache = vv_tif.with_name(vv_tif.stem + "_jrc.tif")
    try:
        water, jrc_has = _jrc_water_on_grid(bounds_ll, transform, shape, crs, cache)
    except Exception as e:
        print(f"  [jrc] skipped ({type(e).__name__}): {e}", flush=True)
        water = np.zeros(shape, bool)
        jrc_has = np.zeros(shape, bool)

    land = (~ne_sea) | (jrc_has & ~water)
    sea = (~land) & valid

    if sea.any() and land.any():
        dist_px = ndimage.distance_transform_edt(~land)
        sea = sea & (dist_px * abs(transform.a) >= SHORE_BUFFER_M)

    return db, sea, transform, crs


def run_detector_v3(vv_tif: Path, vh_tif: Path | None = None) -> list[dict]:
    """Full v3 pipeline on one tile: mask -> GG-CFAR -> gates -> lon/lat.
    VH is used for corroboration only and is optional."""
    db, sea, transform, crs = build_sea_mask(vv_tif)
    if sea.mean() < 0.02:  # shore buffer ate the tile -> nothing to detect
        return []

    vh_db = None
    if vh_tif and Path(vh_tif).exists():
        try:
            with rasterio.open(vh_tif) as s:
                vh = s.read(1).astype("float64")
            if vh.shape == db.shape:
                vh_db = vh
        except Exception:
            vh_db = None

    dets = detect_blobs_v3(db, sea, vh_db)
    if not dets:
        return []

    xs = [detect.pixel_to_xy(d["row"], d["col"], transform)[0] for d in dets]
    ys = [detect.pixel_to_xy(d["row"], d["col"], transform)[1] for d in dets]
    lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
    for d, lon, lat in zip(dets, lons, lats):
        d["lon"], d["lat"] = lon, lat
    return dets
