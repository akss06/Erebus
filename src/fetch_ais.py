"""
Milestone 2 step: Global Fishing Watch (GFW) API v3 - AIS data exploration.

IMPORTANT - what the API actually offers (confirmed against GFW's own API
docs, then against live responses - the two didn't fully agree):

  - Vessels API (/vessels/search, /vessels/{id}): identity/registry lookup
    only - no positions.

  - Events API (/events): DOES return individual timestamped positions
    (start/end + lat/lon per event - fishing, encounter, loitering,
    port_visit, AIS-off "gap", and GFW's own SAR detections). BUT it is
    vessel-ID-centric: every query requires a `vessels[0]` parameter. There
    is no bbox/geometry/region filter on this endpoint. You cannot ask "who
    was in this box on this date" here - only "what did vessel X do",
    which requires already knowing which vessel to ask about.

  - 4Wings API (/4wings/report): the ONLY endpoint that accepts a bounding
    box / GeoJSON polygon directly. Docs describe it as returning grid-cell
    AGGREGATES only (hours, vessel counts) - but live testing found more
    nuance than that:

      * A SINGLE-DAY date-range (e.g. "2026-01-18,2026-01-18") on the
        public-global-presence dataset returns a null entry - no data.
      * A MONTH-WIDE date-range (e.g. "2026-01-01,2026-01-31") on the same
        bbox returns hundreds of individual VESSEL-LEVEL records: mmsi,
        shipName, flag, vesselType, an approximate lat/lon (snapped to the
        ~1/100 degree "HIGH" resolution grid, NOT the vessel's exact
        position), a "date" (one day within the range), and "hours"
        (presence-hours in that grid cell on that date).

    So this dataset is not purely aggregated after all - it's per-vessel,
    per-grid-cell, per-day, but apparently only populated/returned when
    queried over a wide enough date window. Why a single day returns
    nothing isn't understood yet (could be an API quirk, could be how the
    underlying data is binned) - flagging this as a real API behavior
    observed, not something to build around blindly.

  Net effect: this is still not a precise per-vessel ping at the SAR pass
  time - "hours":22 means 22 hours of presence *somewhere in that day*, not
  a timestamp we can compare against the ~00:32 UTC pass directly. That
  part of PROJECT_SPEC.md's gotcha #5 (AIS temporal precision is the weak
  point) holds. But named vessel identity + approximate location per day is
  considerably more than "aggregated numbers only," which is worth
  correcting from the initial docs-only read.

This module is exploratory per Milestone 2 steps 2-3: confirm what's
available and print real responses. No matching logic here yet.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

GFW_API_BASE = "https://gateway.api.globalfishingwatch.org/v3"


def _load_dotenv() -> None:
    """Minimal .env loader (KEY=VALUE lines) so GFW_API_TOKEN can be kept in
    a gitignored .env file instead of re-exported every session. Doesn't
    override a variable that's already set in the real environment. Not a
    full dotenv implementation - just enough for this one file."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def get_token() -> str:
    """Read the GFW API token from the environment (loading .env first if
    present). Never hardcode it."""
    _load_dotenv()
    token = os.environ.get("GFW_API_TOKEN")
    if not token:
        raise RuntimeError(
            "GFW_API_TOKEN is not set. Set it as an environment variable, "
            "or put it in a .env file at the project root, e.g.:\n"
            "  PowerShell:  $env:GFW_API_TOKEN = '<token>'\n"
            "  Bash:        export GFW_API_TOKEN='<token>'\n"
            "  .env file:   GFW_API_TOKEN=<token>"
        )
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def explore_vessel_search(
    token: str, query: str = "test", limit: int = 1, dataset: str = "public-global-vessel-identity:latest"
) -> dict:
    """Lightweight sanity-check call: confirms the token/auth actually works
    before spending a call on the heavier 4wings report request."""
    url = f"{GFW_API_BASE}/vessels/search"
    params = {"query": query, "limit": limit, "datasets[0]": dataset}
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    print(f"[explore] GET {url} -> {resp.status_code}")
    print(resp.text[:2000])
    resp.raise_for_status()
    return resp.json()


def fetch_presence_report(
    token: str,
    lon_min: float,
    lat_min: float,
    lon_max: float,
    lat_max: float,
    date_start: str,
    date_end: str,
    dataset: str = "public-global-presence:latest",
    temporal_resolution: str = "DAILY",
) -> dict:
    """POST /4wings/report for a bbox + date range. See module docstring -
    a single-day range reliably returns null; a wider range returns
    per-vessel, per-grid-cell, per-day presence records. This function just
    fetches and returns the raw parsed JSON - no filtering/matching here.
    """
    url = f"{GFW_API_BASE}/4wings/report"
    geojson = {
        "type": "Polygon",
        "coordinates": [[
            [lon_min, lat_min],
            [lon_max, lat_min],
            [lon_max, lat_max],
            [lon_min, lat_max],
            [lon_min, lat_min],
        ]],
    }
    params = {
        "datasets[0]": dataset,
        "date-range": f"{date_start},{date_end}",
        "temporal-resolution": temporal_resolution,
        "spatial-resolution": "HIGH",
        "format": "JSON",
    }
    resp = requests.post(url, headers=_headers(token), params=params, json={"geojson": geojson}, timeout=60)
    print(f"[fetch] POST {url}  date-range={date_start},{date_end}  -> {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    # Target parameters per Milestone 2 step 3 - our confirmed Milestone 1 sub-box.
    LON_MIN, LAT_MIN, LON_MAX, LAT_MAX = 78.22, 8.72, 78.32, 8.85
    PASS_DATE = "2026-01-18"  # satellite pass was ~00:32 UTC this date

    tok = get_token()

    print("=" * 70)
    print("STEP A: sanity-check auth with a lightweight vessel search")
    print("=" * 70)
    try:
        explore_vessel_search(tok)
    except requests.HTTPError as e:
        print(f"[explore] Step A failed ({e}) - continuing anyway.")

    print()
    print("=" * 70)
    print(f"STEP B: 4wings/report, single day ({PASS_DATE}) - expect null per docstring")
    print("=" * 70)
    try:
        data = fetch_presence_report(tok, LON_MIN, LAT_MIN, LON_MAX, LAT_MAX, PASS_DATE, PASS_DATE)
        print(data)
    except requests.HTTPError as e:
        print(f"[explore] Step B failed ({e}).")

    print()
    print("=" * 70)
    print("STEP C: 4wings/report, full month - expect vessel-level records")
    print("=" * 70)
    try:
        data = fetch_presence_report(tok, LON_MIN, LAT_MIN, LON_MAX, LAT_MAX, "2026-01-01", "2026-01-31")
        entries = data.get("entries", [{}])[0].get("public-global-presence:v4.0") or []
        print(f"[fetch] {len(entries)} vessel-presence records for the month.")

        on_pass_date = [e for e in entries if e.get("date") == PASS_DATE]
        print(f"[fetch] {len(on_pass_date)} of those are on the pass date ({PASS_DATE}):")
        for e in on_pass_date:
            print(f"    mmsi={e['mmsi']}  name={e['shipName']!r:25}  flag={e['flag']}  "
                  f"type={e['vesselType']:10}  lat={e['lat']}  lon={e['lon']}  hours={e['hours']}")
    except requests.HTTPError as e:
        print(f"[explore] Step C failed ({e}).")
