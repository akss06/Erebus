# Multi-Date Findings — Same Anchorage, 4 Passes

Extends Milestone 1/2's single validated date (2026-01-18) to 3 more Sentinel-1 passes over the identical Tuticorin anchorage sub-box (lon 78.22-78.32, lat 8.72-8.85, relative orbit 92), running the same fetch -> land-mask -> CA-CFAR detect -> shape filter -> AIS match pipeline unmodified. Code: [`src/run_multi_date.py`](../src/run_multi_date.py). Data: `data/multi_date/{date}_detections.geojson`.

## 1. Totals across all 4 dates

| date | status | detections | matched | confirmed_vessel* |
|---|---|---|---|---|
| 2026-01-18 | OK | 17 | 13 | 6 |
| 2026-01-06 | OK | 20 | 14 | 7 |
| 2026-01-30 | OK | 22 | 15 | 6 |
| 2026-02-11 | OK | 11 | 7 | 5 |
| **total (OK dates)** | | **70** | **49** | **24** |

\* confirmed_vessel_count = MATCHED detections with contrast_db > 15dB, applied uniformly (including recomputed for 2026-01-18) as a proxy since we haven't manually eyeballed every date. This is looser than Milestone 1's visual ground truth: on 2026-01-18 alone, this proxy flags 6 detections (#1-#6) as confirmed_vessel, vs only 2 (#3, #4) that visual crop review actually confirmed. Treat this column as "worth reviewing," not "confirmed real."

## 2. Does the same vessel reappear across dates?

**DMC JUPITER (MMSI 511101582)** — the vessel Milestone 2 matched to detections #3/#4 on 2026-01-18 — also shows up in the AIS presence data on: 2026-01-18. It was matched to an actual SAR detection (not just present in the box) on: 2026-01-18.

Vessels matched to a SAR detection on 2 or more dates (persistent, SAR-and-AIS-confirmed traffic):

| MMSI | name | dates |
|---|---|---|
| 341970001 | NEREUS PROGRESS | 2026-01-18, 2026-01-06, 2026-01-30, 2026-02-11 |
| 419001466 | OCEAN POISE | 2026-01-18, 2026-01-30 |

Looking at raw AIS presence (not just SAR-matched) in the box: 12 vessel(s) appear on 2+ dates, of which 2 were also matched to a SAR detection on 2+ dates. The remaining 10 are AIS-visible repeat traffic that our detector didn't pick up on multiple passes - consistent with Milestone 1/2's honest recall limitation, not new information.

## 3. Dates that failed or had poor coverage

None - all 3 new dates cleared the resolution and coverage gates.

## 4. Top detections flagged for manual review

The 3 highest-contrast MATCHED detections across the 3 new dates (not yet visually confirmed the way #3/#4 were for 2026-01-18 - crops saved to `results/multi_date_flagged_*_crop.png` for that review):

| # | date | lon | lat | contrast_db | shape | matched vessel | dist_m |
|---|---|---|---|---|---|---|---|
| 1 | 2026-02-11 | 78.27135 | 8.72631 | 19.7 | BLOCKY | SEA VALARY (MMSI 567593000) | 436 |
| 2 | 2026-01-30 | 78.28269 | 8.72565 | 18.8 | BLOCKY | FJ CAMELIA (MMSI 538009555) | 567 |
| 3 | 2026-02-11 | 78.22037 | 8.75479 | 18.7 | BLOCKY | OCEAN ALLIANCE (MMSI 419002047) | 534 |

## 5. Caveats carried over from Milestone 2

- `confirmed_vessel_count` here is a contrast-threshold proxy, not visual confirmation - see §1's note. Don't read it as equivalent to Milestone 1's manually-reviewed #3/#4.
- GFW's `4wings/report` is non-deterministic across identical queries (Milestone 2 §2); the MMSI overlap in §2 is a snapshot from this run, not a stable ground truth - re-running this script could return different AIS records for the same dates.
- Match status still doesn't distinguish real vessels from linear-feature/speckle artifacts without visual review (Milestone 2 §5); the same caution applies to every detection in this document.
