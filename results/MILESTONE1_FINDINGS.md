# Milestone 1 Findings — SAR → Detections

Status: Proof of concept reached. Two vessel candidates visually confirmed
out of 17 CFAR detections in the final run. Milestone 2 (AIS fetch +
matching) not started.

Confirmed configuration: [`config/milestone1_confirmed.json`](../config/milestone1_confirmed.json)
Evidence crops: this directory (`detection_3_crop.png`, `detection_4_crop.png`,
`cluster_6_12_crop.png`, `detection_13_crop.png`)

---

## 1. Pipeline as built

```
[1] AOI + scene selection
      -> Query COPERNICUS/S1_GRD (IW, VV) for the AOI, print real available
         passes (date + orbit), pick one - never assume a date has coverage.
      -> Fetch a scene's exact footprint geometry (metadata only) before
         committing to a download, to check AOI overlap in advance.
        |
        v
[2] fetch_sar.download_scene()
      -> Single-band VV GeoTIFF via Earth Engine getDownloadURL, at a scale
         chosen to fit the 48MiB direct-download cap (10m/px for small
         boxes, up to 30m/px for wider ones).
      -> Resolution floor enforced after download: if EE ever returns
         something coarser than 15m/px, stop before detecting rather than
         silently running CFAR on degraded data.
        |
        v
[3] Coverage gate
      -> % of downloaded pixels that are non-nodata. Below 60%, stop and
         report rather than detecting on a mostly-empty raster.
        |
        v
[4] land_mask.get_sea_mask()
      -> Natural Earth 10m land polygons, clipped to the AOI in EPSG:4326
         THEN reprojected into the raster's native UTM CRS (reprojecting
         the whole global dataset first is what breaks - see Limitations).
      -> Land mask dilated by a coastal buffer (10-30px depending on AOI)
         to push the sea/land boundary further from shore.
        |
        v
[5] detect.detect_blobs() - CA-CFAR
      -> Per-pixel local threshold: local_mean + k * local_std, computed
         over a square training ring (guard band excluded) restricted to
         sea-mask pixels only.
      -> Morphological opening -> connected components -> per-blob
         centroid, area, mean/max intensity, LOCAL background dB and
         contrast_db (mean_intensity_db - local_background_db).
        |
        v
[6] Shape filter
      -> Per blob: bounding-box aspect ratio (long/short side) and fill
         ratio (blob px / bbox area) -> SHIP-LIKE / LINEAR-STRUCTURE /
         BLOCKY / AMBIGUOUS.
        |
        v
[7] Human visual review (this document)
      -> Zoomed, contrast-stretched crops per detection, generated
         on demand and eyeballed before trusting any count.
```

Everything after step 1 runs entirely offline once a scene is cached -
re-tuning CFAR/shape/mask constants and re-running costs no Earth Engine
calls.

---

## 2. Confirmed detections

**Scene:** `S1A_IW_GRDH_1SDV_20260118T003257...`, 2026-01-18, descending,
relative orbit 92.
**AOI:** outer Tuticorin (Thoothukudi) anchorage, lon 78.22-78.32,
lat 8.72-8.85 - deliberately east of the port breakwater complex.
**Config:** CA-CFAR guard=5px, training=15px, k=5.0, 10m/px, 30px (300m)
coastal buffer. Full detail in `config/milestone1_confirmed.json`.

Of 17 CFAR detections in this run, two hold up under visual review:

| # | lon | lat | area_px | mean dB | contrast_db | shape label |
|---|-----|-----|---------|---------|--------------|-------------|
| 3 | 78.28384 | 8.73015 | 19 | 10.8 | 18.4 | BLOCKY (aspect 1.8, fill 0.68) |
| 4 | 78.28371 | 8.72946 | 8  | 11.9 | 17.9 | BLOCKY (aspect 1.3, fill 0.67) |

Both show a discrete, compact bright blob clearly standing out against
darker surrounding sea speckle in a 200x200px (2km) contrast-stretched
crop - no line, no structure, no coastal edge nearby. They sit ~80m apart,
so this is either one vessel split into two CFAR blobs or two vessels
anchored close together - both plausible for an anchorage. These are the
first detections across every AOI tried in this project that visually look
like real ships rather than clutter, structure, or noise.

The shape filter labeled both BLOCKY, not SHIP-LIKE - see Limitations below
for why that label shouldn't be read as "not a ship."

---

## 3. False positive taxonomy

Every non-obvious AOI/config combination tried in this project produced a
distinct, identifiable false-positive mode. In the order encountered:

