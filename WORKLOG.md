# WORKLOG — Scientific & detection-logic audit

Chronological log of the audit requested 2026-10-07. Baseline is git
`549ed4a` (clean tree). Full per-item status lives in [AUDIT_CHECKLIST.md](AUDIT_CHECKLIST.md);
the narrative report is [FINAL_REPORT.md](FINAL_REPORT.md).

Terminology used throughout:
- **BUG** — code does something provably wrong; fix applied in place.
- **SEMANTIC** — code is not wrong but its label/claim overstates evidence; relabelled.
- **EXPERIMENT** — a new algorithm/measurement; kept separately identifiable, not wired
  into the default pipeline unless measured to help.
- **BLOCKED** — cannot be validated without data/labels we do not have; scoped and deferred.

---

## 2026-10-07

### Baseline preservation
- Recorded git rev, Python 3.13.2, installed dep versions, and sha256 of all
  tracked scored outputs + raw per-pass detections in
  [audit/baseline_manifest.json](audit/baseline_manifest.json).
- Raw SAR `.tif` are gitignored (large). Local cache: 577 recent tiles, 143
  validation tiles, 3 multi-date tiles. JRC occurrence caches present for 140
  recent tiles only; none for validation/multi-date — so a full **v3 re-run** of
  the Gulf and Tuticorin sets needs network (GEE JRC fetch), but **semantic
  re-scoring from the frozen detections is fully offline**.
- Baseline itself is the committed tree at `549ed4a`; old detector/v3 behaviour
  is reproducible by checking it out. No large files duplicated.

### Verification pass (code read before any change)
Confirmed against source — see AUDIT_CHECKLIST for file:line evidence:
- §1 `_fit_shape_alpha` trims (not censors) the top 1%, MLE-fits the remainder,
  extrapolates to the 1e-5 quantile → achieved P_fa ≠ nominal. **Confirmed.**
- §1 clutter model is fit on **all sea pixels** (`detect_v3.py` norm = intensity[sea]/lm[sea]),
  not the pure-clutter superpixels the docstring describes. **Confirmed doc/impl mismatch.**
- §2 `binary_opening` runs before labelling, so `MIN_BLOB_PX=3` is partly illusory;
  `confidence.MIN_AREA_PX=8` gates independently. **Confirmed.**
- §3/§9 GFW SAR agreement is labelled "independently reported" and treated as an
  independent confirmation; it is the same Sentinel-1 imagery, different algorithm,
  and is **not AIS**. **Confirmed overstatement.**
- §4 persistence-without-consistent-MMSI → `FIXED_OBJECT` (confident infrastructure
  label) although the docstring already admits a dark ship at anchor is
  indistinguishable. **Confirmed overstatement.**
- §5 `cluster_across_passes` is single-linkage with no diameter cap → transitive
  chains possible. **Confirmed.**
- §6 JRC fetched clipped to tile (no pad); shore distance transform runs inside the
  tile only; JRC failure silently falls back to Natural-Earth-only with no record;
  docstring claims JRC "catches the Gulf of Mannar reefs" but our probe showed
  occurrence=99 at the reef FPs. **Confirmed (several).**
- §7 VH/VV "local" ratio is actually a tile-wide median; only `vh.shape==db.shape`
  is checked, not CRS/transform alignment. **Confirmed.**
- §8 `_local_clutter_mean` does `intensity * clutter.astype(float)`; `NaN*0=NaN`, and
  `uniform_filter` then smears each NaN across an outer-window neighbourhood, where
  `detect_blobs_v3` replaces it with the global median → wrong local background.
  **Confirmed latent bug** (fires only if a tile has non-finite pixels; `build_sea_mask`
  shows non-finite pixels do occur).
- §13 `requirements.txt` lists `folium/pandas/streamlit/streamlit-folium` — none of
  the actual stack. **Confirmed stale.**

### Work done
**Semantics (shared core) — §3, §4, §5, §9:** rewrote `confidence.py`:
- Complete-linkage clustering with `CLUSTER_DIAMETER_M` cap replaces single-linkage
  (no more transitive chains). Test: `tests/test_audit_fixes.py::test_complete_linkage_blocks_transitive_chain`.
- New `PERSISTENT_UNIDENTIFIED` class for persistent + no-consistent-identity targets;
  `FIXED_OBJECT` now requires explicit `context_fixed` evidence, never inferred from
  persistence. Propagated to `api.ts`, `classes.ts`, `backend/main.py` ALERT_PRIORITY.
