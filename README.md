# ERÉBUS — Dark Vessel Detection

Detect ships in real **Sentinel-1 SAR** imagery over Indian waters, cross-reference
them against **AIS** broadcasts, and flag radar targets that appear on satellite but
are **not** broadcasting their position — *candidate "dark vessels"*.

Built by **Team SREEGOAT / Erébus** (Manipal Institute of Technology, Bengaluru) for
TechGig *Ideas of India 2026*, Round 2.

> **Honesty is a design constraint, not an afterthought.** A radar return is only ever
> called a *candidate pending verification*, never a confirmed dark vessel. A zero
> result is reported as a zero result. Every item in the app carries a provenance
> label — **Real detection**, **Simulated**, or **Unverified candidate**. Simulated
> demos are always labelled as simulations. The limitations below are part of the
> deliverable.

---

## 1. The problem

A "dark vessel" is a ship visible to radar but missing from public vessel-tracking
(AIS). This covers illegal fishing across the India–Sri Lanka maritime boundary,
transshipment, and sanctions evasion. The **detection of uncounted vessels** is real
and demonstrable regardless of intent — and the honest nuance is kept front and
centre: in the Gulf of Mannar many small trawlers are "dark" simply because they
never carried an AIS transponder, not because they deliberately went silent.

**Area of interest:** Palk Strait / Gulf of Mannar (India–Sri Lanka IMBL) — the real,
contested site of ongoing trawling disputes, plus the Tuticorin anchorage as a
dense, repeat-pass test bed.

---

## 2. Pipeline

```
[1] AOI bounding box + Sentinel-1 pass date
        │
[2] fetch_sar     → calibrated Sentinel-1 GRD VV clip (GeoTIFF) from Google Earth Engine
        │
[3] land_mask     → mask land pixels (Natural Earth 10 m coastline + coast buffer)
        │
[4] detect        → CA-CFAR on linear intensity → ship-sized bright blobs (lat, lon, area, contrast)
        │
[5] fetch_ais     → GFW AIS presence + GFW SAR-detection records in AOI, ±1 day of the pass
        │
[6] match         → nearest AIS to each detection; no AIS within radius = UNMATCHED ("dark")
        │
[7] confidence    → cluster detections across passes; persistence + AIS-identity → confidence class
        │
[8] app           → interactive web map + review queue (green = matched, red = dark candidate)
```

Detection runs **locally in NumPy** on a small clipped GeoTIFF — far more debuggable
than server-side Earth Engine code. GEE's `COPERNICUS/S1_GRD` collection is already
radiometrically calibrated (sigma-nought, dB) and terrain-corrected, so there is **no
ESA SNAP / snappy** toolchain anywhere in this project.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| SAR source | Google Earth Engine — `COPERNICUS/S1_GRD`, IW mode, VV polarization, 10 m |
| AIS source | Global Fishing Watch API (`public-global-presence` + `public-global-sar-presence`) |
| Detection | Python, NumPy, SciPy, rasterio, GeoPandas, Shapely — CA-CFAR + morphology |
| Backend | FastAPI + Pydantic (serves precomputed GeoJSON; no GEE/GFW calls at request time) |
| Frontend | Vite + React 19 + TypeScript + MapLibre GL |

Secrets (Earth Engine credentials, `GFW_API_TOKEN`) live in `.env` / the host
environment and are gitignored — never committed.

---

## 4. Repo structure

