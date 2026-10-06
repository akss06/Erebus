# Erébus Round 2 — Work Log

Chronological record of every step and change, written so you can retrace it later.
Plan and deadlines live in [ROUND2_PLAN.md](ROUND2_PLAN.md). Newest entries at the bottom.

Convention: throwaway scripts used for checking live in the Claude scratchpad (not the repo).
Anything worth keeping gets copied into `scripts/` or `src/` and noted here.

---

## 2026-10-03 — Step 1: credential and data-availability checks

**Goal:** confirm the external services the pipeline depends on still work.

**What I did (read-only, no repo files changed except this log and the plan):**

1. Read `.env` keys only (values not printed): it contains `GFW_API_TOKEN`. Earth Engine auth is in
   `~/.config/earthengine/credentials`; project id is `dark-vessel-detection-504204`
   (from `run_milestone1.py`).
2. Ran a check script that:
   - called GFW `/v3/vessels/search` with the token → **HTTP 200, token valid**;
   - called `ee.Initialize(project="dark-vessel-detection-504204")` → **works**;
   - listed Sentinel-1 IW/VV scenes in the last 120 days for three boxes.

**Results — Sentinel-1 passes, last 120 days (to 2026-10-03):**

| Area | Box (lon_min, lat_min, lon_max, lat_max) | Scenes | Orbits |
|---|---|---|---|
| Tuticorin anchorage | 78.22, 8.72, 78.32, 8.85 | 10 | 92 only (every 12 days; latest 2026-09-22) |
| Palk Strait central | 79.3, 9.6, 79.9, 10.2 | 42 | 92, 19, 27 (latest 2026-09-29) |
| Gulf of Mannar offshore | 78.6, 8.4, 79.2, 8.9 | 20 | 92 (latest 2026-09-22) |

Takeaway: Palk Strait has the densest recent coverage (three orbits), so it is the best place to
look for repeat passes and dark candidates.

**Finding: GFW has a SAR-detection dataset.** `public-global-sar-presence:latest` works through the
same `/4wings/report` endpoint we already use. Records come back per grid cell with
`detections`, `entryTimestamp`/`exitTimestamp`, lat/lon, and — for detections GFW could not match
to an AIS vessel — **empty `mmsi`/`shipName`**. That is GFW's own "unmatched to AIS" signal, i.e.
an independent label we can compare our detector against.

(Details of the follow-up measurement are in the next entry.)

---

## 2026-10-03 — Step 2: probing GFW's SAR-detection dataset (`public-global-sar-presence:latest`)

**Goal:** find out whether GFW gives us independent "SAR detection, matched/unmatched to AIS" labels
to validate our detector against, and whether any exist over the Palk Strait / Gulf of Mannar.

**How:** same `POST /v3/4wings/report` call as `src/fetch_ais.py`, but with
`datasets[0]=public-global-sar-presence:latest` and `group-by=VESSEL_ID`. Records with empty
`mmsi` are detections GFW could not match to an AIS vessel. (Scripts were throwaway, in scratchpad.)

**Results (January 2026 unless noted):**

| Box | Monthly records | With MMSI (AIS-matched) | No MMSI (unmatched) |
|---|---|---|---|
| Tuticorin anchorage | 10 | 9 | 1 |
| Palk Strait (79.3–79.9E, 9.6–10.2N) | 40 | 0 | **40** |
| Gulf of Mannar (78.0–79.5E, 8.3–9.2N) | 72 | 20 | **52** |

Single pass, 2026-01-18 (queried as range `01-17,01-19`; a one-day range `01-18,01-18` returns
nothing — same quirk as the AIS presence product):

| Box | GFW SAR detections | AIS-matched | Unmatched |
|---|---|---|---|
| Tuticorin | 3 | 3 | 0 |
| Palk Strait | 1 | 0 | 1 |
| Gulf of Mannar | 10 | 5 | 5 |

**What this means:**
- The dataset works and gives us an **independent label source**. This is the validation route.
- **GFW itself reports unmatched SAR detections in the Gulf of Mannar and Palk Strait.** So
  AIS-silent candidates plausibly exist there. Not the same as "confirmed dark vessels" (see caveats).
- **Our Tuticorin run found 17 detections on 2026-01-18; GFW found 3.** Our detector is far less
  selective: most of our 17 are known false positives (linear feature, speckle — see
  `results/MILESTONE1_FINDINGS.md`). That is a real precision number we can quote honestly.
