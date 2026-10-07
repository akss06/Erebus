"""Canonical SAR radar-crop renderer.

One fixed dB window for every crop (Run Analysis, Tuticorin, Gulf, recent). A
per-crop percentile stretch rescales each crop on its own min/max, so a faint
smudge ends up looking as bright as a strong ship and crops are not comparable
across detections or datasets. The fixed [-23, +3] dB window keeps brightness
physically meaningful and comparable everywhere.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw

CROP_DB_LO = -23.0   # fixed brightness floor (dB)
CROP_DB_HI = 3.0     # fixed brightness ceiling (dB)
CROP_HALF_PX = 60    # 600 m each side at 10 m/px
CROP_SCALE = 4       # upscale so the crop is readable in the panel
MARKER_R = 22        # radius of the red locator ring, in upscaled px


def render_db_crop(db: np.ndarray, row: float, col: float,
                   half_px: int = CROP_HALF_PX, scale: int = CROP_SCALE,
                   mark: bool = True) -> Image.Image | None:
    """Fixed-window grayscale crop around (row, col); None if the window is all no-data."""
    r, c = int(round(row)), int(round(col))
    r0, r1 = max(0, r - half_px), min(db.shape[0], r + half_px)
    c0, c1 = max(0, c - half_px), min(db.shape[1], c + half_px)
    crop = db[r0:r1, c0:c1]
    if crop.size == 0 or not np.isfinite(crop).any():
        return None
    norm = np.clip((np.nan_to_num(crop, nan=CROP_DB_LO) - CROP_DB_LO) / (CROP_DB_HI - CROP_DB_LO), 0, 1)
    img = Image.fromarray((norm * 255).astype("uint8"), mode="L")
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST).convert("RGB")
    if mark:
        cx, cy = (c - c0 + 0.5) * scale, (r - r0 + 0.5) * scale
        ImageDraw.Draw(img).ellipse([cx - MARKER_R, cy - MARKER_R, cx + MARKER_R, cy + MARKER_R],
                                    outline=(255, 60, 60), width=2)
    return img


def save_crop(tif: Path, row: float, col: float, out: Path, band: int = 1, **kw) -> bool:
    """Render a fixed-window crop from a GeoTIFF's dB band to `out`. False if nothing to show."""
    with rasterio.open(tif) as src:
        db = src.read(band).astype("float64")
    img = render_db_crop(db, row, col, **kw)
    if img is None:
        return False
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return True
