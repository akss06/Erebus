# Milestone 2 Findings — AIS Overlay & Matching

Status: matching pipeline built and run against real GFW AIS data. One
detection pair (#3, #4) independently cross-validated by AIS. Radius
sensitivity analysis shows the honest precision limit of GFW's public
presence product. Streamlit app not started.

Code: [`src/fetch_ais.py`](../src/fetch_ais.py) (exploration, unchanged from
Milestone 2 step 1-3), [`src/match.py`](../src/match.py) (matching).
Data: `data/matched_detections.geojson`.

---

## 1. What GFW's API can and cannot provide

Three distinct data products, not one, confirmed by reading the docs and
then testing live (the two didn't fully agree):

| Endpoint | Returns | Usable for "who was in this box on this date"? |
|---|---|---|
| `/vessels/search` | Identity/registry only, no positions | No |
| `/events` | Individual timestamped lat/lon per event (fishing, encounter, loitering, port_visit, gap, SAR) | No - requires a known `vessels[0]`, no bbox/region filter |
| `/4wings/report` | The only bbox/GeoJSON-queryable endpoint | Yes, with caveats below |

Since we start from SAR detections, not known vessel identities, `/events`
is a dead end for this project's direction of matching (SAR → AIS). Only
`/4wings/report` accepts a bounding box directly.

## 2. The single-day vs month-query quirk

Docs describe `/4wings/report` as returning grid-cell aggregates. Live
testing found more nuance:

- A **single-day** date-range (`2026-01-18,2026-01-18`) on
  `public-global-presence:latest` for our sub-box returns a **null** entry.
- The **same bbox over a full month** (`2026-01-01,2026-01-31`) returns
  hundreds of **individual vessel-level records**: MMSI, ship name, flag,
  vessel type, an approximate lat/lon (snapped to the ~1/100 degree "HIGH"
  grid, not the vessel's exact position), a `date` within the range, and
  `hours` (presence-hours in that grid cell on that date).

`match.py` works around this exactly as `fetch_ais.py`'s exploration
already showed: fetch the whole month, filter to the pass date locally.
Why a single day returns nothing isn't understood - flagged as an observed
API behavior, not something explained by the documentation.

**A second, separate reproducibility issue surfaced during this
milestone's work**: two back-to-back identical queries (same bbox, same
month, same dataset) returned **different data** - 13 AIS presence records
on the pass date in one run, 17 in the very next run, with some detections
matching different named vessels between runs (e.g. detection #11 matched
`RIVER PEARL 36` in the first run and `AB OLIVIA` in the second). This
wasn't a bug in our code - identical requests, different responses. Any
operational use of this endpoint needs to treat its output as
non-deterministic/eventually-consistent, not a stable ground truth.

## 3. The DMC JUPITER validation

Milestone 1's visual crop review (independently, before any AIS data was
fetched) identified detections **#3** (78.28384, 8.73015, contrast 18.4dB)
and **#4** (78.28371, 8.72946, contrast 17.9dB) as the only two of 17
CFAR detections showing a genuine discrete bright blob with no
line/structure/edge nearby - the strongest vessel candidates across every
AOI tried in this project.

Matching against AIS, independently, both landed within ~450m of the same
vessel:

```
#3  -> DMC JUPITER (MMSI 511101582, CARGO, flag PLW), distance 422m
#4  -> DMC JUPITER (MMSI 511101582, CARGO, flag PLW), distance 413m
```

Same named cargo vessel, present 22 of 24 hours on the pass date, ~80m from
each other in the SAR image, ~15m from each other in AIS-grid terms - fully
consistent with one real ship producing two adjacent CFAR blobs. This is
the project's one clean example of visual confirmation and independent AIS
confirmation agreeing.

The caveat: at a 250m match radius (see below), **this match disappears** -
both #3 and #4 flip to UNMATCHED, because their true nearest-AIS distances
(413-422m) exceed 250m. The "confirmation" holds at 500m and 1500m, not at
250m.

## 4. Radius sensitivity

Same 17 detections, same AIS pull, three match radii:

| radius_m | matched | unmatched |
|---|---|---|
| 1500 | 13 | 4 |
| 500  | 10 | 7 |
| 250  | 2  | 15 |

The match rate collapses well before reaching GFW's own stated ~1km grid
cell size. At 250m - tighter than the grid cell - only detection #1
(unexplained, matched at 92m) and #11 (a LINEAR_FEATURE fragment, matched
at 193m) survive; both CONFIRMED_VESSEL detections drop out. This is the
honest shape of what a ~1km-resolution presence grid can support: anything
tighter than several hundred meters starts rejecting real matches, not just
false ones.

## 5. The core limitation: match/unmatch alone cannot separate real vessels from artifacts

Cross-referencing the match table against Milestone 1's visual
ground truth (`results/MILESTONE1_FINDINGS.md`) at the default 1500m
radius:

- **CONFIRMED_VESSEL** (#3, #4): both MATCHED - correct.
- **LINEAR_FEATURE** (#5-12, eight fragments of one non-vessel pipeline/
  cable/unmapped-shoal feature): **7 of 8 also MATCHED** to nearby AIS
  vessels (mostly `OCEAN POISE`), purely because the anchorage is dense
  enough that most points in the box sit within 1500m of *some* AIS
  position that day.
- **SPECKLE** (#13-17, no visible feature in any crop): mostly UNMATCHED,
  but **#16 MATCHED** anyway (`OCEAN POISE`, 554m) at the 1500m/500m radii.
- **UNEXPLAINED** (#1, #2): both MATCHED, including #1 at just 92m - the
  closest match distance of the entire set, attached to the one detection
  with no visible target at all in its crop.

So a naive "MATCHED = real vessel" reading would have wrongly endorsed 7
non-vessel linear-feature fragments and one speckle detection, while the
closest AIS match in the whole dataset belongs to the one detection that
looks like nothing at all on inspection. **Match status without visual or
shape confirmation is not trustworthy at this detection density and grid
resolution.** The two together - CFAR/shape output plus AIS proximity -
are what make #3/#4 credible; neither alone would have been enough.

## 6. What operational deployment would require

This project used GFW's free public presence product, which is:
- Daily-binned at best (no single-day query works at all)
- Spatially snapped to a ~1km grid, not exact vessel positions
- Not reproducible run-to-run (see §2)
- Not timestamped precisely enough to compare against a single ~30-second
  SAR pass - "22 hours present" cannot confirm a vessel was in-frame at
  00:32 UTC specifically, only "sometime that day"

Real operational dark-vessel detection - matching a SAR pass to the AIS
picture at that exact moment - needs a feed with per-vessel positions at
native AIS reporting intervals (seconds to minutes) and full spatial
precision, not a daily aggregate grid. That means a partnership with a
source carrying a live/near-real-time AIS feed: the Indian Coast Guard,
NIC's National AIS network, or a commercial AIS provider (Spire,
exactEarth/Spectra, MarineTraffic, etc.) - exactly the kind of partnership
this application's Q7 already anticipates. GFW's API was the right tool
for proving the matching *concept* on free data; it is not the tool for an
operational system.