- The pass timestamps (`entryTimestamp` ≈ 00:33 UTC) line up with our Sentinel-1 pass time.

**Caveats (do not overstate):**
- "No MMSI" means GFW did not match it to an AIS vessel in *its* AIS data. That data has gaps in
  Indian waters, and many small fishing boats never carry AIS. So unmatched ≠ intentionally dark.
- GFW's detections are grid-snapped (~0.01°, ~1 km), not exact positions.
- GFW's own detector also has misses and false alarms; it is a reference, not perfect ground truth.

**Next:** build a validation script that, for each Sentinel-1 pass, runs our detector over the
same area and counts agreement with GFW's detections (found / missed / extra).

---

## 2026-10-03 — Step 3: validation script vs GFW (`src/validate_vs_gfw.py`)

**New files:** `src/validate_vs_gfw.py`, `results/VALIDATION_VS_GFW.md`, `data/validation/` (cached GFW
JSON, tiles `*.tif` [gitignored], `validation_rows.json`, `run.log`). No existing code was modified.

**What the script does:** for 3 passes (2026-01-06/18/30) it fetches GFW's SAR detections for the Gulf of
Mannar + Palk Strait box, samples 10 per date, downloads a ~9 km Sentinel-1 tile around each from Earth
Engine, runs our *unmodified* land mask + CA-CFAR detector, and records whether we find a detection within
1500 m. Also counts our "extra" detections per 100 km².

**Problems hit and fixes:**
1. First run tried all GFW detections (52 on the first date alone, ~1–2 min per tile) → would have taken
   hours. Killed it; added `MAX_PER_DATE = 10` sampling spread evenly over the list.
2. Earth Engine dropped a download connection and crashed the batch → added `download_with_retry`
   (3 attempts, then skip that tile).

**Result:** 24/30 (80%) of GFW detections also found by us; median offset ~500 m; ~6.3 extra detections
per 100 km². Full numbers and caveats in `results/VALIDATION_VS_GFW.md`.

**Takeaways for the pitch:** we can quote "agrees with GFW's independent detector on 80% of a 30-ship sample"
(not "80% recall"). Our weak point remains false alarms, which the persistence/confidence work (next) targets.
Also: GFW listed 143 detections across three passes of this region, ~70% without an AIS match, so there
is plenty of unmatched traffic to examine for the dark-vessel search.

**Next:** (a) persistence/confidence score to cut false alarms, (b) the web app skeleton (FastAPI + React)
with the "all passes on one screen + alerts list" design.

---

## 2026-10-03 — Step 4: AISstream live-AIS test (result: NOT usable for India)

**Goal:** record exact-time AIS around a Sentinel-1 pass to fix GFW's coarse timing.

**What I did:** wrote a recorder (`src/record_ais.py`) and tested the key (stored in `.env` as
`AISSTREAM_API_KEY`, never printed).

**Findings:**
- Key and service work: a Singapore control box returned 18 ships in 45 s; a global box returned 677 ships.
- **Our box (Tuticorin / Gulf of Mannar / Palk Strait, 8.0–10.6N, 77.8–80.4E): 0 ship messages in 2.5 min.**
- **Whole India + Sri Lanka seas (5–24N, 68–90E): 0 messages in 45 s.** Kochi, Chennai and Mumbai/Gujarat
  coastal boxes: 0 messages in 40 s each.
- A 2-minute listen over the northern Indian Ocean (0–32N, 55–105E) returned 59 position messages /
  53 ships, **none inside 0–30N, 60–100E** — all came from the corners (outside India).
- (First attempt opened 6 connections at once and got HTTP 429 rate-limited — my error, not a coverage issue.)

**Conclusion:** AISstream's volunteer terrestrial network has essentially no coverage of Indian waters.
Short samples (not a long-term survey), but consistent across 5 separate tests.

**Changes:** deleted `src/record_ais.py`, `data/ais_live/`, and the matching `.gitignore` lines — no
point keeping a recorder that records nothing. No other files touched.

**Pitch value:** this is a concrete, tested reason why an official feed is needed. "We tested the best free
live AIS source; it has no coverage of Indian waters. India's Coast Guard / NIC AIS network is the missing
piece." Use this in Q&A and the partnership slide.