- GFW association kept distinct from AIS evidence via `ais_source` ("our_ais" vs
  "gfw_reported") and `_ais_evidence()`; GFW agreement labelled shared-sensor, not
  independent. Wired into `run_recent.py` (+ many-to-one `ambiguous` flag),
  `build_gulf_candidates.py`, `backend/main.py` (batch + Run Analysis).
- `score_basis` = "heuristic evidence/ranking score" on every detection (§9.1).
- Regenerated Tuticorin offline (`python src/confidence.py`): 15 `FIXED_OBJECT` ->
  `PERSISTENT_UNIDENTIFIED`, `ANCHORED_VESSEL`x4 preserved.
- Re-scored frozen recent+gulf detections offline (`audit/rescore_historical.py`,
  a documented semantic adaptation, NOT a detector re-run). recent: 67
  `FIXED_OBJECT`->`PERSISTENT_UNIDENTIFIED` (rest identical). gulf: `DARK` 23->16 —
  the 8 reclassified are 10.0-11.7 dB, below the already-committed 12 dB bar that
  gulf had never been regenerated under (consistency, not a new tuning or detector
  change). Baselines saved to `audit/results/*_baseline_549ed4a.json`.

**Detector corrections — §1, §6, §7, §8:** rewrote the affected `detect_v3.py`
functions (version tag `v3.1-audit`):
- §8.1 `_local_clutter_mean` finite-masks before the box filter (no NaN propagation);
  §1.4 requires `MIN_TRAIN_PX=12` local clutter pixels or marks the pixel unsupported.
- §1.3 `_fit_shape_alpha` returns a diagnostics dict (dist, params, alpha, n_samples,
  trim quantile, nominal pfa); PFA documented as NOMINAL.
- §8.2 `detect_blobs_v3`/`run_detector_v3` return `(dets, diag)` with explicit
  `status` ∈ {ok, degraded, insufficient_data, no_sea} + notes; wired into `run_recent.py`.
- §6.2 `build_sea_mask` builds NE+JRC land and the shore-distance transform on a grid
  padded by the shore buffer, then crops — off-tile land now pushes shore distance in.
- §6.3 distance in true metres via `_pixel_metres` (metric CRS direct, geographic
  converted); §6.1 excluded-shore km² recorded; §6.4 JRC used/fallback recorded.
- §7.2 VH used only if co-registered with VV (shape+CRS+transform), else skipped with
  reason; §7.1 tile-wide ratio relabelled honestly (local is an experiment).
- Verified on real cached tiles: new mask matches baseline to <0.3% (diff = correct
  off-tile shore exclusion at edges); full path produces detections with gengamma fit,
  0% fallback, VH co-registration gating. Committed `scored_recent` stays the FROZEN
  baseline-detector output; a corrected-detector re-run needs network (JRC) — pending.
- 11/11 regression tests pass (`tests/test_audit_fixes.py`).

**Experiments:**
- §1.5 `audit/experiments/calibration.py`: Gamma(2,0.5), 5 seeds, 5M held-out.
  trim+gengamma (default) achieves **1.45e-4 = 14.5x nominal 1e-5** (reproduces the
  reported ~1.7e-4). Same fit WITHOUT trim -> 1.3x; gamma no-trim -> 1.0x. Conclusion:
  the top-1% trim (there to exclude targets) is what breaks P_fa calibration; this is
  a trade-off, not a free fix. Nominal label is correct; result in `audit/results/calibration.json`.

### Work done (cont.)
- §11/§1 ablation on 37 tiles / 2257 km²: mask excludes 401 km² (15%, 181 km² buffer);
  GG-CFAR pixel exceedance 6.35e-4 (~63x nominal); binary_opening removes 77.7% of
  gated detections. `audit/results/ablation.json`.
- §2 morphology injection: opening erases all thin/≤8px targets (100%->0% recovery);
  only solid 3x3+ survive. Effective floor ~9px compact. `audit/results/morphology.json`.
- §12 enrichment rewritten: actual valid-water area in/out, tile-level block bootstrap,
  radius/contrast sweep, population-association framing (no precision). 119 tiles,
  7.5x CI[4.3,13.2] @1500m/12dB. `audit/results/enrichment.json`.
