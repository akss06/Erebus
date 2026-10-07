"""
ERÉBUS v3 detector: superpixel Generalized-Gamma CFAR with real water masking,
target/clutter discrimination, and optional VH cross-pol corroboration.

Each stage is grounded in the SAR false-positive-reduction literature (see the
Elicit review in docs / the README):

 - JRC Global Surface Water mask + 1 km shore buffer (GFW / Paolo et al. 2024):
   drop everything within 1 km of shore; coastlines and small reefs/islets are
   ambiguous. 30 m, observation-based. NOTE: JRC v1.4 is a historical
   water-OCCURRENCE record, not an acquisition-time map of exposed reefs/shoals;
   our probe found occurrence=99 over the Adam's Bridge reef chain, so JRC does
   NOT reliably exclude those reefs (see AUDIT_CHECKLIST §6.5). An explicit
   charted reef/infrastructure layer is the right tool and is still pending.
 - SLIC superpixels split into pure-clutter vs bright regions (Pappas 2018;
   Li M-D 2022): a robust-MAD outlier test flags bright superpixels (targets /
   residual land). The shape fit (below) is run on the whole sea with the top 1%
   trimmed; fitting on the clutter superpixels ONLY is an available experiment
   (audit/experiments/ablation.py), not the default.
 - Generalized-Gamma CFAR (Martin-de-Nicolas 2015; Li 2022): Sentinel-1 sea
   clutter is heavy-tailed. The threshold is local_clutter_mean * alpha, with
   alpha a GGD/Gamma quantile of the scale-free normalized clutter. P_fa is
   NOMINAL only: the top 1% is TRIMMED (not censoring-aware) before an ordinary
   MLE, so the achieved exceedance differs from the nominal 1e-5 (quantified in
   audit/experiments/calibration.py). Downstream contrast/TCR/shape gates and the
   confidence bar do the real discrimination.
 - Peak-to-clutter ratio (TCR) + Eigen-ellipse shape gates (Ao & Xu 2018;
   Bi 2013): a real vessel is a compact blob many dB above local clutter; faint
   speckle near reefs is not. Shape gates reject ragged blobs and long reef/wake
   lines ONLY -- not an aspect-ratio band, because a 15 m boat is 1-3 px at 10 m.
   Note `area_px` is a thresholded radar-response footprint, not hull area, and
   image pixel spacing (10 m) is not the physical spatial resolution (~20 m).
 - VH/VV cross-pol ratio (standard dual-pol discriminator): an elevated VH return
   is CONSISTENT WITH (not proof of) a hard/metal target. The default background
   ratio is a TILE-WIDE sea median (a genuinely local comparison is an
   experiment, §7.1). Used as a corroborating signal only; a low VH never demotes.

The frozen Round-1 CA-CFAR detector (detect.py) and the GFW validation
(validate_vs_gfw.py) are deliberately left untouched so the reported validation
numbers stay honest.

run_detector_v3 returns (detections, diagnostics); diagnostics records the fit,
mask provenance, fallback reasons and a degraded/insufficient-data status so a
failed or partial analysis is never presented as a normal zero-detection result.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import ee
import numpy as np
import rasterio
import requests
from rasterio.transform import array_bounds, Affine
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
PFA = 1e-5                 # NOMINAL target false-alarm rate for the GGD threshold
SLIC_SEGMENTS = 700        # superpixels per tile (~1-2k px each on a 9 km tile)
SLIC_COMPACTNESS = 0.08    # low: single-channel intensity, favour intensity over shape
MIN_BLOB_PX = 3            # NOTE: binary_opening runs first, so thin 3px shapes do not survive (§2)
MAX_BLOB_PX = 80
TCR_MIN_DB = 6.0           # peak above local clutter; the bright-point-vs-speckle gate
SOLIDITY_MIN = 0.5         # reject ragged / scattered speckle clusters
ECC_MAX = 0.985            # reject near-perfect lines (reef edges, wakes)
GUARD_PX = 5               # guard band around the cell under test (local-clutter ring)
TRAIN_PX = 20              # training ring width for the local-clutter mean
MIN_TRAIN_PX = 12          # min clutter pixels in the ring to trust a local background estimate
SHORE_BUFFER_M = 1000.0    # GFW's 1 km shore exclusion
JRC_OCC_THRESH = 50.0      # % water occurrence at/above which a pixel is water
VH_RATIO_MARGIN_DB = 3.0   # VH/VV elevation over local sea ratio that corroborates a target

DETECTOR_VERSION = "v3.1-audit"


def config_hash() -> str:
    """Short hash of the tunable constants, recorded in diagnostics for provenance."""
    keys = [PFA, SLIC_SEGMENTS, SLIC_COMPACTNESS, MIN_BLOB_PX, MAX_BLOB_PX, TCR_MIN_DB,
            SOLIDITY_MIN, ECC_MAX, GUARD_PX, TRAIN_PX, MIN_TRAIN_PX, SHORE_BUFFER_M,
            JRC_OCC_THRESH, VH_RATIO_MARGIN_DB]
    return hashlib.sha256(repr(keys).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- clutter model

def _fit_shape_alpha(norm: np.ndarray, pfa: float) -> tuple[float, dict]:
    """Generalized-Gamma CFAR multiplier for a NOMINAL target P_fa.

    `norm` is clutter intensity divided by its local mean (scale-free). The top 1%
    is TRIMMED (not censoring-aware -- ordinary MLE on the remainder) to keep sparse
    targets out of the fit, then the fitted distribution is extrapolated to the
    (1-pfa) quantile. Because the tail is trimmed, achieved P_fa != nominal; this is
    quantified in audit/experiments/calibration.py. Returns (alpha, diag). Falls back
    to Gamma then the empirical quantile. inf = too little data to model."""
    x = norm[np.isfinite(norm) & (norm > 0)]
    diag = {"n_samples": int(x.size), "trim_q": 0.99, "pfa_nominal": pfa,
            "dist": None, "alpha": None, "params": None}
    if x.size < 300:
        diag["dist"] = "insufficient"
        return float("inf"), diag
    emp = float(np.quantile(x, 1 - pfa))
    cens = x[x <= np.quantile(x, 0.99)]  # trim the brightest 1% (possible targets)
    if cens.size > 8000:
        cens = np.random.default_rng(0).choice(cens, 8000, replace=False)
    for dist, name in ((gengamma, "gengamma"), (gamma, "gamma")):
        try:
            params = dist.fit(cens, floc=0)
            alpha = dist.ppf(1 - pfa, *params[:-2], loc=0, scale=params[-1])
            if np.isfinite(alpha) and alpha > 1:
                diag.update(dist=name, alpha=float(alpha), params=[float(p) for p in params])
                return float(alpha), diag
        except Exception:
            continue
    diag.update(dist="empirical", alpha=emp)
    return emp, diag


def _clutter_superpixels(db: np.ndarray, sea: np.ndarray) -> np.ndarray:
    """SLIC-segment the sea and keep every superpixel EXCEPT bright outliers
    (targets / residual land). Robust-MAD outlier test, not an Otsu half-split:
    on rough sea almost all superpixels are clutter, which is correct -- only a
    genuinely anomalous-bright superpixel is excluded."""
    vals = db[sea & np.isfinite(db)]
    if vals.size == 0:
        return np.zeros_like(sea)
    lo, hi = np.percentile(vals, [2, 98])
    norm = np.where(sea & np.isfinite(db), np.clip((db - lo) / max(hi - lo, 1e-6), 0, 1), 0.0)
    seg = slic(norm, n_segments=SLIC_SEGMENTS, compactness=SLIC_COMPACTNESS,
               channel_axis=None, mask=sea, start_label=1)
    ids = np.unique(seg[seg > 0])
    if ids.size < 3:
        return sea.copy()
    means = ndimage.mean(np.where(np.isfinite(db), db, lo), labels=seg, index=ids)
    med = float(np.median(means))
    mad = float(np.median(np.abs(means - med))) * 1.4826 or 1.0
    clutter = np.isin(seg, ids[means <= med + 3 * mad]) & sea
    if clutter.sum() < 0.5 * sea.sum():  # degenerate -> use all sea
        return sea.copy()
    return clutter


def _local_clutter_mean(intensity: np.ndarray, clutter: np.ndarray,
                        min_count: int = MIN_TRAIN_PX) -> tuple[np.ndarray, np.ndarray]:
    """Local mean intensity over clutter pixels in a guard+training ring around
    each pixel (CA-CFAR topology, background = clutter superpixels only).

    Numerically safe: only finite clutter pixels contribute (NaN*0 would otherwise
    propagate a NaN across the whole box-filter neighbourhood, §8.1). Returns
    (local_mean_linear, enough_support) where local_mean is NaN and enough_support
    is False where fewer than `min_count` clutter pixels fall in the ring (§1.4)."""
    valid = clutter & np.isfinite(intensity)
    mi = np.where(valid, intensity, 0.0)
    cf = valid.astype("float64")
    inner = 2 * GUARD_PX + 1
    outer = 2 * (GUARD_PX + TRAIN_PX) + 1
    num = detect._box_sum(mi, outer) - detect._box_sum(mi, inner)
    cnt = detect._box_sum(cf, outer) - detect._box_sum(cf, inner)
    enough = cnt >= min_count
    lm = np.divide(num, cnt, out=np.full_like(num, np.nan), where=enough)
    return lm, enough


# ---------------------------------------------------------------- detection

def detect_blobs_v3(vv_db: np.ndarray, sea: np.ndarray, vh_db: np.ndarray | None = None) -> tuple[list[dict], dict]:
    """Superpixel GG-CFAR detection on one tile. Returns (detections, diagnostics).

    diagnostics records the fit, the fraction of sea pixels that fell back to the
    global clutter median for lack of local training support, and a status so a
    degraded/insufficient analysis is distinguishable from a true zero-detection."""
    intensity = detect.db_to_linear(vv_db)
    clutter = _clutter_superpixels(vv_db, sea)

    local_bg, enough = _local_clutter_mean(intensity, clutter)        # linear
    finite_clutter = intensity[clutter & np.isfinite(intensity)]
    global_bg = float(np.median(finite_clutter)) if finite_clutter.size else 1.0
    if not np.isfinite(global_bg) or global_bg <= 0:
        global_bg = 1.0
    use_local = np.isfinite(local_bg) & (local_bg > 0) & enough
    lm = np.where(use_local, local_bg, global_bg)

    sea_px = int(sea.sum())
    fallback_frac = float((sea & ~use_local).sum() / sea_px) if sea_px else 1.0

    norm = intensity[sea] / lm[sea]
    alpha, fit = _fit_shape_alpha(norm, PFA)
    diag = {"detector_version": DETECTOR_VERSION, "config_hash": config_hash(),
            "fit": fit, "sea_px": sea_px, "local_bg_fallback_frac": round(fallback_frac, 4),
            "status": "ok", "notes": []}
    if not np.isfinite(alpha):
        diag["status"] = "insufficient_data"
        diag["notes"].append(f"fit insufficient: only {fit['n_samples']} usable sea pixels")
        return [], diag
    if fallback_frac > 0.5:
        diag["status"] = "degraded"
        diag["notes"].append(f"{fallback_frac:.0%} of sea lacked local training support (<{MIN_TRAIN_PX}px); used global median")

    bright = (intensity > lm * alpha) & sea & np.isfinite(intensity)
    bright = ndimage.binary_opening(bright, iterations=1)
    labels = sk_label(bright)
    if labels.max() == 0:
        return [], diag

    # tile-wide median sea VH/VV ratio (dB) for corroboration (§7.1: this is
    # tile-wide, not local; a local version is an experiment).
    tile_sea_ratio_db = None
    if vh_db is not None:
        ok = clutter & np.isfinite(vh_db) & np.isfinite(vv_db)
        if ok.sum() > 100:
            tile_sea_ratio_db = float(np.median(vh_db[ok] - vv_db[ok]))

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
            "local_bg_from_global_median": not bool(use_local[rr, cc]),
        }

        if vh_db is not None and tile_sea_ratio_db is not None:
            vh_blob = vh_db[rows, cols]
            vv_blob = vv_db[rows, cols]
            ok = np.isfinite(vh_blob) & np.isfinite(vv_blob)
            if ok.any():
                ratio_db = float(np.mean(vh_blob[ok] - vv_blob[ok]))
                det["vh_vv_db"] = round(ratio_db, 1)
                det["vh_corroborated"] = bool(ratio_db >= tile_sea_ratio_db + VH_RATIO_MARGIN_DB)

        dets.append(det)
    return dets, diag


# ---------------------------------------------------------------- water mask

def _pixel_metres(transform: Affine, crs, lat: float) -> tuple[float, float]:
    """(y, x) pixel spacing in metres. Uses the projected transform directly for a
    metric CRS; converts degrees->metres at `lat` for a geographic CRS (§6.3)."""
    if crs is not None and crs.is_projected:
        return abs(transform.e), abs(transform.a)
    import math
    return abs(transform.e) * 110574.0, abs(transform.a) * 111320.0 * math.cos(math.radians(lat))


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


def build_sea_mask(vv_tif: Path) -> tuple[np.ndarray, np.ndarray, object, object, dict]:
    """Sea mask for a tile: Natural Earth land UNION JRC non-water, then a 1 km
    shore buffer. The land/water mask and the shore-distance transform are built on
    a grid PADDED by the shore buffer so land just outside the tile still pushes the
    shore distance inward (§6.2); the result is cropped back to the tile.

    Returns (vv_db, sea_mask, transform, crs, mask_info). mask_info records JRC
    availability/fallback and the excluded-coverage accounting (§6.1, §6.4)."""
    with rasterio.open(vv_tif) as src:
        db = src.read(1).astype("float64")
        transform, nodata, crs = src.transform, src.nodata, src.crs
    H, W = db.shape
    valid = np.isfinite(db) if nodata is None else (db != nodata) & np.isfinite(db)

    bounds = array_bounds(H, W, transform)
    lat_mid = transform_bounds(crs, "EPSG:4326", *bounds)[1::2]
    lat_mid = (lat_mid[0] + lat_mid[1]) / 2
    py_m, px_m = _pixel_metres(transform, crs, lat_mid)
    pad = int(np.ceil(SHORE_BUFFER_M / min(py_m, px_m))) + 2

    # padded grid: shift origin up/left by `pad` pixels, grow shape by 2*pad
    pt = transform * Affine.translation(-pad, -pad)
    pshape = (H + 2 * pad, W + 2 * pad)
    pbounds = array_bounds(pshape[0], pshape[1], pt)

    ne_sea_p = land_mask.get_sea_mask(bounds=pbounds, transform=pt, out_shape=pshape,
                                      crs=crs, cache_dir=DATA)
    pbounds_ll = transform_bounds(crs, "EPSG:4326", *pbounds)
    cache = vv_tif.with_name(vv_tif.stem + "_jrc.tif")
    mask_info = {"jrc_used": False, "jrc_fallback_reason": None, "shore_buffer_m": SHORE_BUFFER_M,
                 "pad_px": pad, "pixel_m": [round(py_m, 2), round(px_m, 2)],
                 "crs": str(crs), "metric_crs": bool(crs is not None and crs.is_projected)}
    try:
        water_p, jrc_has_p = _jrc_water_on_grid(pbounds_ll, pt, pshape, crs, cache)
        mask_info["jrc_used"] = True
    except Exception as e:
        mask_info["jrc_fallback_reason"] = f"{type(e).__name__}: {e}"
        print(f"  [jrc] skipped ({type(e).__name__}): {e}", flush=True)
        water_p = np.zeros(pshape, bool)
        jrc_has_p = np.zeros(pshape, bool)

    land_p = (~ne_sea_p) | (jrc_has_p & ~water_p)

    # shore distance on the padded land, with true pixel spacing, then crop
    if land_p.any() and (~land_p).any():
        dist_m_p = ndimage.distance_transform_edt(~land_p, sampling=(py_m, px_m))
    else:
        dist_m_p = np.full(pshape, np.inf)
    dist_m = dist_m_p[pad:pad + H, pad:pad + W]
    land = land_p[pad:pad + H, pad:pad + W]

    sea_no_buffer = (~land) & valid
    sea = sea_no_buffer & (dist_m >= SHORE_BUFFER_M)

    excl_px = int((sea_no_buffer & ~sea).sum())
    px_area_km2 = (py_m * px_m) / 1e6
    mask_info.update({
        "valid_px": int(valid.sum()),
        "sea_px": int(sea.sum()),
        "excluded_by_shore_buffer_px": excl_px,
        "excluded_by_shore_buffer_km2": round(excl_px * px_area_km2, 3),
        "sea_km2": round(int(sea.sum()) * px_area_km2, 3),
    })
    return db, sea, transform, crs, mask_info


def run_detector_v3(vv_tif: Path, vh_tif: Path | None = None) -> tuple[list[dict], dict]:
    """Full v3 pipeline on one tile: mask -> GG-CFAR -> gates -> lon/lat.
    Returns (detections, diagnostics). VH is corroboration only and is optional;
    if VH cannot be confirmed co-registered with VV it is skipped with a reason."""
    db, sea, transform, crs, mask_info = build_sea_mask(vv_tif)
    diag = {"mask": mask_info, "vh": {"used": False, "reason": "not provided"}}
    if sea.mean() < 0.02:  # shore buffer / land ate the tile -> nothing to detect
        diag["status"] = "no_sea"
        diag["notes"] = ["<2% of tile is open sea after masking"]
        return [], diag

    vh_db = None
    if vh_tif and Path(vh_tif).exists():
        try:
            with rasterio.open(vh_tif) as s:
                vh = s.read(1).astype("float64")
                vh_tf, vh_crs = s.transform, s.crs
            same_grid = (vh.shape == db.shape and vh_crs == crs
                         and all(abs(a - b) < 1e-6 for a, b in zip(tuple(vh_tf)[:6], tuple(transform)[:6])))
            if same_grid:
                vh_db = vh
                diag["vh"] = {"used": True, "reason": "co-registered with VV"}
            else:
                diag["vh"] = {"used": False, "reason": "VH not co-registered with VV (shape/CRS/transform mismatch)"}
        except Exception as e:
            diag["vh"] = {"used": False, "reason": f"VH read failed: {type(e).__name__}"}

    dets, det_diag = detect_blobs_v3(db, sea, vh_db)
    diag.update(det_diag)
    if not dets:
        return [], diag

    xs = [detect.pixel_to_xy(d["row"], d["col"], transform)[0] for d in dets]
    ys = [detect.pixel_to_xy(d["row"], d["col"], transform)[1] for d in dets]
    lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
    for d, lon, lat in zip(dets, lons, lats):
        d["lon"], d["lat"] = lon, lat
    return dets, diag
