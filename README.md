# ERÉBUS — Dark Vessel Detection

Detect ships in real **Sentinel-1 SAR** imagery over Indian waters, cross-reference
them against **AIS** broadcasts, and flag radar targets that appear on satellite but
are **not** broadcasting their position — *candidate "dark vessels"*.

Built by **Team SREEGOAT / Erébus** (Manipal Institute of Technology, Bengaluru) for
TechGig *Ideas of India 2026*, Round 2.

**Live:** frontend on **Vercel**, backend API on **Render** (Docker). The hosted demo
serves the full precomputed viewer; the live "Run Analysis" button is gated there (it
needs Earth Engine credentials) — see [§11 Deployment](#11-deployment) and
[DEPLOY.md](DEPLOY.md).

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
| Deployment | Frontend → Vercel (static build); backend → Render / any Docker host (`Dockerfile`) |

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
├── DEPLOY.md                    — step-by-step deploy (Vercel + Render)
├── FINAL_REPORT.md              — Oct-2026 audit: bugs, corrected claims, experiments, outcomes
├── AUDIT_CHECKLIST.md           — per-finding evidence → fix → verification table
├── Dockerfile / .dockerignore   — backend image for Render / any Docker host
├── render.yaml                  — Render blueprint (optional; free tier is created manually)
├── requirements.txt             — Python deps (the real detection + API stack)
├── backend/
│   └── main.py                  — FastAPI app + live "Run Analysis" (SSE) + /api/health capability flag
├── frontend/                    — Vite + React + TypeScript + MapLibre
│   └── src/                     — App, MapView, AlertsPanel, DetailPanel, AnalyzePanel, api.ts, classes.ts
│                                  (api.ts reads VITE_API_BASE for the backend origin)
├── src/
│   ├── fetch_sar.py             — GEE → calibrated VV GeoTIFF clip (service-account auth for servers)
│   ├── land_mask.py             — Natural Earth 10 m land → sea mask (reprojected to the tile grid)
│   ├── detect.py                — CA-CFAR detector (linear intensity → blobs + contrast/shape)
│   ├── detect_v3.py             — v3 superpixel Generalized-Gamma CFAR detector (§9)
│   ├── crops.py                 — canonical fixed [-23,+3] dB radar-crop renderer (shared everywhere)
│   ├── fetch_ais.py             — GFW API client (AIS presence + SAR-presence)
│   ├── match.py                 — spatial match of detections to AIS → MATCHED / UNMATCHED
│   ├── confidence.py            — cross-pass clustering + persistence + chain geometry → confidence class
│   ├── validate_vs_gfw.py       — run our detector where GFW reports SAR detections; agreement metrics
│   ├── build_gulf_candidates.py — turn validation tiles into scored Gulf of Mannar candidates + crops
│   ├── run_recent.py            — multi-date hotspot pipeline on recent (Jun–Sep 2026) passes
│   ├── enrichment_test.py       — enrichment test: do strong blobs cluster at GFW's AIS-less points?
│   └── run_multi_date.py        — multi-pass Tuticorin run
├── audit/                       — baseline manifest, experiments (calibration/morphology/ablation),
│                                  results, rescore_historical.py, regen_crops.py
├── tests/                       — test_audit_fixes.py (regression tests for the audit fixes)
├── config/
│   └── milestone1_confirmed.json— frozen Round 1 detector settings + AOI
├── data/                        — SAR clips (gitignored *.tif), scored GeoJSON, crops, caches
└── results/                     — findings write-ups (Milestone 1/2, validation, dark search)
```

---

## 5. Detection methodology

The project has **two detectors**: the original **CA-CFAR** (`detect.py`, described
here — still used for the Tuticorin and Jan 2026 sets and for the frozen GFW
validation) and the newer **v3 superpixel Generalized-Gamma CFAR** (`detect_v3.py`,
§9, used for the recent Jun–Sep set). Both feed the same confidence scorer below.

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
| `ANCHORED_VESSEL` | same spot on 3+ passes **and** the same AIS MMSI each time (our own AIS match) — anchored-vessel hypothesis |
| `PERSISTENT_UNIDENTIFIED` | same spot on 3+ passes but no consistent identity — could be fixed infrastructure **or** a dark ship at anchor; stays in the review queue |
| `SUSPECTED_FIXED` | heuristic fixed-object signal — a **collinear persistent chain** (reef/shoal/causeway; a compact anchorage blob is *not* flagged) or **position-stability** (pinned across passes). Supports a reef/infrastructure reading but does **not** confirm it; **stays in the review queue**, deprioritized, labelled *suspected* not charted (`fixed_evidence` records which) |
| `FIXED_OBJECT` | **confirmed** fixed infrastructure from **charted** evidence (`context_fixed`) only — never inferred from persistence or geometry. Excluded from the review queue |
| `VESSEL_CANDIDATE` | single pass, strong contrast, ship-sized, AIS evidence of a vessel nearby |
| `DARK_CANDIDATE` | single pass, strong contrast, ship-sized, with **established** no-AIS evidence (our own radius check came up empty, or GFW reports no AIS here) — needs review |
| `UNVERIFIED_TARGET` | strong, ship-sized, but AIS status **never established** (not checked / no reference). Reviewable, but cannot be called "dark" without an AIS check |
| `LOW_CONFIDENCE` | bright but small/weak |
| `CLUTTER` | below the weak-contrast evidence bar |

The 0–100 number is a **heuristic evidence/ranking score, not a calibrated
probability** (`score_basis` records this on every detection).

**Persistence does not prove "fixed structure."** A dark ship anchored for weeks looks
exactly like infrastructure on radar; persistence cannot separate the two, so such
targets are `PERSISTENT_UNIDENTIFIED` (reviewable), not silently dismissed as
`FIXED_OBJECT`. **GFW association is kept distinct from AIS evidence**
(`gfw_association` ∈ associated/ambiguous/none; `ais_evidence` ∈ matched /
gfw_reported_ais / gfw_reported_no_ais / unmatched / unknown). When GFW's SAR-presence
product corroborates a target *within match radius*, the contrast bar drops from 15 dB
to **12 dB** — but this is **shared-sensor algorithmic agreement on the same Sentinel-1
imagery, not independent-sensor confirmation**, and the lowered bar is an experimental
heuristic, not a validated threshold. A positive **VH/VV cross-pol** signal adds
confidence but never demotes.

> See [FINAL_REPORT.md](FINAL_REPORT.md), [AUDIT_CHECKLIST.md](AUDIT_CHECKLIST.md) and
> [audit/](audit/) for the Oct-2026 scientific audit: confirmed bugs, corrected
> interpretations, experiments (P_fa calibration, morphology, ablation, enrichment),
> measured outcomes and the experiments blocked by missing labels.

---

## 6. Validation — how well does it agree with GFW?

GFW publishes Sentinel-1 SAR detections (`public-global-sar-presence`), each flagged
matched/unmatched to AIS. We run our detector where GFW reports a detection and measure
agreement. **GFW is derived from the same Sentinel-1 imagery by a different algorithm**
— so this is shared-sensor agreement, not independent-sensor confirmation — and it is a
**reference, not ground truth** (it also misses ships and has false alarms; positions
are grid-snapped to ~1 km).

**Full search, 3 passes (2026-01-06 / 18 / 30), Gulf of Mannar + Palk Strait:**
- GFW SAR detections: **143** (22 with an AIS match, 121 without).
- Found by us within 1500 m: **111 (78%)** — AIS-matched 20/22 (91%), AIS-less 91/121 (75%).

**Do not quote "78% recall" to the jury:** our detector fires often enough that a
1.5 km circle catches a random detection a large fraction of the time. The defensible
figure is the **enrichment test** (`enrichment_test.py`, rewritten in the audit to use
actual valid-water area and a tile-level block bootstrap instead of a per-blob Poisson
independence assumption):

> Strong ship-like radar returns occur at a **~7.5× higher density** (95% CI
> [4.3, 13.2] at 1.5 km / 12 dB; 6–12× across the radius/contrast sweep) near GFW's
> AIS-less detections than in the surrounding valid water. This is a **population-level
> association only** — it is *not* candidate precision and *not* a probability that any
> particular target is a vessel, and it is not evidence over arbitrary ocean (all tiles
> are GFW hotspots).

**AIS coverage, tested:** AISstream's free live-AIS network returned **0 messages**
over Indian waters across five separate tests (677 ships in a global box; 0 in ours).
This is the concrete, tested case for an official feed (Coast Guard / NIC) — the
single biggest gap in operational-grade matching.

---

## 7. Scored datasets currently in the app

| Area | Detections | Breakdown | Detector |
|---|---|---|---|
| **Gulf of Mannar (Jun–Sep 2026)** | 572 | VESSEL_CANDIDATE 4 · PERSISTENT_UNIDENTIFIED 67 · LOW 4 · CLUTTER 480 · **DARK 17** | **v3** (superpixel GG-CFAR, §9) |
| **Tuticorin anchorage** | 70 over 4 passes | ANCHORED 4 · VESSEL_CANDIDATE 11 · PERSISTENT_UNIDENTIFIED 15 · LOW 12 · CLUTTER 28 · **DARK 0** | CA-CFAR detections, audited scoring |
| **Gulf of Mannar / Palk Strait (Jan 2026)** | 95 | VESSEL_CANDIDATE 9 · CLUTTER 70 · **DARK 16** | CA-CFAR detections, audited scoring |

All dark-candidate counts are **unverified candidates pending review**, not confirmed
dark vessels. The Oct-2026 audit re-scored all three sets with the corrected semantics
(former `FIXED_OBJECT` → `PERSISTENT_UNIDENTIFIED`; GFW≠AIS; heuristic score). The
recent set's detections come from the v3 detector; the Tuticorin and Jan 2026
*detections* are still the original CA-CFAR output (their geometry is frozen as the
baseline — re-running them through the corrected v3 detector needs a network JRC fetch
and is noted as pending in [FINAL_REPORT.md](FINAL_REPORT.md) §5). The Jan 2026 DARK
count moved 23→16 because the already-committed 12 dB evidence bar (which the recent set
already used) was finally applied consistently — a relabel, not a detector change.

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
  NEREUS PROGRESS, seen across passes, falls to `PERSISTENT_UNIDENTIFIED` — the honest
  limitation: an anchored vessel with AIS removed looks like a persistent unidentified
  return, not a clean dark candidate. It does, however, stay in the review queue rather
  than being dismissed. Always behind an amber **SIMULATION** banner.
- **Run Analysis**: kick off the detection pipeline on a new AOI/date on demand, with
  live progress streamed over Server-Sent Events. It runs the same corrected
  `detect_v3.run_detector_v3` detector the write-up describes (not the Round 1 path) and
  renders crops with the same fixed −23/+3 dB window as every other dataset
  (`src/crops.py`). It **discovers the actual Sentinel-1 acquisition dates and never
  guesses** — if discovery fails the run errors, and if there are no scenes it reports
  that, rather than fabricating a 12-day cadence. It reports **coverage** (tiles analysed
  vs unavailable vs no open sea) and distinguishes a **GFW request failure** (that date
  *unperformed* → `partial`) from a **successful empty response** (no detections there),
  so a failed retrieval is never presented as an empty sea. On the hosted demo this
  button is disabled unless the backend has live-analysis credentials (gated via
  `/api/health`; see §11).

**API endpoints** (`backend/main.py`): `/api/health`, `/api/areas`, `/api/detections`
(+`/{id}`), `/api/clusters`, `/api/alerts`, `/api/crop/{id}`,
`/api/overlay/tuticorin.png`, `/api/reviews` (GET/POST), `/api/simulation/vessels`,
and `/api/analyze` + `/api/analyze/{run_id}` + `/api/analyze/{run_id}/stream` for live
runs.

---

## 9. The v3 detector (`detect_v3.py`)

Visual review of the recent run surfaced three false-positive modes the original
CA-CFAR could not handle: clustered clutter flagged as vessels, detections on small
unmasked reefs/islets, and faint speckle near bright returns reading as dark vessels.
`detect_v3.py` replaces the hand-tuned post-filters with a literature-grounded
detector — each stage mapped to a paper from a SAR false-positive-reduction review
(the Elicit set):

1. **JRC Global Surface Water mask + 1 km shore buffer** (GFW / Paolo et al. 2024) —
   replaces the coarse coastline. Water = JRC `occurrence ≥ 50 %`; land = Natural Earth
   land ∪ JRC non-water; then every pixel within 1 km of shore is dropped. **Caveat:**
   JRC v1.4 is a historical water-*occurrence* record, not an acquisition-time reef map
   — our probe found occurrence = 99 over the Adam's Bridge reefs, so JRC does **not**
   reliably exclude them. An authoritative charted reef layer is the right tool (pending).
2. **SLIC superpixels, robust-MAD clutter selection** (Pappas 2018; Li M-D 2022) —
   keep the dominant sea as clutter, exclude only genuinely bright outlier superpixels.
   (An earlier Otsu half-split flooded rough-sea tiles; the MAD test fixed it.)
3. **Generalized-Gamma CFAR, locally adaptive** (Martín-de-Nicolás 2015; Li 2022) —
   `threshold = local_clutter_mean × α`, where α is the GGD quantile of the *normalized*
   clutter for a **nominal** P_fa = 1e-5. P_fa is nominal only: the fit **trims** (does
   not censor) the top 1% before an ordinary MLE, so the achieved exceedance differs
   from nominal — the audit measured **~14.5× on synthetic Gamma clutter and ~63× pixel
   exceedance on real tiles** (`audit/experiments/`). The downstream gates and the 12 dB
   evidence bar do the real discrimination. A `MIN_TRAIN_PX=12` floor and finite-masking
   (no NaN propagation) were added; `run_detector_v3` returns diagnostics + an explicit
   status (ok/degraded/insufficient_data/no_sea).
4. **Shape + peak-to-clutter gates** (Ao & Xu 2018; Bi 2013) — eigen-ellipse solidity
   and eccentricity reject ragged blobs and reef/wake lines. *Note:* peak-to-clutter
   (TCR) does **not** discriminate over dark water, so plain contrast is the real lever.
   **Size floor caveat:** the pre-labelling `binary_opening` erases every target smaller
   than a solid 3×3 block, so `MIN_BLOB_PX=3` is effectively ~9 px compact (the audit's
   morphology experiment measured 100%→0% recovery for thin/≤8 px injected targets).
   A 15 m boat (~1–2 px at 10 m) is below this floor.
5. **VH/VV cross-pol** — a corroborating signal only (see §5); the background ratio is
   **tile-wide** (a genuinely local version is an experiment), and VH is used only if it
   is confirmed co-registered with VV.

The 1 km shore buffer is built on a padded grid (off-tile land included) with true
metric pixel spacing; the audit measured it **excludes ~15% of sea coverage** — a real
coverage cost, recorded per tile, not only "better discrimination".

**Honest crops:** `save_crop` now uses a fixed VV window (−23 → +3 dB) instead of a
per-crop percentile stretch, which was getting hijacked by bright azimuth-ambiguity
streaks in frame and washing real targets down to grey.

The frozen Round-1 CA-CFAR detector (`detect.py`) and the GFW validation
(`validate_vs_gfw.py`) are deliberately left untouched so the §6 validation numbers
stay honest.

**Still pending (needs data/network, see [FINAL_REPORT.md](FINAL_REPORT.md) §5):**
(a) an authoritative charted Adam's Bridge reef-chain mask (two Jun-30 dark candidates
sit on that shoal, which JRC reports as permanent water); (b) re-running the Tuticorin
and Jan 2026 *detections* through the corrected v3 detector (their scoring is already
audited; a full detector re-run needs the JRC fetch); (c) a labelled evaluation set to
turn the density/exceedance measurements into precision/recall.

---

## 10. How to run

**Prerequisites:** Python 3.11+, Node 18+, a Google Earth Engine account
(project `dark-vessel-detection-504204`), and a `.env` with `GFW_API_TOKEN`.

```bash
# Python deps for the detection pipeline
pip install -r requirements.txt

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

## 11. Deployment

The app is split for hosting: the **frontend** is a static Vite build on **Vercel**, and
the **backend** runs as a Docker container on **Render** (or any Docker host). The
FastAPI backend can't go on Vercel — its native deps (GDAL via rasterio) and the
long-running live analysis exceed serverless limits — so only the static frontend lives
there.

**How the two connect**
- The frontend reads the backend origin from **`VITE_API_BASE`** (baked in at build
  time; unset in dev → it calls `/api`, proxied to `localhost:8000` by `vite.config.ts`).
  Every data fetch, the SSE stream, and the crop/overlay **image URLs** are prefixed
  with it.
- The backend allows the frontend's browser origin via **`ALLOWED_ORIGINS`**
  (comma-separated; `*` is fine for this public, read-only demo).

**Backend environment variables** (Render → Environment):

| Var | Needed for | Notes |
|---|---|---|
| `ALLOWED_ORIGINS` | always | the Vercel origin(s), or `*` |
| `GFW_API_TOKEN` | live analysis only | Global Fishing Watch API token |
| `GEE_SERVICE_ACCOUNT` + `GEE_SA_KEY_JSON` | live analysis only | Earth Engine service account for headless auth |

**Live-analysis gating.** `/api/health` returns a `live_analysis` flag — true only when
the GFW token **and** an Earth Engine credential (a service account, or a cached local
login) are present. The frontend disables the **Run Analysis** button when it's false,
so the hosted demo never offers a run that would just error. The precomputed viewer
(areas, crops, overlay, AIS-off simulation) needs **no secrets**.

**Cold start.** Render's free tier sleeps when idle; the first request wakes it
(~30–60 s). The frontend shows a *"Waking the backend…"* overlay (with a Retry) so a
cold start never looks broken.

Full step-by-step for both services, in order, is in **[DEPLOY.md](DEPLOY.md)**.

---

## 12. Limitations (kept deliberately visible)

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
  validated. The 0–100 score is a **heuristic rank, not a calibrated probability**.
- **The nominal P_fa = 1e-5 is not the achieved rate.** The GG-CFAR fit trims rather
  than censors the tail; the audit measured ~14.5× inflation on synthetic clutter and
  ~63× pixel exceedance on real tiles. The gates and the 12 dB bar, not the P_fa, do the
  discrimination.
- **The morphology step suppresses small targets.** `binary_opening` erases everything
  below a solid 3×3 block (measured), so the effective size floor is ~9 px compact, well
  above a small boat — a sensitivity cost traded for ~4.5× fewer raw detections.
- **No labelled ground truth exists.** All quantitative results are density, pixel
  exceedance, coverage or population-level enrichment — **never precision/recall or
  "false objects per km²"**, which would require an independent labelled set (§5 of the
  final report lists what is blocked on this).
- **Reefs and azimuth ambiguities still produce some false positives.** Intermittently
  exposed shoals (e.g. Adam's Bridge) can read as targets, and strong scatterers throw
  bright azimuth-ambiguity "ghost" streaks; the v3 shape/mask gates reduce but do not
  eliminate these (see §9 pending work).
- **A human must eyeball the output.** Automated detection can be confidently wrong
  (clutter, platforms, rain cells); red dots are candidates for an analyst, not
  verdicts.

---

See `WORKLOG.md` for the full step-by-step record and `results/` for the detailed
Milestone 1/2, validation, and dark-search write-ups.
```