- §13.1 requirements.txt replaced with the real stack. §13.2/§13.3 top-level provenance
  block + per-detection detector/mask/vh fields; explicit status distinguishes
  zero/failed/partial.
- §9.1 + §3 UI copy: "evidence score" + heuristic note; AIS&GFW evidence section
  distinguishes our match / gfw_reported / unknown; GFW labelled shared-sensor.
- §10.1 live-analysis acquisition discovery: queries actual S1 dates (EE), falls back
  to 12-day stepping. Needs live EE to exercise.
- Frontend: `tsc --noEmit` clean, `vite build` clean. Backend restarted on fresh data;
  `/api/areas`, `/api/alerts`, `/api/detections/{id}` verified (PERSISTENT_UNIDENTIFIED
  surfaced as alert; score_basis/ais_evidence/gfw_association present).
- 11/11 regression tests pass; all pipeline + backend modules import/compile.

### Follow-up: fixed-object evidence (post-audit, same day)
Review flagged a real defect in the §4 relabel: `FIXED_OBJECT` was gated behind
`det["context_fixed"]`, which **nothing in the pipeline ever set**, so the class was
unreachable and every persistent target fell to `PERSISTENT_UNIDENTIFIED` -- including
obvious fixed features. Fix gives `FIXED_OBJECT` real, positive evidence paths, keeping
the asymmetry (a positive signal promotes; ambiguity always falls back to reviewable):
- **Position-stability** (`FIXED_POSITION_SPREAD_M=15`): a persistent cluster pinned to
  one spot across passes -> `FIXED_OBJECT` (`fixed_evidence="position_stable"`). Labelled
  a heuristic, not charted: a short-scope/current-pinned anchored vessel can also stay
  within the radius (reason string says so). Recovered 4 pinned Tuticorin clusters.
- **Collinear-chain geometry** (`find_chain_members`): a long, thin, persistent run of
  clusters (perp RMS <= 60 m, aspect >= 4, members linked <= 1500 m, >= 4 members) is
  reef/shoal/causeway geometry, not a vessel formation -> `FIXED_OBJECT`
  (`fixed_evidence="chain_geometry"`). A compact anchorage *blob* has low aspect and is
  NOT flagged, so anchored vessels are not swept in (tested).
- Measured on the frozen recent set: the two Gulf-of-Mannar arcs (a 2.9 km, 10:1 line,
  perp RMS 42 m) = 64 detections -> `FIXED_OBJECT` via chain geometry; 3 (the isolated
  southern cluster) stay `PERSISTENT_UNIDENTIFIED` (correctly still reviewable). Tuticorin
  chain rule flagged nothing (anchorage is a blob). Position-stability alone recovered 0
  on recent (tightest cluster already spreads 16 m -- reef returns wander with tide/sea
  state), which is exactly why the chain rule, not a looser spread threshold, is the right
  tool; the threshold was NOT loosened to force the relabel.
- Wired into all cluster+score sites: `confidence.score_all`, `audit/rescore_historical.py`,
  `src/run_recent.py`, `backend/main.py` live path. `build_gulf_candidates.py` left
  untouched (single-date -> no cross-pass persistence, chains cannot form).
- UI: `fixed_evidence` added to `api.ts` + a basis line in `DetailPanel` (charted /
  chain geometry / position-stable). 13/13 tests pass; tsc + vite build clean; backend
  restarted on regenerated data.
- Still pending (unchanged): an authoritative *charted* reef/infrastructure layer remains
  the gold standard; the chain heuristic and position-stability are in-data proxies for it.

### Follow-up: review-round fixes (live detector, labels, suspected-fixed, coverage)
Four reviewer points, verified in code then fixed:
1. **Live "Run Analysis" used the Round 1 detector.** `vgfw.run_detector_on_tile`
   (docstring: "Unmodified Round 1 pipeline", old `detect.detect_blobs` + old mask) was
   driving the live path, contradicting the write-up. Swapped to
   `detect_v3.run_detector_v3` (the corrected v3.1-audit detector) in `backend/main.py`.
2. **Labels now follow the evidence.** `DARK_CANDIDATE` now requires *established*
   no-AIS evidence (`unmatched` or `gfw_reported_no_ais`). A strong target whose AIS was
   never established (`ais_evidence="unknown"`) is the new **`UNVERIFIED_TARGET`**, not
   "dark". The `DARK_CANDIDATE` UI blurb no longer claims "no AIS vessel is nearby"
   unconditionally -- it states the actual basis. Tests: `test_unknown_ais_is_unverified_not_dark`,
   `test_gfw_reported_no_ais_is_dark_candidate`.
