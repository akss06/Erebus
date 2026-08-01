"""
Milestone 3 prep: cache raw AIS presence records for the pass date to
data/ais_positions.json, so app.py can render AIS markers fully offline
(no live GFW calls at Streamlit runtime).

Does not modify fetch_ais.py or match.py - only calls their existing
public functions once and writes a plain JSON snapshot. Because GFW's
4wings/report endpoint is non-deterministic across identical queries (see
results/MILESTONE2_FINDINGS.md #2), this file is a single point-in-time
snapshot, not a live source of truth - re-run this script to refresh it.

Run once from the project root: `python src/cache_ais.py`
"""
from __future__ import annotations

import json
from pathlib import Path

import fetch_ais
import match

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "milestone1_confirmed.json"
OUT_PATH = PROJECT_ROOT / "data" / "ais_positions.json"


def main() -> None:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    box = config["detection_sub_box"]

    token = fetch_ais.get_token()
    records = match.load_ais_records_for_date(
        token, box["lon_min"], box["lat_min"], box["lon_max"], box["lat_max"], match.PASS_DATE
    )
    OUT_PATH.write_text(json.dumps(records, indent=2))
    print(f"[cache_ais] Saved {len(records)} AIS presence record(s) to {OUT_PATH}")


if __name__ == "__main__":
    main()