1. **Coastal mudflat/lagoon edges (Palk Strait).** A smooth, radar-dark
   mudflat/lagoon near Point Calimere, not resolved by Natural Earth's
   generalized 10m coastline. Threshold detections traced a curved line
   along the mudflat's edge - a boundary artifact, not vessels. Mean
   nearest-neighbor spacing of the cluster (~324m) was misleadingly
   consistent with "anchored fleet," which is why visual review matters
   more than spacing statistics alone.

2. **Resolution-limited speckle (Gulf of Mannar, 30m/px).** A wide AOI
   forced 30m/px to fit the download size cap. In a genuinely clean,
   0%-land box, all 40 CFAR-adjacent candidates were indistinguishable
   from ordinary sea-clutter speckle under zoomed inspection - no isolated
   bright point anywhere. Confirms the spec's warning that small/mid
   vessels are near the resolution limit; at 30m/px that limit is crossed.

3. **Port infrastructure (Tuticorin, west of 78.20E).** The first
   Tuticorin box included the breakwater/harbor complex. The 30
   highest-contrast detections (8-20.6dB contrast, which superficially
   matched the "10+dB = real vessel" heuristic) were unambiguously
   buildings, roads, and breakwater geometry on visual review - dense
   coastal development that a 10m-generalized coastline and even a 100m
   mask buffer didn't fully exclude.

4. **Unmapped linear features (this run, detections #5-12).** A single
   continuous bright line with a sharp bend (likely a pipeline, cable
   route, channel marker, or unmapped narrow shoal) sits inside a box that
   registered 0% land-masked. CA-CFAR + morphological opening fragmented
   it into 8 separate small blobs, each individually near-square in its
   own bounding box, so the shape filter didn't catch it as one line. See
   `cluster_6_12_crop.png`.

5. **Low-contrast speckle (detections #13-17, this run).** contrast_db
   6.1-8.4dB, no visible feature in any crop at all - the same
   invisible-in-the-image pattern as failure mode #2, just below the
   high-intensity flag threshold this time. See `detection_13_crop.png`.

Two detections (#1, #2) didn't fit cleanly into this taxonomy: #2 sat on
an unrelated bright streak (probably a wake or ambiguity artifact); #1 had
the single highest contrast_db of the whole run (22.4dB) yet showed no
visible feature in its crop at all - flagged as unexplained rather than
forced into a category.

---

## 4. Limitations

- **Shape filter fragments linear features.** The aspect/fill classifier
  operates per connected component *after* thresholding and morphological
  opening. A genuinely long, thin feature that gets broken into short
  disconnected pieces (by a local intensity dip, or by the opening step
  itself) will have each piece individually look compact/near-square, so
  it's misclassified as BLOCKY rather than LINEAR-STRUCTURE. This is why
  #3/#4 - real-looking blobs - came out BLOCKY rather than SHIP-LIKE: at
  10m/px, a SAR ship return is often dominated by one or two bright
  scatterers (bridge/superstructure) rather than a uniformly-bright filled
  hull silhouette, so the "aspect 2-8, fill>0.4" ship signature may simply
  not match how real vessels appear at this resolution and k. Shape
  labels here should be read as informative, not authoritative.

- **contrast_db alone is insufficient.** Land structures readily produce
  10-20+dB local contrast too (see false-positive mode #3) - the "real
  vessels are 10+dB above local background" heuristic is necessary but not
  sufficient. Every detection in this project that looked convincing on
  paper still required a zoomed visual crop to confirm or reject.

- **Reprojection order matters for the land mask.** Reprojecting the
  *entire* global Natural Earth land dataset into a small local UTM zone
  sends antipodal geometries to near-infinite coordinates and hangs.
  `land_mask.py` clips to the AOI in EPSG:4326 first, then reprojects only
  that small piece - a genuine correctness bug caught during this project,
  not just a performance tweak.

- **10m/px GeoTIFF downloads cap AOI size hard.** Earth Engine's
  `getDownloadURL` enforces a 48MiB request limit. Any AOI wider than
  roughly 10x10km needs a coarser scale to fit, which is a direct
  detectability tradeoff (see failure mode #2). Two vessel candidates were
  only found once the AOI was small enough to stay at full 10m/px
  resolution.

- **Sample size is two.** This is a proof of concept that the pipeline
  *can* surface plausible detections, not a validated vessel count. No
  ground truth (AIS, port records, etc.) confirms #3/#4 are actually
  vessels - that cross-reference is exactly what Milestone 2 is for.