3. **Heuristic fixed-object shortcut corrected.** The chain-geometry / position-stable
   promotions now yield **`SUSPECTED_FIXED`**, not `FIXED_OBJECT`: they **stay in the
   review queue** (added to `ALERT_PRIORITY`, deprioritized below dark/unverified/
   persistent), are rendered as a distinct "Suspected fixed / reef" class, and are
   labelled *suspected*, never *charted*/*confirmed*. `FIXED_OBJECT` is now charted-only
   (and still queue-excluded). Effect: recent 64 -> `SUSPECTED_FIXED` (was FIXED_OBJECT),
   Tuticorin 4 -> `SUSPECTED_FIXED`. Tests updated accordingly.
4. **Unavailable data vs empty sea.** The live path now tracks a `coverage` record
   (requested / no_scene / download_failed / analyzed / no_sea / degraded), logs each
   unavailable tile, and sets `data_unavailable` when nothing could be retrieved. The UI
   shows an amber "No data available" notice (not a green success) in that case and prints
   the coverage summary, so a failed retrieval is never reported as an empty-sea result.
- 15/15 tests pass; tsc + vite build clean; backend restarted (live queue now: 17 dark,
  1 persistent, 16 suspected-fixed, 4 vessel, 4 low). Live detector swap verified by import
  + wiring; full live run needs EE/GFW auth to exercise end-to-end.

### Follow-up: crop brightness consistency + data-availability honesty
1. **One fixed crop brightness window everywhere.** The Tuticorin and Gulf saved
   crops used a per-crop percentile [2,98] stretch (each crop rescaled on its own
   min/max, so a faint smudge looked as bright as a ship and crops were not
   comparable). Created `src/crops.py` as the single canonical renderer at the fixed
   **[-23, +3] dB** window and routed `run_recent`, `build_gulf_candidates` and the
   backend live-analysis crop through it (recent crops already used this window).
   Regenerated the saved crops from source imagery with `audit/regen_crops.py`:
   **17 Tuticorin (2026-01-18) from `data/sar_vv_clip.tif`, 95 Gulf (Jan 2026) from
   `data/validation/{date}_*.tif`** -- no missing inputs. Removed the now-dead
   per-module crop code/imports/constants.
2. **GFW request failure vs successful empty response.** The live path conflated a
   GFW request that *failed* with one that *succeeded with zero records*. Now tracked
   separately (`gfw_ok` per date): a failure marks that date **unperformed** (unknown),
   an empty success means **no SAR detections there**. Result carries `gfw_failed_dates`,
   `partial` (some dates unfetched) and `data_unavailable` (all dates failed / nothing
   retrieved); the UI shows an amber "Analysis partial" / "No data available" notice
   instead of a green success with zeros.
3. **No guessed acquisition dates.** Acquisition discovery no longer falls back to a
   fabricated 12-day cadence. If EE discovery **fails** -> the run errors ("no analysis
   performed, no dates guessed"); if it **returns no scenes** -> the run completes as
   `data_unavailable` with a clear note. Neither invents dates.
- 15/15 tests pass; tsc + vite build clean; backend restarted; crop endpoint serves the
  regenerated PNGs. Live-path items (2, 3) need EE/GFW auth to exercise end-to-end.

### Deliverables
- Implemented fixes + tests: `src/confidence.py`, `src/detect_v3.py`, `src/run_recent.py`,
  `src/build_gulf_candidates.py`, `src/enrichment_test.py`, `backend/main.py`,
  `frontend/src/{api,classes,DetailPanel}.*`, `tests/test_audit_fixes.py`.
- Experiments (separately identifiable): `audit/experiments/{calibration,morphology,ablation}.py`.
- Baseline + manifests: `audit/baseline_manifest.json`, `audit/results/*_baseline_549ed4a.json`.
- Reports: `FINAL_REPORT.md`, `AUDIT_CHECKLIST.md`, README updated.

### Not committed
Per instruction, nothing has been committed or pushed. Committed scored_recent/gulf
detection geometry remains the FROZEN baseline-detector output; only scoring semantics
were re-applied (documented as a relabel). Corrected-detector regeneration of those two
sets is BLOCKED on a network JRC fetch (see FINAL_REPORT §5).
