"""
Attach the ISRO EOS-04 cross-check (README §6) to the app's 29 Aug 2026 Gulf detections.

For every Sentinel-1 detection on that date that lies inside an EOS-04 tile we ran,
records whether eos04_detect.py found an object within SEEN_M and renders an EOS-04
crop of the same ground (same 1.2 km window as the Sentinel-1 crop, same fixed dB
window), centred on the Sentinel-1 position. Every covered detection gets an entry,
seen or not, so the panel shows misses as well as hits.

Writes data/eos04_crosscheck.json and data/recent/crops/<id>_eos04.png.
Needs the local EOS-04 outputs from eos04_detect.py (data/eos04/, gitignored).
Run from the project root:  python -P -E src/build_eos04_crosscheck.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import rasterio
from rasterio.warp import transform as warp_transform, transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crops
import match

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATE = "2026-08-29"
TILES = DATA / "eos04" / "scene_25" / "tiles"
EOS_DETS = DATA / "eos04" / f"eos04_{DATE}_scene25_detections.geojson"
OUT = DATA / "eos04_crosscheck.json"
CROP_DIR = DATA / "recent" / "crops"
SEEN_M = 100.0
S1_PX_M, EOS_PX_M = 10.0, 18.0


def main() -> None:
    eos = [f["geometry"]["coordinates"] for f in json.loads(EOS_DETS.read_text(encoding="utf-8"))["features"]]
    tiles = []
    for t in sorted(TILES.glob(f"{DATE}_*.tif")):
        if t.stem.count("_") != 1:   # skip _vh / _jrc companions
            continue
        with rasterio.open(t) as s:
            tiles.append((t, transform_bounds(s.crs, "EPSG:4326", *s.bounds)))

    s1 = [f for f in json.loads((DATA / "scored_recent.geojson").read_text(encoding="utf-8"))["features"]
          if f["properties"]["date"] == DATE]
    out: dict[str, dict] = {}
    half_px = round(crops.CROP_HALF_PX * S1_PX_M / EOS_PX_M)
    scale = round(crops.CROP_SCALE * crops.CROP_HALF_PX / half_px)
    for f in s1:
        lon, lat = f["geometry"]["coordinates"]
        inside = [(t, b) for t, b in tiles if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]]
        if not inside:
            continue
        # tile whose centre is closest, so the crop is least likely to hit an edge
        tif, _ = min(inside, key=lambda tb: (lon - (tb[1][0] + tb[1][2]) / 2) ** 2 + (lat - (tb[1][1] + tb[1][3]) / 2) ** 2)
        with rasterio.open(tif) as s:
            db = s.read(1).astype("float64")
            xs, ys = warp_transform("EPSG:4326", s.crs, [lon], [lat])
            col, row = ~s.transform * (xs[0], ys[0])
        img = crops.render_db_crop(db, row, col, half_px=half_px, scale=scale)
        pid = f["properties"]["id"]
        crop_name = None
        if img is not None:
            crop_name = f"{pid}_eos04.png"
            img.save(CROP_DIR / crop_name)
        dist = min((match.haversine_m(lon, lat, e[0], e[1]) for e in eos), default=float("inf"))
        out[pid] = {"seen": dist <= SEEN_M, "distance_m": round(dist), "crop": crop_name,
                    "s1_time_utc": "00:32", "eos04_time_utc": "00:37"}

    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    n_seen = sum(v["seen"] for v in out.values())
    print(f"[eos04] {len(out)} detections covered, {n_seen} seen by EOS-04 within {SEEN_M:.0f} m -> {OUT.name}")


if __name__ == "__main__":
    main()
