> **Objective follow-up (no eyeballing): `src/enrichment_test.py`.** Strong blobs (>=10 dB, >=8 px) occur within 1.5 km of
> GFW's AIS-less detections **7.4x more often than chance** (58 observed vs 7.9 expected, 119 tiles), the same enrichment as
> for AIS-matched ships (7.4x, 11 observed vs 1.5 expected, control). About 1 in 7 of those blobs is expected to be chance. The
> visual remarks in this document are the author's non-expert impressions and are not evidence.

> **UPDATE, full run (supersedes the 30-detection sample below).** All 143 GFW detections over the three passes were tested.
> Our detector produced a detection within 1500 m for **111 (78%)**: **20 of 22 (91%)** of GFW's AIS-matched ships and
> **91 of 121 (75%)** of its AIS-less detections. False alarms: 755 extra detections over 11,349 km² = **6.7 per
> 100 km²**, so a 1.5 km circle contains a random detection ~37% of the time. After scoring, of the 95 found detections
> outside Tuticorin, 62 are noise-level clutter and 23 look like ships (10-16 dB), of which none has an AIS match.
> The headline stays the same as the correction below: we reliably see AIS-matched ships; for AIS-less detections most of
> what "agrees" is noise-level, and a minority (~1 in 5) are clear ship-like returns. Those 23 candidates are in
> `dark_candidates_for_review.csv` / `DARK_CANDIDATES_CONTACT_SHEET.png` for human review; by the author's quick visual
> read about 16 look like clear compact targets, about 6 are doubtful and 3 sit at the end of bright streaks (possible
> radar artefacts). None is verified.

# Validation vs GFW SAR detections (Round 2, 2026-10-03)

Code: [`src/validate_vs_gfw.py`](../src/validate_vs_gfw.py). Raw rows: `data/validation/validation_rows.json`.

> **Correction, same day (read this first).** The 80% figure below is too generous. Our detector fires about
> 6.3 times per 100 km², so a 1.5 km search circle (7.1 km²) contains some random detection ~36% of the time by
> chance alone. When we look at *what* we found: of the 8 GFW ships that have an AIS match we found 7 with strong
> contrast (>=15 dB, 88%); of the 22 GFW detections with NO AIS match we found 17, but 13 of those 17 are
> noise-level blobs (6-9 dB). Only 4 of the 22 (18%) are clear ship-like returns (10-13 dB). The honest summary:
> **we reliably see GFW's AIS-matched ships, and we see only about one in five of its AIS-less detections**, which
> are probably small boats near the 10 m resolution floor. Do not quote "80% recall".

**Question:** when Global Fishing Watch (GFW) reports a ship in a Sentinel-1 scene, does our unmodified
detector (Round 1 settings: CA-CFAR k=5, 30 px coast buffer) find a detection at the same spot?

**Method:** 3 Sentinel-1 passes (2026-01-06, -18, -30; Gulf of Mannar + Palk Strait). GFW listed 52, 20 and 71
detections. We sampled 10 per date (evenly spread), downloaded a ~9 km tile around each at 10 m, ran our
detector, and counted a hit if we had a detection within 1500 m (GFW positions are snapped to a ~1 km grid).

## Results (30 GFW detections)

| | Found by us | Total |
|---|---|---|
| All | **24 (80%)** | 30 |
| GFW says AIS-matched | 7 | 8 |
| GFW says no AIS match | 17 | 22 |
| By date: 06 Jan / 18 Jan / 30 Jan | 9 / 8 / 7 | 10 each |

- Median distance from GFW's point to our nearest detection: **~500 m**; 22 of 30 within 1000 m.
- **Extra detections** (ours that are not near the tested GFW point): 151 over 2,408 km² of sea
  = **6.3 per 100 km²**.

## How to read this (honest limits)

- **Recall ~80% against GFW is an agreement figure, not true recall.** GFW also misses ships. Both systems
  use the same Sentinel-1 scene, so shared blind spots (very small boats) are invisible here.
- **The 6 misses** are mostly weaker/no-AIS targets (one was AIS-matched). A 1500 m radius is generous; the
  strict (<=1000 m) figure is 22/30.
- **"Extras" over-count our errors:** each tile contains other ships GFW also reported but that we did not
  test, and real ships GFW missed. It is an upper bound on our false-alarm rate, but it is real: expect
  roughly 1 non-GFW detection per 16 km². Most are the clutter/linear-feature type documented in Round 1.
- **The sample is 30 of 143** GFW detections, not all of them.
