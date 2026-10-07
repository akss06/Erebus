# Audit final report — Erébus dark-vessel detection

Date: 2026-10-07. Baseline: git `549ed4a` (clean tree). Scope: the 13-section
scientific & detection-logic audit. Companion docs: [AUDIT_CHECKLIST.md](AUDIT_CHECKLIST.md)
(per-item evidence), [WORKLOG.md](WORKLOG.md) (chronology), [audit/](audit/)
(baseline manifest, experiments, results).

This report separates **confirmed bugs**, **corrected interpretations**,
**experimental hypotheses**, **measured outcomes**, and **blocked experiments**.
Nothing here claims improved detection *performance* — we have no labelled field
truth, so performance is reported as density/exceedance/coverage, never as
precision/recall or "false objects per km²".

---

## 1. Confirmed bugs (provably wrong → fixed in place)

| id | bug | fix | verification |
|---|---|---|---|
|§8.1|`_local_clutter_mean` did `intensity * clutter_float`; `NaN*0=NaN`, and the box filter smeared each NaN across an outer-window neighbourhood, where the detector then silently substituted the global median — corrupting the local background near any non-finite pixel.|Finite-mask before the box filter; only finite clutter pixels contribute.|`tests/test_audit_fixes.py::test_local_mean_no_nan_propagation`|
|§6.2|Shore-distance transform ran inside the tile only, so land just beyond the tile edge did not push the 1 km buffer inward — edge pixels over near-shore water were wrongly kept.|Build NE+JRC land and the distance transform on a grid padded by the buffer, then crop. Measured effect: ≤0.3% fewer sea px (correct edge exclusion).|`build_sea_mask`; offline old-vs-new mask comparison|
|§6.3|Shore distance used `abs(transform.a)` px size and assumed square metric pixels.|`_pixel_metres`: use both axes; metric CRS directly, geographic converted at latitude; `distance_transform_edt(sampling=...)`.|`tests/..::test_pixel_metres_metric_vs_geographic`|
|§6.4|A JRC fetch failure silently produced Natural-Earth-only masking that looked like the full method.|`build_sea_mask` records `jrc_used`/`jrc_fallback_reason` in `mask_info`, surfaced in diagnostics.|code read; diagnostics on real tiles|
|§5.1|`cluster_across_passes` was single-linkage with no diameter cap, so A–B and B–C within 100 m merged A–C even when >100 m apart (transitive chains).|Complete-linkage with `CLUSTER_DIAMETER_M` cap: clusters merge only if every cross-pair is within range.|`tests/..::test_complete_linkage_blocks_transitive_chain`|
|§13.1|`requirements.txt` listed `folium/pandas/streamlit` — none of the actual stack.|Replaced with the real stack + verified versions.|`audit/baseline_manifest.json`|

Also fixed as robustness (§1.4, §8.2): a local ring with `< MIN_TRAIN_PX=12` clutter
pixels is no longer trusted; `detect_blobs_v3`/`run_detector_v3` return an explicit
`status ∈ {ok, degraded, insufficient_data, no_sea}` + notes, so a failed/partial
analysis is never presented as a normal zero-detection result.

## 2. Corrected interpretations (not a code bug — overstated claims relabelled)

These change what the system *claims*, matching the standing "never overstate" rule.
Historical detections are preserved; the relabelling is marked as semantic, not a
detector change (top-level `provenance` block in each scored file).

- **§3/§9.2 — GFW is not independent confirmation, and not AIS.** GFW's SAR-presence
  product is derived from the *same* Sentinel-1 imagery by a different algorithm.
  Every place that said "independently reported" now says "shared-sensor algorithmic
  agreement, not independent confirmation" (`confidence.py`, `run_recent.py`,
  `build_gulf_candidates.py`, `backend/main.py`, `validate_vs_gfw.py`, UI).
- **§3.1/§3.4 — GFW association ≠ AIS evidence.** New fields: `gfw_association ∈
  {associated, ambiguous, none}` and `ais_evidence ∈ {matched, gfw_reported_ais,
  gfw_reported_no_ais, unmatched, unknown}`, with `ais_source` separating an AIS match
  we computed (Tuticorin) from an identity GFW attributed to a SAR record. Many-to-one
  GFW records are flagged `ambiguous`. "No GFW record" is `unknown`, not a verified
  no-AIS observation.