**Next:** back to the plan: persistence/confidence score, then the web app.

---

## 2026-10-03 — Step 5: persistence + confidence score (`src/confidence.py`)

**New files:** `src/confidence.py`, `data/scored_detections.geojson` (all 70 Tuticorin detections over 4 passes,
each with `confidence_class`, `confidence` 0–100, `reasons`). No existing file modified.

**Key discovery (from the data, before writing code):** persistence alone does NOT mean "fixed structure".
- The pipeline/cable fragments (#5–12) reappear on 3–4 passes, but the AIS ship they "match" is a *different
  vessel each pass* (OCEAN POISE → HONG FU → GN RUBY → OCEAN ALLIANCE): a fixed object that is merely near
  whichever ship anchors beside it that day. So the Round 1 "49 of 70 matched" figure is inflated by these.
- NEREUS PROGRESS also reappears on 4 passes, but matches the *same* vessel every time → ship at anchor.
- So the rule is persistence **plus AIS-identity consistency**.

**Classes:** ANCHORED_VESSEL (3+ passes, same MMSI), FIXED_OBJECT (3+ passes, MMSI keeps changing/absent),
VESSEL_CANDIDATE (single pass, ≥15 dB, ≥8 px, AIS nearby), DARK_CANDIDATE (same but NO AIS — needs review),
LOW_CONFIDENCE (bright but small/weak), CLUTTER (<10 dB). Thresholds are named constants at the top of the file.

**Result vs the Round 1 hand labels on 2026-01-18 (cluster radius 100 m):**
- #1 NEREUS → ANCHORED_VESSEL ✓ (resolves the old "unexplained" label)
- #3/#4 DMC JUPITER → VESSEL_CANDIDATE ✓
- #5–8, #11 pipeline fragments → FIXED_OBJECT ✓; #9, #10, #12 → LOW_CONFIDENCE (not wrongly endorsed)
- #13–17 speckle → all CLUTTER ✓
- #2 (hand label "unexplained", streak) → VESSEL_CANDIDATE: nearest AIS ship is SSI DIGNITY at 401 m, and GFW's
  own SAR detection list has SSI DIGNITY at 78.27E 8.75N on that pass. So #2 is probably a real ship and the
  Round 1 "streak/artifact" reading may have been wrong. Hypothesis only; not visually re-checked.
- Across all 70: ANCHORED 4, FIXED 18, VESSEL_CANDIDATE 10, LOW 12, CLUTTER 26, **DARK_CANDIDATE 0**.
  No detection was wrongly promoted to "vessel" from the known fixed-object or clutter sets.

**Honest caveats:**
- I chose the 100 m cluster radius while looking at the Jan 18 labels (75 m split real fragments, 125 m began
  over-merging). That is mild tuning on the same data we score against; ship classes were identical at all three
  radii. Needs to be tested on a different area before being called validated.
- Only 4 passes of one anchorage. Persistence logic cannot be applied to the Gulf of Mannar tiles (1 pass each).
- A *dark* ship anchored in one place for weeks would look like a FIXED_OBJECT. Persistence cannot tell these
  apart; shape and a known-infrastructure layer would be needed. Stated limitation, not hidden.
- Still zero dark candidates at Tuticorin, matching the Round 1 negative result.

**Next:** the web app skeleton (FastAPI + React), using `scored_detections.geojson` as its first data source.

---

## 2026-10-03 — Step 6: web app skeleton (FastAPI + React)

**New files:**
- `backend/main.py`, `backend/__init__.py` — FastAPI app. Reads `data/scored_*.geojson` at startup; no Earth Engine/GFW
  calls at request time (works offline). Endpoints: `/api/health`, `/api/areas`, `/api/detections` (+`/{id}`),
  `/api/clusters` (one row per location across passes), `/api/alerts` (ranked review queue; fixed objects and clutter
  excluded), `/api/crop/{id}`, `/api/overlay/tuticorin.png`, `/api/reviews` (GET/POST; saved to `data/reviews.json`).
  If `frontend/dist` exists it is served at `/`, so the whole app is one process.
- `frontend/` — Vite + React + TypeScript + MapLibre. Files in `frontend/src/`: `api.ts` (types/fetch), `classes.ts`
  (plain-language class names + colours), `MapView.tsx`, `AlertsPanel.tsx`, `DetailPanel.tsx`, `App.tsx`, `styles.css`.

**Changes to existing files:** `src/confidence.py` now also emits a stable `id` (`<date>-<NN>`) and `cluster_id` per
detection (needed by the app; the multi-date files had no `rank`). `.gitignore`: added `data/reviews.json`.

**What the UI shows:** map with one dot per physical location ("All passes combined"; dot size = number of passes
seen), or a single pass via the date chips; toggle-able class legend; optional SAR radar overlay; ranked review queue;
detail panel (class + plain explanation, confidence, radar crop, reasons, AIS check, same-location-on-other-passes
table, confirm/reject/unsure + note). Provenance line on every item.

**Problems hit and fixes:**
1. Shell heredoc batch silently failed (unmatched quote) — files re-created one by one with the Write tool.
2. TypeScript rejected the MapLibre colour expression built with a spread → cast with a comment.
3. Map canvas stayed at 400×300 (container measured before flex layout settled) → added a `ResizeObserver`.
(The browser pane viewport is 1024×768; screenshots are a scaled crop, so the page only looks cut off.)

**How to run (from project root):**
```
cd frontend && npm install && npm run build     # once, or after frontend changes
cd .. && python -m uvicorn backend.main:app --port 8000
# open http://localhost:8000
```
Frontend dev mode with hot reload: `npm run dev` in `frontend/` (port 5173, proxies `/api` to 8000).

**Verified:** `npm run build` (includes `tsc --noEmit`) passes; in the browser the map renders dots, the queue lists 20
locations, clicking NEREUS PROGRESS opens its detail with the radar crop and reasons.

**Known gaps (next):**
- "Gulf of Mannar / Palk Strait" is empty (0 detections). Next step: `src/build_gulf_candidates.py` turns the validation
  tiles into scored detections + crops, which is where real dark-vessel candidates (GFW "no AIS match" AND detected by
  us) will appear.
- Basemap tiles come from OpenStreetMap, so they need internet (Delhi fallback: hotspot, or bundle a basemap).
- Not tested: review POST from the UI, the date chips, the Radar image overlay, narrow/mobile layout.
- No automated tests yet. Reviews are a JSON file (fine for a prototype, not multi-user).

---

## 2026-10-03 — Step 7: Gulf of Mannar candidates (`src/build_gulf_candidates.py`) + a correction

**New:** `src/build_gulf_candidates.py` → `data/scored_gulf.geojson` (24 scored detections) and 24 radar crops in
`data/validation/crops/` (120 px window, 4× upscaled, red circle on the detection). The backend picks them up
automatically as the "Gulf of Mannar / Palk Strait" area. Built only from the 30-detection validation sample.

**What it does:** for each GFW detection our detector also found, keep OUR nearest blob, mark it matched/unmatched using
GFW's AIS flag, score it with `confidence.py`, save a crop. The 6 GFW detections we missed are left out of the app.

**Result:** 7 VESSEL_CANDIDATE (all AIS-matched), 13 CLUTTER, **4 DARK_CANDIDATE**:
`2026-01-30-G04` (79.210E 9.077N, 13.4 dB), `2026-01-30-G06` (78.867E 9.128N, 10.8 dB),
`2026-01-06-G07` (79.398E 9.728N, 10.0 dB), `2026-01-06-G08` (79.557E 10.106N, 12.7 dB).

**I looked at the four crops myself:** each shows a compact bright return with a vertical streak trailing along the
satellite's track direction (similar to Round 1's detection #2). Plausibly a moving ship, but a streak can also be a
radar artefact. **Not verified.** They are candidates for analyst review, not findings.

**Design change to `src/confidence.py` (decided after seeing the crops — treat as re-testable, not validated):**
`score_detection` has a new `corroborated_by` argument. When an independent detector (GFW's SAR detections) reports the
same object, the contrast bar for vessel/dark candidate drops from 15 dB to 10 dB. Tuticorin results are unchanged
(no corroboration data there). Without this, the four above would have been LOW_CONFIDENCE.

**Correction to Step 3 (my earlier headline was too generous):** the "80% found" figure is inflated by chance — our detector
fires ~6.3 times per 100 km², so a 1.5 km circle holds a random detection ~36% of the time. Looking at what was found:
all 7 AIS-matched GFW ships were found with strong contrast (7/8 = 88%), but of the 22 GFW detections with no AIS match we
found 17, of which 13 are noise-level (6–9 dB) and only 4 are clear. So: **we see GFW's AIS-matched ships reliably, and
about 1 in 5 of its AIS-less detections.** `results/VALIDATION_VS_GFW.md` now opens with this correction. Do not quote
"80% recall" to the jury.

**Why that is still a useful story:** the AIS-less detections GFW reports in the Gulf of Mannar are mostly very weak —
consistent with small boats at the edge of Sentinel-1's 10 m resolution. That is the honest case for higher-resolution
SAR (NISAR/RISAT/commercial) in the pitch.

**Caveats:** AIS status comes from GFW (patchy in Indian waters; many small boats carry no AIS), so "no AIS" ≠ switched
off. Only 30 of GFW's 143 detections were tested; the full list may hold more candidates (next possible step: run all of
the ~110 AIS-less ones, ~1–2 min each, could be parallelised).

**Next:** the AIS-off demo on NEREUS PROGRESS (clearly labelled simulation).

---

## 2026-10-03 — Step 8: AIS-off simulation (backend + UI) and Gulf of Mannar in the app

**Changes:**
- `backend/main.py`: new `sim=<mmsi>` query parameter on `/api/detections`, `/api/detections/{id}`, `/api/clusters`,
  `/api/alerts`, plus `/api/simulation/vessels`. `_simulated()` takes the real Tuticorin detections, sets the ones whose
  nearest AIS vessel is that MMSI to UNMATCHED, and re-scores everything with `confidence.score_all` (so cross-pass
  persistence is recomputed too). Affected detections get `simulated: true` and a provenance line starting "SIMULATION".
- `frontend/src/*`: a "Simulate: switch off AIS of <vessel>" dropdown (Tuticorin only), an amber SIMULATION banner with an
  Exit button, a gold ring on dots that changed, and the simulation flag carried into the detail panel.
- `src/build_gulf_candidates.py`: now skips GFW points inside the Tuticorin box (they were double-counted as Gulf
  detections). Gulf set is now 19 detections: 4 DARK_CANDIDATE, 2 VESSEL_CANDIDATE, 13 CLUTTER.

**Simulation results (what the system does when a ship's AIS is removed):**
- **DMC JUPITER (seen on one pass):** its two detections (#3/#4 on 2026-01-18, 18.4 and 17.9 dB) become DARK_CANDIDATE
  (76 and 70). This is the clean "we flagged an AIS-silent ship" demo.
- **NEREUS PROGRESS (same spot on all 4 passes):** it is NOT flagged. With no AIS identity it is reclassified FIXED_OBJECT
  and drops out of the review queue. **This is a real blind spot** (a dark ship parked in one place for weeks looks like a
  fixed object). The simulation exposes it rather than hiding it; say so in the pitch before a juror finds it.

**Verified in the browser:** DMC JUPITER scenario puts two red dark-vessel candidates on top of the queue under the banner;
the Gulf of Mannar view lists the 4 dark candidates first; the detail panel shows crop, reasons ("independently reported by
GFW SAR detection", "no AIS vessel within match radius") and provenance.

**Not tested yet:** the NEREUS scenario in the UI, the date chips in the Gulf view, review buttons end-to-end, mobile layout.

**Next options:** (a) run the detector on all ~110 AIS-less GFW detections (not just the 30-sample) to see how many
dark candidates there really are; (b) offline basemap for Delhi; (c) pitch material (deck, demo script, Q&A); (d) NISAR
feasibility check; (e) the small ML classifier (stretch).

---

## 2026-10-03 — Step 9: full search over all 143 GFW SAR detections

**Changes:** `src/validate_vs_gfw.py` gained `--all` (test every GFW detection) and `--workers` (parallel Earth Engine
downloads via a thread pool; the sequential version would have taken hours). The earlier 30-sample rows are kept in
`data/validation/validation_rows_sample30.json`. Log of the run: `data/validation/run_all.log`. 143 tiles, none skipped,
~20 min. Then `src/build_gulf_candidates.py` was re-run: `data/scored_gulf.geojson` now has 95 detections.

**Numbers (full set, 3 passes: 2026-01-06/18/30, Gulf of Mannar + Palk Strait):**
- GFW SAR detections: 143 (22 with an AIS match, **121 without**).
- Found by us within 1500 m: 111 (78%) — AIS-matched 20/22 (91%), AIS-less 91/121 (75%).
- Extra detections: 755 over 11,349 km² = 6.7 per 100 km² (chance of a random hit in a 1.5 km circle ≈ 37%).
- After scoring (Tuticorin box excluded): CLUTTER 62, VESSEL_CANDIDATE 9, **DARK_CANDIDATE 23**, LOW_CONFIDENCE 1.
- All 23 dark candidates are single-pass (none repeats across passes), 10.0–16.1 dB, 8–31 px.

**Visual check (me, quick, unverified):** contact sheet of all 23 = `results/DARK_CANDIDATES_CONTACT_SHEET.png`. About 16 show a
clear compact bright target (often with an along-track streak); about 6 are doubtful (e.g. 2026-01-06-G03 shows nothing
but noise at 11 dB; 2026-01-30-G27 is faint); 3 (2026-01-06-G09, 2026-01-18-G12, 2026-01-30-G05) sit at the end of bright
streaks and may be radar artefacts (azimuth ambiguity) of a nearby stronger target, not ships.

**Hand-off for teammates:** `results/dark_candidates_for_review.csv` (blank verdict/name/notes columns) plus the contact sheet.
Ask them to mark each ship / not ship / unsure. That gives us the only human ground truth we have for the dark search.

**Caveats to keep in the pitch:**
- 23 candidates ≠ 23 dark vessels. "No AIS" is GFW's AIS database (patchy in Indian waters); many small boats never carry AIS.
- The 10 dB bar for GFW-corroborated objects was chosen after seeing crops (see Step 7). Re-test after human labels arrive.
- Several candidates are clustered within ~1 km (e.g. around 79.55E 10.10N; around 79.40E 9.61–9.73N): possibly fishing
  fleets, possibly one object counted from several GFW cells. Not yet resolved.
- Single pass each, so persistence cannot help; only GFW's agreement and the radar crop support them.

**Next:** teammates label the 23; then re-score with their labels (tests whether the 10 dB rule is sound).

---

## 2026-10-03 — Step 10: objective enrichment test (replaces eyeballing)

**Why:** the user pointed out that the team cannot reliably judge "ship / not ship" by eye (neither can I; my quick visual read in
Step 9 is not evidence). So the human-labelling hand-off in Step 9 is **withdrawn**. `results/dark_candidates_for_review.csv` and the
contact sheet remain only as a convenience for a real analyst; no team labelling is needed.

**New:** `src/enrichment_test.py`. For every tile (centred on a GFW detection) it counts "strong" blobs (>=10 dB, >=8 px) within
1500 m of the centre and compares with the number expected by chance from the density of strong blobs elsewhere in the same
tile (Poisson test). Tuticorin tiles excluded. No human judgement involved.

**Result:**
- GFW detections WITH an AIS match (control, 11 tiles): observed 11, expected 1.5 → **7.4x**, p = 5e-07.
- GFW detections WITHOUT an AIS match (119 tiles): observed 58, expected 7.9 → **7.4x**, p = 2e-30.
- Reading: AIS-less GFW detections are accompanied by strong ship-sized radar blobs about as strongly as AIS-matched ships are. About
  8 of the 58 (14%) are expected to be chance, so roughly 6 in 7 of our strong-blob "dark candidates" correspond to something real
  in the radar, and about 3 of the 23 listed candidates are probably noise. We cannot say which.

**What this does NOT establish (say so if asked):**
- That the object is a ship, not another bright thing (buoy, platform, rain cell). It shows the bright return is real and that GFW's
  independent detector also fired there.
- That the ship is "dark" on purpose: AIS status is from GFW's database, which is patchy over India, and many small boats carry no AIS.
- Independence: our detector and GFW's use the same Sentinel-1 scenes, so this is agreement between two algorithms on one
  image, not two sensors. Blobs cluster (fleets), so the Poisson p-values are optimistic; the 7.4x ratio is the robust number.

**Pitch wording that is true:** "Strong ship-like radar returns occur 7x more often than chance at the places GFW reports AIS-less
detections, the same enrichment as for AIS-matched ships. We flag them for analyst review; confirming them needs a proper AIS
feed and analyst or optical verification."

**Next:** offline basemap for Delhi; then pitch material; stretch: synthetic-ship injection test to measure detection sensitivity by size.
