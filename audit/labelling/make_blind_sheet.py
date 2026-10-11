"""
Build the blind labelling sheet: one offline HTML page with the Gulf of Mannar review
queue (one image per location) plus a random sample of detections the app discarded
as clutter, shuffled together, with no class, score, AIS or date shown. Each teammate
labels alone and downloads their answers; score_labels.py combines them.

Writes the sheet to the given folder and the code -> detection key to
audit/labelling/blind_key.json (kept out of the sheet so labellers can't see it).
Run from the project root:  python -P -E audit/labelling/make_blind_sheet.py "<out dir>"
"""
from __future__ import annotations

import base64
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.main import DETECTIONS, alerts, crop  # noqa: E402

AREA = "gulf_of_mannar"
N_CLUTTER = 20
SEED = 20261011
KEY = Path(__file__).resolve().parent / "blind_key.json"


def crop_b64(det_id: str) -> str:
    return base64.b64encode(Path(crop(det_id).path).read_bytes()).decode("ascii")


def main(out_dir: Path) -> None:
    rng = random.Random(SEED)
    items = [{"detection_id": a["best_detection_id"], "set": "queue", "class": a["confidence_class"]}
             for a in alerts(area=AREA, limit=200, sim=None)]
    clutter = sorted(d["id"] for d in DETECTIONS.values()
                     if d["area_id"] == AREA and d["confidence_class"] == "CLUTTER" and d.get("_crop_file"))
    items += [{"detection_id": i, "set": "clutter_control", "class": "CLUTTER"} for i in rng.sample(clutter, N_CLUTTER)]
    rng.shuffle(items)
    for n, it in enumerate(items, 1):
        it["code"] = f"L{n:02d}"
    KEY.write_text(json.dumps(items, indent=1), encoding="utf-8")

    data = [{"code": it["code"], "img": crop_b64(it["detection_id"])} for it in items]
    html = (Path(__file__).resolve().parent / "sheet_template.html").read_text(encoding="utf-8")
    out = out_dir / "Erebus-Blind-Labelling.html"
    out.write_text(html.replace("__ITEMS__", json.dumps(data)), encoding="utf-8")
    print(f"[sheet] {len(items)} images ({sum(i['set'] == 'queue' for i in items)} queue + {N_CLUTTER} clutter) -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