- **§4 — persistence does not prove fixed infrastructure.** New
  `PERSISTENT_UNIDENTIFIED` class for persistent targets without a consistent identity
  (could be infrastructure *or* a dark ship at anchor — persistence cannot separate
  them). `FIXED_OBJECT` is assigned only on a **positive** fixed-object signal, never
  inferred from mere persistence: (a) explicit `context_fixed`/charted evidence;
  (b) **collinear-chain geometry** — a long, thin, persistent run of clusters (reef/
  shoal/causeway; `find_chain_members`), which a compact anchorage blob fails; or
  (c) **position-stability** — a cluster pinned to one spot across passes, labelled an
  explicit heuristic (a short-scope anchored vessel is not excluded). The design is
  deliberately asymmetric: a positive signal promotes to `FIXED_OBJECT`, but any
  ambiguity falls back to `PERSISTENT_UNIDENTIFIED` (reviewable), never the reverse, so
  a long-anchored dark vessel is not silently dismissed. (A first pass gated
  `FIXED_OBJECT` behind `context_fixed` alone, which nothing set — making the class
  unreachable; the chain/position evidence paths fix that.)
- **§9.1 — the 0–100 score is heuristic.** Every detection carries
  `score_basis = "heuristic evidence/ranking score (not a calibrated probability)"`;
  the UI label changed from "confidence /100" to "evidence score /100" with that note.
