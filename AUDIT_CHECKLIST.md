# Audit checklist (final)

finding → evidence → correction/experiment → verification → remaining limitation → status.
Baseline = git `549ed4a`. Status ∈ {DONE, EXPERIMENT, PARTIAL, BLOCKED}. Full narrative:
[FINAL_REPORT.md](FINAL_REPORT.md). Classification: **BUG** (fixed in place),
**SEMANTIC** (relabel), **EXPERIMENT** (separately identifiable), **BLOCKED** (needs labels/data).

## §1 GG-CFAR fitting & false-alarm calibration
- **1.1** trim≠censoring, nominal≠achieved P_fa (`detect_v3.py:_fit_shape_alpha`). → PFA documented *nominal* everywhere; synthetic calibration added. → `calibration.py` shows 14.5× inflation. → No field FAR without labels. **DONE (relabel) + EXPERIMENT.**
- **1.2** fit on all-sea vs doc "pure-clutter superpixels". → Docstring reconciled to match impl (all-sea + trim) with selection-bias/heterogeneity caveats; superpixel-only fit left as experiment. → code read. → choosing default needs labels. **DONE (doc) / EXPERIMENT.**
- **1.3** global-median fallback unrecorded. → `_fit_shape_alpha` returns diag (dist/params/alpha/n/trim/pfa); `local_bg_fallback_frac` recorded. → tests `test_fit_*`. → — **DONE.**
- **1.4** no min training support. → `MIN_TRAIN_PX=12`; ring below it marked unsupported. → `test_local_mean_insufficient_support`. → — **DONE.**
- **1.5** synthetic counterexample. → reproduced across 5 seeds. → `audit/results/calibration.json` (1.45e-4). → synthetic, not field. **DONE (EXPERIMENT).**

## §2 Morphology vs small-vessel detection
- **2.1** `binary_opening` before labelling erases thin ≤3px targets. → quantified. → `morphology.json`: lines/L/2×2/pairs 100%→0% recovery with opening; ablation: opening removes 77.7% of dets. → — **DONE (measured).**
- **2.2** detector 3px vs `confidence.MIN_AREA_PX=8`. → documented effective floor ~9px compact in code + README + report. → — → — **DONE.**
- **2.3** compact-target peak path. → identified; ablation exposes opening on/off. → — → needs labels to adopt. **EXPERIMENT/BLOCKED.**

## §3 GFW SAR association vs AIS evidence
- **3.1** detection inherited AIS identity from nearest GFW record. → `gfw_association` + `ais_evidence` + `ais_source` separated. → API shows `ais_evidence=gfw_reported_no_ais` etc. → GFW coarse/aggregated. **DONE (SEMANTIC).**
- **3.2** "independently reported" overstatement. → reworded to shared-sensor agreement everywhere. → `test_gfw_reported_ais_is_not_called_our_match`; API reasons. → — **DONE.**
- **3.3** many-to-one GFW record. → `ambiguous` flag (run_recent + rescore). → code. → no per-record timestamps cached. **DONE.**
- **3.4** missing/failed/absent collapsed to UNMATCHED. → `unknown` (no record/not queried) vs `gfw_reported_no_ais` (queried) vs `none`. → `_ais_evidence`. → — **DONE.**

## §4 Persistence interpretation
- **4.1** persistence→confident FIXED_OBJECT. → `PERSISTENT_UNIDENTIFIED` by default; FIXED_OBJECT now reachable on a POSITIVE signal only: `context_fixed` (charted), collinear-chain geometry (`find_chain_members` -> reef/shoal), or position-stability (`FIXED_POSITION_SPREAD_M`, heuristic). Asymmetric: positive promotes, ambiguity stays reviewable. → `test_collinear_chain_is_fixed_object_but_blob_is_not`, `test_position_stable_*`, `test_persistent_but_spread_out_*`, `test_context_fixed_*`. → position-stability can't exclude a tightly-moored vessel; charted layer still the gold standard. **DONE (SEMANTIC).**
- **4.2** repeated coarse MMSI = identity. → ANCHORED wording = "hypothesis"; only `our_ais` can anchor. → code. → — **DONE.**
- **4.3** propagate to UI/legend/sim. → `api.ts`, `classes.ts`, `ALERT_PRIORITY`, DetailPanel. → tsc + vite build + API alerts. → — **DONE.**

## §5 Clustering & observation accounting
- **5.1** transitive chains. → complete-linkage + `CLUSTER_DIAMETER_M`. → `test_complete_linkage_blocks_transitive_chain`. → — **DONE.**
- **5.2** persistence counts dates not acquisitions. → documented; live-analysis acquisition discovery added (§10.1). → — → cached recent lacks scene ids. **PARTIAL/BLOCKED.**
- **5.3** 60 m dedup can drop distinct vessels. → documented limitation; not blindly widened (would re-introduce tile-overlap dupes). → — → needs acquisition identity. **PARTIAL.**