```
dark-vessel-detection/
├── PROJECT_SPEC.md              — Round 1 spec, stack decisions, gotchas
├── ROUND2_PLAN.md               — Round 2 plan, deadlines, jury-question prep
├── WORKLOG.md                   — chronological record of every step (source of truth)
├── README.md                    — this file
├── backend/
│   └── main.py                  — FastAPI app + live "Run Analysis" (SSE) endpoints
├── frontend/                    — Vite + React + TypeScript + MapLibre
│   └── src/                     — App, MapView, AlertsPanel, DetailPanel, AnalyzePanel, api.ts, classes.ts
├── src/
│   ├── fetch_sar.py             — GEE → calibrated VV GeoTIFF clip
│   ├── land_mask.py             — Natural Earth 10 m land → sea mask (reprojected to the tile grid)
│   ├── detect.py                — CA-CFAR detector (linear intensity → blobs + contrast/shape)
│   ├── fetch_ais.py             — GFW API client (AIS presence + SAR-presence)
│   ├── match.py                 — spatial match of detections to AIS → MATCHED / UNMATCHED
│   ├── confidence.py            — cross-pass clustering + persistence → confidence class
│   ├── validate_vs_gfw.py       — run our detector where GFW reports SAR detections; agreement metrics
│   ├── build_gulf_candidates.py — turn validation tiles into scored Gulf of Mannar candidates + crops
│   ├── run_recent.py            — multi-date hotspot pipeline on recent (Jun–Sep 2026) passes
│   ├── enrichment_test.py       — objective Poisson test: do strong blobs cluster at GFW's AIS-less points?
│   └── run_multi_date.py        — multi-pass Tuticorin run
├── config/
│   └── milestone1_confirmed.json— frozen Round 1 detector settings + AOI
├── data/                        — SAR clips (gitignored *.tif), scored GeoJSON, crops, caches
└── results/                     — findings write-ups (Milestone 1/2, validation, dark search)
```

---

## 5. Detection methodology

### Land mask (`land_mask.py`)
Land is a bright radar reflector and buries a naive threshold in coastal false
positives. Land pixels are masked with the **Natural Earth 10 m** land polygon,
clipped to the AOI and reprojected onto the scene's native UTM grid, then the land
edge is dilated by a **30 px (~300 m) coast buffer** before detection.

### CA-CFAR detector (`detect.py`)
Cell-Averaging Constant False Alarm Rate, replacing a Round-1 global percentile
threshold. Each pixel is compared against the statistics of its own local
sea-clutter neighbourhood rather than one fixed cutoff for the whole scene:

```
dB → linear intensity
  → per-pixel threshold = local_mean + k · local_std   (guard band excluded, sea pixels only)
  → morphological opening (speckle removal)
  → connected-component labelling
  → per-blob centroid, area, mean/max dB, local-background contrast, aspect/fill shape
  → keep ship-sized blobs
```

Frozen settings (`config/milestone1_confirmed.json`): `guard_px=5`, `training_px=15`,
`k=5.0`, `opening_iterations=1`, blob area `2–60 px`. The number that actually
separates a target from clutter is **contrast** — the blob's brightness above its
*local* background, not a global scene average.

### Confidence scoring (`confidence.py`)
Detections from different passes over the same spot are clustered (100 m radius).
What a cluster does across passes is the strongest evidence available:

| Class | Rule |
|---|---|
| `ANCHORED_VESSEL` | same spot on 3+ passes **and** the same AIS MMSI each time — ship at anchor |
| `FIXED_OBJECT` | same spot on 3+ passes but the "matched" AIS vessel changes / is absent — pipeline, cable, shoal |
| `VESSEL_CANDIDATE` | single pass, strong contrast, ship-sized, AIS vessel nearby |
| `DARK_CANDIDATE` | single pass, strong contrast, ship-sized, **no** AIS within match radius — needs review |
| `LOW_CONFIDENCE` | bright but small/weak |
| `CLUTTER` | below the weak-contrast bar — indistinguishable from sea clutter |