- **§6.5 — JRC does not catch the Gulf reefs.** The docstring claim was false (our
  probe: JRC occurrence = 99 over the Adam's Bridge reefs). Corrected; an authoritative
  charted reef/infrastructure layer is noted as the right tool (still pending — §5 below).
- **§7.1 — VH background is tile-wide, not local.** Relabelled honestly; the universal
  "metal vessels spike VH" claim softened to "consistent with (not proof of)".

## 3. Experimental hypotheses (separately identifiable, NOT wired into defaults)

Under `audit/experiments/`. None is claimed to improve detection without measurement.

- **Calibration (§1.5)** `calibration.py`: fit alternatives on synthetic clutter.
- **Morphology (§2)** `morphology.py`: cost of the pre-labelling `binary_opening`.
- **Ablation (§11)** `ablation.py`: staged mask/detector/opening comparison on real tiles.
- **Local VH (§7.1)**, **clutter-superpixel-only fit (§1.2)**, and a **compact-target
  peak path (§2.3)** are identified as the next experiments; the ablation already
  exposes the opening on/off knob that a compact-target path would exploit.

## 4. Measured outcomes (valid comparisons only; no labels → no precision/recall)

**P_fa calibration (synthetic, Gamma(2,0.5), 5 seeds × 5M held-out) —
`audit/results/calibration.json`:**

| fit method | achieved P_fa | × nominal (1e-5) |
|---|---|---|
| trim+gengamma (**detector default**) | 1.45e-4 | **14.5×** |
| gengamma, no trim | 1.3e-5 | 1.3× |
| gamma, no trim | 1.0e-5 | 1.0× |
| empirical quantile | 1.3e-5 | 1.3× |

Reproduces the reported ~1.7e-4 counterexample (same order). **The top-1% trim — which
exists to keep targets out of the fit — is what inflates P_fa ~14×.** Removing it
calibrates on clean clutter but would re-admit target contamination on real scenes;
this is a trade-off, not a free fix. PFA is now documented as *nominal* everywhere.

**Morphology opening loss (injected targets in real clutter, 150 sites) —
`audit/results/morphology.json`:** recovery WITH vs WITHOUT `binary_opening`:

| target | with opening | without |
|---|---|---|
| single 1 px | 0.00 | 0.00 (blocked by MIN_BLOB_PX=3) |
| line 1×3, 1×4, L-3px, block 2×2, adjacent pair | **0.00** | **1.00** |
| block 3×3 | 1.00 | 1.00 |

**The default opening erases every target smaller than a solid 3×3 block.** So
`MIN_BLOB_PX=3` is largely illusory; the real floor is ~9 px compact (and
`confidence.MIN_AREA_PX=8` gates on top). A 15 m boat (~1–2 px at 10 m) is below this.

**Staged ablation, 37 tiles / 2257 km² common footprint —
`audit/results/ablation.json`:**

| quantity | value |
|---|---|
| NE-only sea vs v3 sea | 2657 → 2257 km² (**401 km², 15%, excluded by JRC + buffer**; 181 km² from the shore buffer) |
| CA-CFAR density (old mask → v3 mask) | 0.140 → 0.101 / km² |
| GG-CFAR pixel exceedance over sea | **6.35e-4 (~63× nominal 1e-5)** — upper bound, includes real targets |
| GG-CFAR density, opening on → off | 0.099 → 0.444 / km² (**opening removes 77.7% of candidates**) |

Honest reading: the **mask is the biggest lever** on detection count; GG-CFAR+gates
lands at a density comparable to CA-CFAR on the same footprint; the real achieved
exceedance is far above the nominal P_fa (corroborating the calibration result); and
the 1 km buffer is a real **15% coverage cost**, not only "better discrimination".

**Enrichment (corrected, population-level) — `audit/results/enrichment.json`:**
strong-blob density near GFW no-AIS points vs surrounding valid water, tile-level
block bootstrap (119 tiles): e.g. r=1500 m, 12 dB → **7.5×, 95% CI [4.3, 13.2]**
(range 6–12× across radius/contrast). This is a **population association only** — not
candidate precision and not a per-target probability.

**Semantic relabel (before → after), preserved in `audit/results/*_baseline_549ed4a.json`:**

| set | baseline | after audit |
|---|---|---|
| tuticorin | FIXED_OBJECT 15 | SUSPECTED_FIXED 4 (position-stable), PERSISTENT_UNIDENTIFIED 11, ANCHORED×4 preserved |
| recent | FIXED_OBJECT 67 | SUSPECTED_FIXED 64 (collinear reef chain), PERSISTENT_UNIDENTIFIED 3 (isolated), DARK 17 / VESSEL 4 unchanged |
| gulf | DARK 23, CLUTTER 62 | DARK 16, CLUTTER 70 (8 at 10–11.7 dB reclassified under the already-committed 12 dB bar; consistency, not a new tuning or detector change) |

Fixed-object handling is **asymmetric and honest about confidence**: heuristic signals
(`fixed_evidence` = **chain_geometry**, a collinear persistent run — reef/shoal; or
**position_stable**, pinned across passes) are classed **`SUSPECTED_FIXED`** — they
**stay reviewable** (in the queue, deprioritized), labelled *suspected*, never *charted*.
Only **charted** evidence (`context_fixed`) reaches the confirmed, queue-excluded
`FIXED_OBJECT` (none yet). The recent arcs are a measured 2.9 km, 10:1 line (perpendicular
RMS 42 m) in Gulf-of-Mannar reef water — chain geometry, not vessels; position-stability
alone recovered 0 on recent (tightest cluster already spreads 16 m), and the spread
threshold was **not** loosened to force the relabel.

Label discipline also tightened elsewhere: `DARK_CANDIDATE` now requires *established*
no-AIS evidence; a strong target with AIS status never established is `UNVERIFIED_TARGET`,
not "dark". The live "Run Analysis" path now runs the corrected `detect_v3` detector (not
Round 1) and reports a coverage record distinguishing *unavailable data* from *empty sea*.

## 5. Blocked experiments (need data/labels we do not have)

- **§11 precision/recall, blind review:** no labelled field truth exists. Only
  density/exceedance/coverage are reported. Unblock: an independently labelled set
  (expert review or AIS-confirmed vessels) blind to model scores.
- **§11.2 arbitrary-ocean performance:** all tiles are GFW hotspots; results do not
  represent open ocean. Unblock: tiles over independently chosen, non-GFW areas
  (needs new downloads / GEE).
- **§1.2 clutter-superpixel-only fit, §2.3 compact-target path, §7.1 local VH:** coded
  as experiments but choosing a default requires the labelled set above.
- **Corrected-detector regeneration of recent/gulf:** the detector corrections change
  output on some tiles; committed `scored_recent`/`scored_gulf` remain the FROZEN
  baseline-detector output. A full re-run needs the JRC fetch for ~437 recent + 143
  validation tiles (network/GEE). The corrected detector is verified to run on the 140
  JRC-cached recent tiles.
- **§10.2 per-detection scene-id/orbit persistence:** acquisition *discovery* is
  implemented in the live analysis path (§10.1, replaces the 12-day assumption, needs
  live EE to exercise); persisting scene id/orbit on every stored detection is a
  further plumbing step not yet done.
- **§5.2 acquisition-based observation accounting:** cached recent data is date-keyed
  with no scene ids, so persistence is still counted per calendar date. Unblock: store
  scene/orbit ids at acquisition time.

## 6. Reproduce

```bash
pip install -r requirements.txt
python -m pytest tests/test_audit_fixes.py -q          # 11 regression tests
python src/confidence.py                                # regenerate Tuticorin (offline)
python audit/rescore_historical.py                      # semantic re-score recent+gulf (offline)
python audit/experiments/calibration.py                 # synthetic P_fa calibration
python audit/experiments/morphology.py                  # opening loss on injected targets
python audit/experiments/ablation.py 40                 # staged ablation (cached tiles)
python src/enrichment_test.py                            # corrected enrichment (cached tiles)
python -m uvicorn backend.main:app --port 8000          # serve; classes incl. PERSISTENT_UNIDENTIFIED
```

Baseline manifest (git rev, deps, seeds, output hashes): `audit/baseline_manifest.json`.
Seeds: `np.random.default_rng(0)` for the fit subsample, bootstrap and injections;
SLIC is deterministic.
