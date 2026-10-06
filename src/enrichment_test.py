"""
Round 2: objective test that does not rely on anyone eyeballing a radar image.

Question: are strong, ship-sized blobs (>= MIN_CONTRAST_DB, >= MIN_AREA_PX) found near GFW's SAR detections more
often than they would be at an arbitrary place in the same tile?

For every tile (centred on one GFW detection) we count strong blobs within RADIUS_M of the centre, and estimate how
many we'd expect there by chance from the density of strong blobs in the rest of the tile. Summed over tiles and
compared with a Poisson test. Run separately for GFW detections WITH an AIS match (a control: real ships, so the
test should light up) and WITHOUT one (the dark-vessel question).

Conservative by construction: other GFW detections inside a tile raise the "elsewhere" density, which makes it
harder to show enrichment, not easier.

Run from the project root (needs data/validation tiles from validate_vs_gfw.py --all):
    python src/enrichment_test.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from scipy.stats import poisson

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match  # noqa: E402
import validate_vs_gfw as vgfw  # noqa: E402

RADIUS_M = 1500
MIN_CONTRAST_DB = 10.0
MIN_AREA_PX = 8
TUTICORIN_BOX = (78.20, 8.70, 78.34, 8.87)  # port anchorage: dense AIS traffic, different regime


def main() -> None:
    rows = json.loads((vgfw.VAL_DIR / "validation_rows.json").read_text())
    stats = {True: [0, 0.0, 0], False: [0, 0.0, 0]}  # has_ais -> [observed, expected, tiles]
    for r in rows:
        if TUTICORIN_BOX[0] <= r["lon"] <= TUTICORIN_BOX[2] and TUTICORIN_BOX[1] <= r["lat"] <= TUTICORIN_BOX[3]:
            continue
        gfw = json.loads((vgfw.VAL_DIR / f"gfw_sar_{r['date']}.json").read_text())
        i = next(k for k, g in enumerate(gfw) if abs(g["lon"] - r["lon"]) < 1e-6 and abs(g["lat"] - r["lat"]) < 1e-6)
        dets, sea_km2 = vgfw.run_detector_on_tile(vgfw.VAL_DIR / f"{r['date']}_{i:03d}.tif")
        strong = [d for d in dets if d["contrast_db"] >= MIN_CONTRAST_DB and d["area_px"] >= MIN_AREA_PX]
        dist = [match.haversine_m(r["lon"], r["lat"], d["lon"], d["lat"]) for d in strong]
        inside = sum(1 for x in dist if x <= RADIUS_M)
        outside = len(dist) - inside
        circle_km2 = math.pi * (RADIUS_M / 1000) ** 2
        elsewhere_km2 = max(sea_km2 - circle_km2, 1e-6)
        s = stats[r["gfw_has_ais_match"]]
        s[0] += inside
        s[1] += outside / elsewhere_km2 * circle_km2
        s[2] += 1

    for has_ais, label in ((True, "GFW detections WITH an AIS match (control)"), (False, "GFW detections WITHOUT an AIS match")):
        obs, exp, n = stats[has_ais]
        p = poisson.sf(obs - 1, exp) if exp > 0 else float("nan")
        print(f"{label}: {n} tiles")
        print(f"  strong blobs within {RADIUS_M} m of the GFW point: observed {obs}, expected by chance {exp:.1f}"
              f"  -> {obs / exp:.1f}x, Poisson p = {p:.2g}")


if __name__ == "__main__":
    main()
