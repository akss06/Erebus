"""
Milestone 1 step: detect ship-like bright blobs on the (land-masked) sea.

v2 detector: CA-CFAR (Cell-Averaging Constant False Alarm Rate) replaces the
v1 global percentile threshold. Rather than one fixed brightness cutoff for
the whole scene, each pixel is compared against the mean+std of its own
local sea-clutter neighborhood (a training ring, with a guard band excluded
so a target's own energy doesn't bias its local background estimate). This
adapts to spatially varying sea clutter instead of assuming one uniform
distribution across the whole image.

Pipeline: dB -> linear intensity -> CA-CFAR threshold per pixel ->
morphological opening (speckle removal) -> connected components ->
centroid + area per blob, filtered to ship-sized blobs.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage


def db_to_linear(db: np.ndarray) -> np.ndarray:
    """COPERNICUS/S1_GRD bands are calibrated backscatter in dB; convert to linear."""
    return 10 ** (db / 10.0)


def _box_sum(arr: np.ndarray, size: int) -> np.ndarray:
    """Sum over a size x size box centered on each pixel (out-of-bounds treated as 0)."""
    return ndimage.uniform_filter(arr, size=size, mode="constant", cval=0.0) * (size * size)


def _classify_shape(aspect_ratio: float, fill_ratio: float) -> str:
    """Rough shape classifier from a blob's bounding-box aspect ratio (long/short
    side) and fill ratio (blob pixels / bounding-box area).

    Ships: solid and moderately elongated (aspect 2-8, fill > 0.4).
    Linear structures (breakwaters/roads/causeways): thin and long (aspect
    >= 10, fill < 0.3).
    Blocky (buildings/rooftop clusters): close to square (aspect <= 2).
    Anything in between the defined bands is AMBIGUOUS rather than forced
    into one of the three - with area_px this small (a few pixels), shape
    stats are noisy and a forced label would overstate confidence.
    """
    if aspect_ratio <= 2.0:
        return "BLOCKY"
    if aspect_ratio >= 10.0 and fill_ratio < 0.3:
        return "LINEAR-STRUCTURE"
    if 2.0 < aspect_ratio <= 8.0 and fill_ratio >= 0.4:
        return "SHIP-LIKE"
    return "AMBIGUOUS"


def detect_blobs(
    intensity: np.ndarray,
    sea_mask: np.ndarray,
    guard_px: int,
    training_px: int,
    k: float,
    opening_iterations: int,
    min_blob_px: int,
    max_blob_px: int,
) -> list[dict]:
    """CA-CFAR detection. Returns one dict per surviving blob: row, col (pixel
    centroid, float), area_px, mean_intensity_db, max_intensity_db.

    guard_px: half-width of the guard band around the cell under test (CUT),
        excluded from the background estimate so target energy doesn't leak
        into its own threshold.
    training_px: width of the training ring beyond the guard band, used to
        estimate local mean/std. Both guard and training regions are square
        (box-shaped), not literal circles - the standard, cheap-to-compute
        form of 2D CA-CFAR via box filters.
    k: threshold = local_mean + k * local_std. Higher k -> fewer, stricter
        detections.

    Training-window statistics are computed over sea_mask pixels only, so
    nearby land/nodata doesn't contaminate the local background estimate.
    """
    inner_size = 2 * guard_px + 1
    outer_size = 2 * (guard_px + training_px) + 1

    mask_f = sea_mask.astype("float64")
    masked_intensity = intensity * mask_f
    masked_sq = (intensity**2) * mask_f

    outer_sum = _box_sum(masked_intensity, outer_size)
    inner_sum = _box_sum(masked_intensity, inner_size)
    outer_sumsq = _box_sum(masked_sq, outer_size)
    inner_sumsq = _box_sum(masked_sq, inner_size)
    outer_count = _box_sum(mask_f, outer_size)
    inner_count = _box_sum(mask_f, inner_size)

    train_sum = outer_sum - inner_sum
    train_sumsq = outer_sumsq - inner_sumsq
    train_count = outer_count - inner_count

    has_training_data = train_count > 0
    local_mean = np.divide(train_sum, train_count, out=np.zeros_like(train_sum), where=has_training_data)
    mean_sq = np.divide(train_sumsq, train_count, out=np.zeros_like(train_sumsq), where=has_training_data)
    local_var = np.clip(mean_sq - local_mean**2, 0.0, None)
    local_std = np.sqrt(local_var)

    cfar_threshold = local_mean + k * local_std
    bright = (intensity > cfar_threshold) & sea_mask & has_training_data

    if opening_iterations > 0:
        bright = ndimage.binary_opening(bright, iterations=opening_iterations)

    labels, n_labels = ndimage.label(bright)
    if n_labels == 0:
        return []

    detections = []
    for label_id, slc in enumerate(ndimage.find_objects(labels), start=1):
        if slc is None:
            continue
        blob_mask = labels[slc] == label_id
        area_px = int(blob_mask.sum())
        if area_px < min_blob_px or area_px > max_blob_px:
            continue

        rows, cols = np.nonzero(blob_mask)
        centroid_row = float(rows.mean() + slc[0].start)
        centroid_col = float(cols.mean() + slc[1].start)

        blob_intensity_db = 10 * np.log10(intensity[slc][blob_mask])
        mean_intensity_db = float(blob_intensity_db.mean())

        # Local CFAR background (linear) at this blob, in dB - and the contrast
        # between the blob's own brightness and that background. This is the
        # number that actually distinguishes a real target from clutter: a
        # genuine ship stands many dB above its *local* surroundings, not just
        # above some global scene average.
        local_bg_linear = float(local_mean[slc][blob_mask].mean())
        local_background_db = 10 * np.log10(local_bg_linear) if local_bg_linear > 0 else float("-inf")
        contrast_db = mean_intensity_db - local_background_db

        bbox_h = slc[0].stop - slc[0].start
        bbox_w = slc[1].stop - slc[1].start
        long_side, short_side = max(bbox_h, bbox_w), max(1, min(bbox_h, bbox_w))
        aspect_ratio = long_side / short_side
        fill_ratio = area_px / (bbox_h * bbox_w)
        shape_label = _classify_shape(aspect_ratio, fill_ratio)

        detections.append(
            {
                "row": centroid_row,
                "col": centroid_col,
                "area_px": area_px,
                "mean_intensity_db": mean_intensity_db,
                "max_intensity_db": float(blob_intensity_db.max()),
                "local_background_db": local_background_db,
                "contrast_db": contrast_db,
                "aspect_ratio": aspect_ratio,
                "fill_ratio": fill_ratio,
                "shape_label": shape_label,
            }
        )
    return detections


def pixel_to_xy(row: float, col: float, transform) -> tuple[float, float]:
    """Pixel centroid (row, col) -> (x, y) in the raster's own CRS, via its
    affine geotransform. NOT necessarily lon/lat - GEE GeoTIFFs commonly come
    back in a UTM CRS (meters); reproject to EPSG:4326 separately if needed."""
    x, y = transform * (col + 0.5, row + 0.5)
    return x, y