**Key discovery (from the data):** persistence alone does **not** mean "fixed
structure." Pipeline/cable fragments reappear on every pass but "match" a *different*
AIS ship each time (whichever anchored nearby that day); a true anchored ship matches
the *same* MMSI every time. So the rule is persistence **plus AIS-identity
consistency**. When an independent detector (GFW's SAR detections) corroborates a
target, the contrast bar for vessel/dark-candidate drops from 15 dB to 10 dB — a
design decision to re-test, documented as such, not a validated threshold.

**Known blind spot:** a *dark* ship anchored in one place for weeks looks exactly like
a `FIXED_OBJECT`. Persistence cannot separate those two cases; such objects are
labelled `FIXED_OBJECT`, never "vessel." The AIS-off simulation exposes this rather
than hiding it.

---

## 6. Validation — how well does it agree with GFW?

GFW publishes its own independent Sentinel-1 SAR detections
(`public-global-sar-presence`), each flagged matched/unmatched to AIS. We run our
detector where GFW reports a detection and measure agreement. **GFW is a reference,
not perfect ground truth** — it also misses ships and has false alarms, and its
positions are grid-snapped to ~1 km.

**Full search, 3 passes (2026-01-06 / 18 / 30), Gulf of Mannar + Palk Strait:**
- GFW SAR detections: **143** (22 with an AIS match, 121 without).
- Found by us within 1500 m: **111 (78%)** — AIS-matched 20/22 (91%), AIS-less 91/121 (75%).

**Honest correction (do not quote "78% recall" to the jury):** our detector fires
~6.7 times per 100 km², so a 1.5 km circle catches a random detection ~37% of the
time. The defensible figure is the **enrichment test** (`enrichment_test.py`), a
Poisson comparison with no human judgement:

> Strong ship-like radar returns occur **~7.4× more often than chance** at the places
> GFW reports AIS-less detections — the *same* enrichment as for AIS-matched ships
> (p ≈ 2e-30). About 1 in 7 are expected to be chance; we flag the rest for analyst
> review. We cannot say which individual one is a ship.

**AIS coverage, tested:** AISstream's free live-AIS network returned **0 messages**
over Indian waters across five separate tests (677 ships in a global box; 0 in ours).
This is the concrete, tested case for an official feed (Coast Guard / NIC) — the
single biggest gap in operational-grade matching.

---

## 7. Scored datasets currently in the app

| Area | Detections | Breakdown | Notes |
|---|---|---|---|
| **Tuticorin anchorage** | 70 over 4 passes | ANCHORED 4 · VESSEL_CANDIDATE 10 · FIXED 18 · LOW 12 · CLUTTER 26 · **DARK 0** | Repeat-pass test bed; matches the Round-1 negative result |
| **Gulf of Mannar / Palk Strait** | 95 | VESSEL_CANDIDATE 9 · LOW 1 · CLUTTER 62 · **DARK 23** | From the 143-detection GFW search; all 23 dark candidates single-pass, 10–16 dB |
| **Gulf of Mannar (Jun–Sep 2026)** | 444 | VESSEL_CANDIDATE 17 · FIXED 57 · LOW 26 · CLUTTER 264 · **DARK 80** | Recent multi-date hotspot run; **detector being upgraded (see §9)** |

All dark-candidate counts are **unverified candidates pending review**, not confirmed
dark vessels.

---

## 8. The web app

Single FastAPI process: if `frontend/dist` exists it is served at `/`, and all data is
precomputed GeoJSON so the app works offline (no Earth Engine / GFW calls at request
time, except the live "Run Analysis" feature).

**What the UI shows**
- Map with one dot per physical location ("All passes combined"; dot size = number of
  passes seen), or a single pass via the date chips.
- Toggle-able class legend; optional SAR radar overlay.
- Ranked **review queue** (fixed objects and clutter excluded), with a detail panel:
  class + plain-language explanation, confidence, radar crop, the reasons behind the
  score, the AIS check, the same-location-on-other-passes table, and
  confirm/reject/unsure + note.
- **AIS-off simulation** (Tuticorin): switch off a chosen vessel's AIS and watch the
  system reclassify it. DMC JUPITER becomes a `DARK_CANDIDATE` (the clean demo);
  NEREUS PROGRESS becomes a `FIXED_OBJECT` (the honest blind spot). Always behind an
  amber **SIMULATION** banner.
- **Run Analysis**: kick off the detection pipeline on a new AOI/date on demand, with
  live progress streamed over Server-Sent Events.

**API endpoints** (`backend/main.py`): `/api/health`, `/api/areas`, `/api/detections`
(+`/{id}`), `/api/clusters`, `/api/alerts`, `/api/crop/{id}`,
`/api/overlay/tuticorin.png`, `/api/reviews` (GET/POST), `/api/simulation/vessels`,
and `/api/analyze` + `/api/analyze/{run_id}` + `/api/analyze/{run_id}/stream` for live
runs.

---

## 9. Detector upgrade — in progress

Visual review of the recent (Jun–Sep 2026) run surfaced three false-positive modes
the CA-CFAR detector does not handle: clustered clutter flagged as vessels, detections
on small unmasked reefs/islets, and faint speckle near bright coastal returns that
reads as a dark vessel. The fix in progress (`detect_v3.py`) replaces the hand-tuned
post-filters with a literature-grounded detector — each stage mapped to a paper from a
SAR false-positive-reduction review:

1. **JRC Global Surface Water mask + 1 km shore buffer** (GFW / Paolo et al. 2024) —
   replaces the coarse coastline so small Gulf of Mannar reefs and islets are masked.
2. **SLIC superpixels split into pure-clutter vs bright regions** (Pappas 2018; Li
   2022) — so target and land pixels do not poison the clutter estimate.
3. **Generalized-Gamma CFAR** (Martín-de-Nicolás 2015; Li 2022) — a heavy-tailed
   clutter quantile for a target false-alarm rate, instead of the Gaussian `mean+k·std`
   that over-detects speckle spikes.
4. **Peak-to-clutter ratio + Eigen-ellipse shape gates** (Ao & Xu 2018; Bi 2013) — the
   "bright radiating point vs faint speckle" discriminator.

The frozen Round-1 CA-CFAR detector (`detect.py`) and the GFW validation
(`validate_vs_gfw.py`) are left untouched so the reported validation numbers stay
honest.

---

## 10. How to run

**Prerequisites:** Python 3.11+, Node 18+, a Google Earth Engine account
(project `dark-vessel-detection-504204`), and a `.env` with `GFW_API_TOKEN`.

```bash
# Python deps for the detection pipeline
pip install earthengine-api rasterio numpy scipy geopandas shapely requests pillow scikit-image fastapi uvicorn pydantic

# Build the frontend once (or after any frontend change)
cd frontend && npm install && npm run build && cd ..

# Serve the whole app (API + built frontend) from one process
python -m uvicorn backend.main:app --port 8000
# open http://localhost:8000
```

Frontend dev mode with hot reload: `npm run dev` in `frontend/` (port 5173, proxies
`/api` to 8000).

**Re-run the detection pipelines** (needs Earth Engine + GFW auth):
```bash
python src/validate_vs_gfw.py --all --workers 4   # agreement vs GFW
python src/build_gulf_candidates.py               # Gulf of Mannar candidates + crops
python src/run_recent.py --max-hotspots 20        # recent multi-date hotspot run
```

---

## 11. Limitations (kept deliberately visible)

- **Small boats (~10–20 m) are near Sentinel-1's 10 m limit** and get lost in speckle.
  Many Gulf of Mannar AIS-less detections are very weak — consistent with exactly
  these boats. This is the case for higher-resolution SAR (NISAR / RISAT / commercial).
- **AIS is the weak link, not the SAR.** GFW AIS is aggregated, coarse in time, and
  patchy over India; free live AIS has no coverage here. "No AIS" means "not in GFW's
  AIS data," which is **not** the same as "deliberately dark."
- **Our detector and GFW's share the same Sentinel-1 scenes**, so agreement between
  them is two algorithms on one image, not two independent sensors.
- **Persistence cannot distinguish a long-anchored dark ship from a fixed structure**
  (see §5). Resolving this needs a known-infrastructure layer and/or shape analysis.
- **Confidence thresholds involve mild tuning on the same data** they were checked
  against (noted in `WORKLOG.md`); they need testing on a fresh area to be called
  validated.
- **A human must eyeball the output.** Automated detection can be confidently wrong
  (clutter, platforms, rain cells); red dots are candidates for an analyst, not
  verdicts.

---

See `WORKLOG.md` for the full step-by-step record and `results/` for the detailed
Milestone 1/2, validation, and dark-search write-ups.
```