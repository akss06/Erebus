"""
EXPERIMENT (AUDIT §2): what does the pre-labelling binary_opening cost small targets?

Injects synthetic radar-response footprints (compact / block / thin-line / L /
adjacent-pair / weak) into REAL sea clutter from a cached tile, at the detector's
own local threshold, and measures how many survive to a post-gate detection WITH the
default `binary_opening(iterations=1)` vs WITHOUT it. This isolates the morphology
step; it is synthetic target recovery, NOT field vessel recall.

Run:  python audit/experiments/morphology.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage
from skimage.measure import label as sk_label, regionprops

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
import detect  # noqa: E402
import detect_v3 as D  # noqa: E402

TILE = ROOT / "data" / "recent" / "2026-06-30_1000.tif"
N_SITES = 150
FACTOR_STRONG = 4.0   # peak intensity = local_mean * alpha * factor
FACTOR_WEAK = 1.4

SHAPES = {
    "single_1px":      [(0, 0)],
    "line_1x3":        [(0, 0), (0, 1), (0, 2)],
    "line_1x4":        [(0, 0), (0, 1), (0, 2), (0, 3)],
    "L_3px":           [(0, 0), (1, 0), (1, 1)],
    "block_2x2":       [(0, 0), (0, 1), (1, 0), (1, 1)],
    "block_3x3":       [(r, c) for r in range(3) for c in range(3)],
    "adjacent_2x2_pair": [(0, 0), (0, 1), (1, 0), (1, 1), (0, 3), (0, 4), (1, 3), (1, 4)],
}


def passes_gates(rp) -> bool:
    area = int(rp.area)
    if area < D.MIN_BLOB_PX or area > D.MAX_BLOB_PX:
        return False
    if area >= 6 and float(rp.solidity) < D.SOLIDITY_MIN:
        return False
    if area >= 12 and float(rp.eccentricity) > D.ECC_MAX and (area / (
            (rp.bbox[2] - rp.bbox[0]) * (rp.bbox[3] - rp.bbox[1]) or 1)) < 0.35:
        return False
    return True


def recovered(bright, footprint_mask, opening: bool) -> bool:
    b = ndimage.binary_opening(bright, iterations=1) if opening else bright
    labels = sk_label(b)
    if labels.max() == 0:
        return False
    for rp in regionprops(labels):
        coords = rp.coords
        hit = footprint_mask[coords[:, 0], coords[:, 1]].any()
        if hit and passes_gates(rp):
            return True
    return False


def main():
    db, sea, _, _, _ = D.build_sea_mask(TILE)
    intensity = detect.db_to_linear(db)
    clutter = D._clutter_superpixels(db, sea)
    lm, enough = D._local_clutter_mean(intensity, clutter)
    gbg = float(np.median(intensity[clutter & np.isfinite(intensity)]))
    use_local = np.isfinite(lm) & (lm > 0) & enough
    lm = np.where(use_local, lm, gbg)
    alpha, _ = D._fit_shape_alpha(intensity[sea] / lm[sea], D.PFA)
    thr = lm * alpha
    base_bright = (intensity > thr) & sea & np.isfinite(intensity)

    H, W = db.shape
    rng = np.random.default_rng(0)
    # candidate sites: sea, locally supported, not already bright, room for a 3x5 stamp
    ys, xs = np.where(sea & use_local & ~base_bright)
    keep = (ys > 5) & (ys < H - 6) & (xs > 5) & (xs < W - 6)
    ys, xs = ys[keep], xs[keep]
    idx = rng.choice(ys.size, min(N_SITES, ys.size), replace=False)

    out = {"tile": TILE.name, "alpha": round(float(alpha), 3), "n_sites": int(idx.size),
           "factor_strong": FACTOR_STRONG, "factor_weak": FACTOR_WEAK, "results": {}}

    for label, offsets in SHAPES.items():
        for tag, factor in (("strong", FACTOR_STRONG), ("weak", FACTOR_WEAK)):
            rec_open = rec_noopen = 0
            for k in idx:
                r0, c0 = ys[k], xs[k]
                fp = np.zeros((H, W), bool)
                work = intensity.copy()
                for dr, dc in offsets:
                    rr, cc = r0 + dr, c0 + dc
                    fp[rr, cc] = True
                    work[rr, cc] = thr[rr, cc] * factor
                bright = (work > thr) & sea & np.isfinite(work)
                rec_open += recovered(bright, fp, opening=True)
                rec_noopen += recovered(bright, fp, opening=False)
            n = idx.size
            out["results"][f"{label}/{tag}"] = {
                "recovery_with_opening": round(rec_open / n, 3),
                "recovery_without_opening": round(rec_noopen / n, 3),
                "lost_to_opening": round((rec_noopen - rec_open) / n, 3),
            }

    (ROOT / "audit" / "results" / "morphology.json").write_text(json.dumps(out, indent=2))
    print(f"tile={TILE.name} alpha={out['alpha']} sites={out['n_sites']}\n")
    print(f"{'shape/strength':26} {'with opening':>13} {'no opening':>12} {'lost to opening':>16}")
    for k, v in out["results"].items():
        print(f"{k:26} {v['recovery_with_opening']:>13.2f} {v['recovery_without_opening']:>12.2f} {v['lost_to_opening']:>16.2f}")


if __name__ == "__main__":
    main()
