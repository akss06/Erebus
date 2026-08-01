# Dark Vessel Detection — Project Spec

**One-line goal:** Detect ships in real Sentinel-1 SAR imagery over Indian
waters, cross-reference against AIS broadcasts, and flag vessels that appear
on radar but are *not* broadcasting their position ("dark vessels").

**What Round 1 needs this repo to prove:** that we can pull a real SAR scene,
detect vessels in it, and demonstrate the AIS cross-reference concept — with a
map showing green (matched) vs red (dark/unmatched) detections. Reaching
**Proof of Concept** (not just Idea Stage) is the goal.

---

## The pipeline (data flow)

```
[1] Define AOI (bounding box) + acquisition date
        ↓
[2] fetch_sar   → calibrated Sentinel-1 GRD VV clip (GeoTIFF)
        ↓
[3] land_mask   → mask out all land pixels (MANDATORY — see gotchas)
        ↓
[4] detect      → threshold / CFAR → list of detections (lat, lon, area, intensity)
        ↓
[5] fetch_ais   → AIS vessel positions in AOI within ±time window of the pass
        ↓
[6] match       → nearest AIS to each detection; unmatched = "dark vessel"
        ↓
[7] app         → interactive map: green = matched, red = dark
```

---

## Stack decisions (the "do this, not that")

- **SAR source: Google Earth Engine** (`COPERNICUS/S1_GRD`).
  - This collection is already **calibrated (sigma-nought) and terrain-corrected.**
  - **DO NOT use ESA SNAP / snappy.** It's a Java toolchain with a notoriously
    painful Python bridge and gigabyte downloads. GEE skips all of it.
  - Filter: instrument mode `IW`, polarization `VV`, transmitterReceiverPolarisation
    contains VV, orbit either. Pick a date/AOI where a real scene exists
    (query the collection for available dates first — don't assume a date has a pass).
  - Export a **small** clipped GeoTIFF for local processing. Keep the AOI modest
    (start ~10×10 km, then ~25×25 km) so `getDownloadURL` at 10 m scale stays
    under the size limit. Do detection **locally** in numpy — far more debuggable
    than GEE server-side code.
  - Fallback if GEE signup is slow: Copernicus Data Space Ecosystem (CDSE)
    Sentinel Hub Process API, free tier, returns calibrated GeoTIFF directly.

- **AIS source: Global Fishing Watch (GFW) API**, free research token.
  - This is the genuinely hard part of the project. Precise-timestamp historical
    AIS over Indian waters, free, does not really exist. GFW is aggregated and
    coarser in time than a real operational feed.
  - For Round 1 this is acceptable: the PoC proves *detection*; the AIS overlay
    demonstrates the *matching concept*. Be honest in the writeup that
    operational-grade matching needs a proper AIS feed (Coast Guard / commercial),
    which is exactly the kind of partnership named in the application's Q7.

- **Detection: start simple, add CFAR only if time allows.**
  - v1: convert to linear intensity → adaptive/percentile threshold on masked sea
    → morphological opening (remove speckle) → connected-component labeling →
    per-blob centroid + area → filter by area (ships ≈ a few to tens of pixels).
  - v2 (optional): CA-CFAR → local mean+std via a guard/training window
    (`scipy.ndimage`), threshold = mean + k·std. Handles varying sea state better.
  - Convert pixel centroids → lat/lon via the raster geotransform.

- **Frontend: Streamlit + folium** (or leafmap). Map with SAR footprint, green
  markers (matched), red markers (dark), popups with detection stats. Deploy to
  Streamlit Community Cloud — that URL is the demo-video / evidence link the form asks for.

- **Language:** Python. **Libraries:** `earthengine-api`, `geemap`, `rasterio`,
  `numpy`, `scipy`, `geopandas`, `shapely`, `requests`, `streamlit`, `folium`.

---

## Repo structure

```
dark-vessel/
├── PROJECT_SPEC.md          — this file
├── requirements.txt
├── data/                    — SAR clips, AIS pulls, coastline (gitignore large files)
├── notebooks/               — exploration / one-off checks
├── src/
│   ├── fetch_sar.py         — GEE → calibrated VV GeoTIFF clip
│   ├── land_mask.py         — mask land pixels using a coastline polygon
│   ├── detect.py            — threshold/CFAR → detection points
│   ├── fetch_ais.py         — GFW API → AIS positions in AOI + time window
│   └── match.py             — spatial+temporal match → flag dark vessels
└── app.py                   — Streamlit map demo
```

---

## Area of interest (AOI) — the India story

**Palk Strait / Gulf of Mannar** (India–Sri Lanka maritime boundary). Strong
narrative: this is the real, contested, ongoing site of illegal trawling disputes
and fishermen crossing the International Maritime Boundary Line.

**Honest nuance to keep in mind:** many small trawlers here are "dark" simply
because they never carried an AIS transponder, not because they deliberately went
silent. The *detection of uncounted vessels* is real and demonstrable either way.
The "deliberately went dark" framing is strongest for larger vessels in open
ocean (transshipment, sanctions evasion). Frame the PoC around "vessels present
but not in AIS" — that's true regardless of intent.

---

## 17-day milestone plan

**Milestone 1 — days 1–4: SAR → detections. THIS ALONE DE-RISKS THE IDEA.**
Get one real Sentinel-1 VV clip over the AOI, mask land, run v1 detection, and
render "here are N vessels we found" on a map. If this works, the idea is real
and every form answer can be written with confidence. If it breaks, you found out
on day 4, not day 17.

**Milestone 2 — days 5–9: AIS overlay + matching.**
Pull GFW AIS for the AOI/time, match to detections, split into green (matched)
vs red (dark). This completes the "dark vessel" story.

**Milestone 3 — days 10–14: web map + polish.**
Streamlit app, deploy, record a short demo. This is your evidence link.

**Days 15–17: write the form + buffer.**
Q1–Q9 mostly write themselves once the pipeline is real. Record the demo video.
Leave a full day of buffer for the things that always break.

---

## Gotchas Claude Code won't warn you about

1. **Land masking is not optional.** Land is a bright radar reflector and will
   bury you in false positives. Mask it with a coastline polygon (Natural Earth
   10 m land, or OSM/GSHHG) before detection.
2. **Skip SNAP.** If any tool or tutorial tells you to install `snappy`/`esa_snappy`,
   stop — use GEE's pre-calibrated collection instead.
3. **Small fishing boats (~10–20 m) are near the 10 m detection limit** and get
   lost in speckle. Don't overclaim; state this limit honestly.
4. **Confirm a real satellite pass exists** for your chosen date+AOI before
   building around it. Sentinel-1B failed in 2021, so revisit is sparser than
   old tutorials assume; Sentinel-1C (2024) helps. Query available dates first.
5. **AIS temporal precision is the weak point,** not the SAR. Budget your honesty
   here, not your effort — the SAR side is where the impressive, provable result is.
6. **A human must eyeball the output.** Vibe-coded detection can be confidently
   wrong (e.g. flagging sea clutter or platform structures). Sanity-check that
   red dots sit on plausible vessel-sized bright spots, not noise.
