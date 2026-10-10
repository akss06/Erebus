"""
Tuticorin outer anchorage, 29 Aug 2026: run v3 on the Sentinel-1 pass (00:32 UTC) and
the ISRO EOS-04 pass (00:37 UTC) over the same box, and count which detections both
satellites saw. The anchorage is excluded from the Gulf run (run_recent.TUTICORIN_BOX)
and the Tuticorin tab only has Jan-Feb dates, so this pass was never analysed there.

Same box as the Tuticorin tab (run_multi_date.AOI_*), same own-AIS match (GFW presence,
1500 m) as build_tuticorin_v3.py. EOS-04 handling (dB conversion, 18 m rescaling) is
shared with eos04_detect.py.

Run from the project root (needs Earth Engine + GFW_API_TOKEN):
  python -P -E src/eos04_anchorage.py data/eos04/scene_25/<product dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_tuticorin_v3 as bt
import detect_v3
import eos04_detect as e4
import fetch_ais
import fetch_sar
import match
import run_multi_date as rmd
import validate_vs_gfw as vgfw

ROOT = Path(__file__).resolve().parent.parent
DATE = "2026-08-29"
OUT_DIR = ROOT / "data" / "eos04" / "anchorage"


def s1_clip() -> tuple[Path, Path]:
    vv, vh = OUT_DIR / f"{DATE}_s1_vv.tif", OUT_DIR / f"{DATE}_s1_vh.tif"
    if not (vv.exists() and vh.exists()):
        aoi = fetch_sar.build_aoi(rmd.AOI_LON_MIN, rmd.AOI_LAT_MIN, rmd.AOI_LON_MAX, rmd.AOI_LAT_MAX)
        scene = vgfw.find_scene((rmd.AOI_LON_MIN + rmd.AOI_LON_MAX) / 2, (rmd.AOI_LAT_MIN + rmd.AOI_LAT_MAX) / 2, DATE)
        fetch_sar.download_scene(aoi, scene, vv, scale=rmd.DOWNLOAD_SCALE_M, band="VV")
        fetch_sar.download_scene(aoi, scene, vh, scale=rmd.DOWNLOAD_SCALE_M, band="VH")
    return vv, vh


def nearest(d: dict, others: list[dict]) -> float:
    return min((match.haversine_m(d["lon"], d["lat"], o["lon"], o["lat"]) for o in others), default=float("inf"))


def main(product_dir: Path) -> None:
    fetch_sar.authenticate_and_init(rmd.PROJECT_ID)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    vv, vh = s1_clip()
    s1, s1_diag = detect_v3.run_detector_v3(vv, vh)          # default (10 m) settings

    meta = e4.read_band_meta(product_dir)
    hh = next(product_dir.glob("scene_HH/imagery_HH.tif"))
    hv = next(product_dir.glob("scene_HV/imagery_HV.tif"))
    with rasterio.open(vv) as s, rasterio.open(hh) as e:
        b_eos = transform_bounds(s.crs, e.crs, *s.bounds)
    eos_hh, eos_hv = OUT_DIR / f"{DATE}_eos04_hh.tif", OUT_DIR / f"{DATE}_eos04_hv.tif"
    frac = e4.cut_tile(hh, float(meta["Calibration_Constant_HH"]), b_eos, eos_hh)
    e4.cut_tile(hv, float(meta["Calibration_Constant_HV"]), b_eos, eos_hv)
    settings = e4.scale_v3_to_pixel(abs(rasterio.open(hh).transform.a))   # after the S1 run: mutates v3 globals
    eos, eos_diag = detect_v3.run_detector_v3(eos_hh, eos_hv)

    ais = bt.ais_for(DATE, fetch_ais.get_token())
    for dets in (s1, eos):
        matched = match.match_detections(dets, ais, rmd.MATCH_RADIUS_M)
        for d, m in zip(dets, matched):
            d.update(match_status=m["match_status"], matched_name=m.get("matched_name"))
    for d in s1:
        d["nearest_other_m"] = round(nearest(d, eos))
    for d in eos:
        d["nearest_other_m"] = round(nearest(d, s1))

    def agree(dets: list[dict]) -> dict:
        out = {"n": len(dets), **{f"other_within_{r}m": sum(d["nearest_other_m"] <= r for d in dets) for r in (100, 250)}}
        for status in ("MATCHED", "UNMATCHED"):
            sub = [d for d in dets if d["match_status"] == status]
            out[status.lower()] = {"n": len(sub), "other_within_100m": sum(d["nearest_other_m"] <= 100 for d in sub)}
        return out

    summary = {"date": DATE, "box": [rmd.AOI_LON_MIN, rmd.AOI_LAT_MIN, rmd.AOI_LON_MAX, rmd.AOI_LAT_MAX],
               "eos04_product": meta["OTSProductID"], "eos04_box_coverage": round(frac, 3),
               "v3_eos04_rescaled": settings, "s1_status": s1_diag.get("status"), "eos04_status": eos_diag.get("status"),
               "ais_records": len(ais),
               "sentinel1": agree(s1), "eos04": agree(eos)}
    for name, dets in (("s1", s1), ("eos04", eos)):
        keep = ("lon", "lat", "row", "col", "area_px", "tcr_db", "contrast_db", "match_status", "matched_name", "nearest_other_m")
        (OUT_DIR / f"{DATE}_{name}_detections.json").write_text(
            json.dumps([{k: d.get(k) for k in keep} for d in dets], default=float, indent=1), encoding="utf-8")
    res = ROOT / "audit" / "results" / f"eos04_vs_s1_{DATE}_anchorage.json"
    res.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=float))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