## §6 Coastal masking & coverage
- **6.1** buffer removes vessels too. → excluded-coverage km² recorded; ablation: 401 km² (15%) excluded, 181 km² by buffer. → `ablation.json`. → — **DONE.**
- **6.2** mask not padded beyond tile. → padded grid then crop. → old-vs-new ≤0.3% (edge correctness). → — **DONE (BUG).**
- **6.3** distance units/axes. → `_pixel_metres` + `sampling`. → `test_pixel_metres_metric_vs_geographic`. → — **DONE (BUG).**
- **6.4** silent JRC fallback. → `jrc_used`/`jrc_fallback_reason` recorded. → diagnostics. → — **DONE.**
- **6.5** JRC "catches reefs" false (occurrence=99). → claim corrected; reef/shoal chains now caught by in-data collinear-chain geometry (`find_chain_members`), classed `FIXED_OBJECT (chain_geometry)` -- e.g. the 2.9 km Gulf-of-Mannar arc (64 dets). → probe + rescore counts. → geometric proxy, not a *charted* layer; an authoritative reef/bathymetry layer remains the gold-standard upgrade. **DONE (doc + in-data proxy) / charted layer PENDING.**

## §7 VH/VV corroboration
- **7.1** "local" ratio is tile-wide. → relabelled honestly; local VH = experiment. → code read. → — **DONE (relabel) / EXPERIMENT.**
- **7.2** only shape checked, not co-registration. → VH used only if shape+CRS+transform match, else skipped with reason. → `run_detector_v3` diag `vh.reason`. → — **DONE.**
- **7.3** nodata/universal-claim. → invalids handled (finite masks); claim softened; increment kept heuristic. → code. → increment uncalibrated (no labels). **DONE/BLOCKED(calibration).**

## §8 Numerical robustness
- **8.1** NaN propagation via `intensity*mask`. → finite-mask before box filter. → `test_local_mean_no_nan_propagation`. → — **DONE (BUG).**
- **8.2** no insufficient-data state. → explicit `status`+notes returned and logged. → `test_all_masked_tile_is_degraded_not_zero`; `run_recent` prints status. → — **DONE.**

## §9 Confidence semantics
- **9.1** 0–100 presented as certainty. → `score_basis` on every detection; UI "evidence score" + note. → API; DetailPanel. → not calibrated. **DONE.**
- **9.2** lowered bar for GFW-associated unvalidated. → kept but explicitly labelled experimental in reasons. → API reason text. → no labels to validate. **DONE (labelled)/BLOCKED.**
- **9.3** unknown-AIS permitted a DARK label; UI overstated "no AIS nearby". → `DARK_CANDIDATE` now requires *established* no-AIS (`unmatched`/`gfw_reported_no_ais`); unknown-AIS strong target -> new `UNVERIFIED_TARGET`; DARK blurb reworded to state the actual basis. → `test_unknown_ais_is_unverified_not_dark`, `test_gfw_reported_no_ais_is_dark_candidate`. → — **DONE.**
- **9.4** heuristic fixed labelled "confirmed/charted" and hidden from queue. → chain/position heuristics -> `SUSPECTED_FIXED` (reviewable, deprioritized, labelled *suspected*); `FIXED_OBJECT` reserved for charted evidence only. → `test_*_suspected_fixed*`; API queue. → — **DONE.**

## §10 Acquisition selection & dedup
- **10.1** fixed 12-day cadence. → live analysis queries actual S1 acquisition dates (EE) and **no longer guesses**: discovery failure -> run errors (no dates invented); no scenes -> `data_unavailable` with a note. Runs the **corrected `detect_v3.run_detector_v3`** (not Round 1). GFW request failure vs successful-empty now distinguished (`gfw_ok`/`gfw_failed_dates`/`partial`); `coverage` separates unavailable tiles from empty sea. → code + import check; needs live EE to exercise end-to-end. → — **DONE (needs live EE).**
- **10.2** date-only association; keep scene/orbit/time. → acquisition discovery done; per-detection scene-id persistence not yet. → — → cached data lacks scene ids. **PARTIAL/BLOCKED.**
- **10.3** scene-bound caches. → documented; recent cache keyed by hotspot+date, not scene. → — → — **PARTIAL.**

## §11 Independent v3 evaluation
- **11.1** v3 never evaluated independently. → staged ablation on common footprint (mask/CA-CFAR/GG-CFAR/opening). → `ablation.json`. → no labels → density/exceedance/coverage only, not precision/recall. **PARTIAL (no labels).**
- **11.2** hotspot ≠ arbitrary ocean. → stated explicitly in report + ablation note. → — → needs non-GFW tiles. **BLOCKED.**

## §12 Enrichment statistics
- **12.1** full-circle area, overlap, Poisson independence, →precision. → actual valid-water area in/out; tile-level block bootstrap CI; sensitivity sweep; population-association framing only. → `enrichment.json` (7.5× CI[4.3,13.2] @1500m/12dB). → matched coast-distance controls not applied; overlapping tiles share blobs. **DONE (improved) / partial.**

## §13 Provenance & reproducibility
- **13.1** stale requirements.txt. → real stack + versions. → manifest. → — **DONE.**
- **13.2** outputs lacked provenance. → top-level `provenance` block per scored file; per-detection detector_version/config/mask/vh in run_recent. → files. → historical fields not fabricated. **DONE.**
- **13.3** zero vs failed not distinguished. → explicit detector `status`; provenance `status`; live-path `coverage` + `data_unavailable` + `partial` separate unavailable data / unperformed GFW dates from an empty sea. → diag + run result. → — **DONE.**
- **13.4** crop brightness not comparable (per-crop percentile stretch). → single canonical renderer `src/crops.py` at a fixed [-23,+3] dB window; `run_recent`/`build_gulf`/backend route through it; saved Tuticorin (17) + Gulf (95) crops regenerated from source via `audit/regen_crops.py`. → regen run (no missing inputs); crop endpoint serves PNGs. → — **DONE.**
