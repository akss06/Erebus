"""
Round 2: automatic confidence score for CFAR detections, replacing the hand-labelled
`visual_class` from Round 1.

Inputs are the per-pass detections already produced by the pipeline (same GeoJSON
properties: contrast_db, area_px, match_status, matched_mmsi, ...). Detections from
different passes over the same area are clustered by position; what each cluster
does across passes is the strongest evidence we have:

  - Same spot on 3+ passes AND the same AIS vessel each time -> anchored-vessel
    hypothesis (a ship repeatedly at anchor).
  - Same spot on 3+ passes but no consistent AIS identity -> PERSISTENT_UNIDENTIFIED:
    a stationary/recurring target whose nature is unresolved. It may be fixed
    infrastructure (pipeline, cable, shoal) OR a dark vessel anchored for weeks;
    persistence alone cannot tell them apart, so this stays a reviewable alert.
    FIXED_OBJECT (confirmed) is reserved for CHARTED evidence (det["context_fixed"]).
    Heuristic fixed-object signals -- membership of a collinear persistent chain
    (reef/shoal/causeway geometry, see find_chain_members), or a tight cross-pass
    position spread (<= FIXED_POSITION_SPREAD_M m) -- are classed SUSPECTED_FIXED:
    they support a reef/infrastructure explanation but do NOT confirm it, so they
    stay in the review queue, labelled suspected, not charted. The design is
    deliberately asymmetric: a positive signal promotes at most to SUSPECTED_FIXED
    (reviewable), ambiguity falls back to PERSISTENT_UNIDENTIFIED, and only charted
    evidence reaches FIXED_OBJECT -- so a long-anchored dark vessel is never silently
    dismissed. A strong single-pass target whose AIS status was never established is
    UNVERIFIED_TARGET, not DARK_CANDIDATE (which requires established no-AIS evidence).
  - Seen on one pass only -> judged on contrast, size and AIS evidence alone.

Evidence-semantics rules enforced here (see AUDIT_CHECKLIST §3, §4, §9):
  - The 0-100 `confidence` number is a HEURISTIC evidence/ranking score, not a
    calibrated probability. `score_basis` says so on every detection.
  - GFW's SAR-presence agreement is algorithmic agreement on the SAME Sentinel-1
    imagery, never described as an independent-sensor confirmation.
  - "AIS evidence" is kept distinct from "GFW association". `ais_source`
    distinguishes an AIS match we computed ourselves ("our_ais", Tuticorin) from an
    AIS identity GFW attributed to a nearby SAR record ("gfw_reported").

Run from the project root:  python src/confidence.py
"""
from __future__ import annotations

import glob
import json
import math
from collections import Counter
from pathlib import Path

CLUSTER_RADIUS_M = 100       # NEREUS PROGRESS drifted <60 m; 75 m split real fixed-object fragments, 125 m started over-merging
CLUSTER_DIAMETER_M = 100     # complete-linkage cap: a cluster's max pairwise distance may not exceed this (prevents transitive chains)
MIN_PASSES_FOR_FIXED = 3     # persistence needed to call something persistent / anchored
SAME_VESSEL_SHARE = 0.75     # share of passes with the top MMSI to call it "the same vessel"
STRONG_CONTRAST_DB = 15.0    # contrast level of every confirmed ship in Round 1
WEAK_CONTRAST_DB = 12.0      # below this, returns are speckle-like (recalibrated Oct 2026 after reviewing v3 crops; was 10.0)
MIN_AREA_PX = 8
FIXED_POSITION_SPREAD_M = 15 # max cross-pass centroid spread to call a persistent target position-stable (~1.5 px at 10 m GRD; tune from the spread histogram the rescorer prints)

# A long, thin, persistent chain of returns is reef/shoal/causeway geometry, not a
# vessel formation -- ships do not hold a straight multi-km line across passes.
CHAIN_MIN_MEMBERS = 4        # persistent clusters needed to call a run a chain
CHAIN_LINK_GAP_M = 1500      # max centroid gap to link two members into one run
CHAIN_PERP_RMS_M = 60        # max RMS perpendicular offset from the run's best-fit line
CHAIN_MIN_ASPECT = 4.0       # along-line extent / cross-line extent: a line, not a blob

SCORE_BASIS = "heuristic evidence/ranking score 0-100 (not a calibrated probability)"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CLASS_ORDER = ["ANCHORED_VESSEL", "VESSEL_CANDIDATE", "DARK_CANDIDATE", "UNVERIFIED_TARGET",
               "PERSISTENT_UNIDENTIFIED", "SUSPECTED_FIXED", "LOW_CONFIDENCE", "FIXED_OBJECT", "CLUTTER"]


def _dist_m(a: dict, b: dict) -> float:
    lat0 = math.radians((a["lat"] + b["lat"]) / 2)
    return math.hypot((a["lon"] - b["lon"]) * 111320 * math.cos(lat0), (a["lat"] - b["lat"]) * 110574)


def _cluster_spread_m(cluster: list[tuple[str, dict]]) -> float:
    """Max pairwise distance (m) between the detections in a cross-pass cluster.

    A fixed structure lands on the same pixel every pass (spread ~ geolocation
    jitter); a vessel swinging at anchor sweeps a circle roughly its rode length
    across, so a large spread argues against a fixed object. Used only to PROMOTE
    to FIXED_OBJECT below FIXED_POSITION_SPREAD_M -- never to downgrade."""
    pts = [d for _, d in cluster]
    return max((_dist_m(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]), default=0.0)


def _centroid(cluster: list[tuple[str, dict]]) -> dict:
    lons = [d["lon"] for _, d in cluster]
    lats = [d["lat"] for _, d in cluster]
    return {"lon": sum(lons) / len(lons), "lat": sum(lats) / len(lats)}


def _is_collinear(cents: list[dict]) -> bool:
    """True if the centroids form a thin line (small perpendicular RMS, high aspect)."""
    lat0 = math.radians(sum(c["lat"] for c in cents) / len(cents))
    xs = [c["lon"] * 111320 * math.cos(lat0) for c in cents]
    ys = [c["lat"] * 110574 for c in cents]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    xs = [x - mx for x in xs]
    ys = [y - my for y in ys]
    sxx = sum(x * x for x in xs)
    syy = sum(y * y for y in ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)  # principal-axis angle
    vx, vy = math.cos(theta), math.sin(theta)
    along = [x * vx + y * vy for x, y in zip(xs, ys)]
    perp = [-x * vy + y * vx for x, y in zip(xs, ys)]
    perp_rms = (sum(p * p for p in perp) / len(perp)) ** 0.5
    aspect = (max(along) - min(along)) / ((max(perp) - min(perp)) or 1.0)
    return perp_rms <= CHAIN_PERP_RMS_M and aspect >= CHAIN_MIN_ASPECT


def find_chain_members(clusters: list[list[tuple[str, dict]]]) -> set[int]:
    """ids of clusters belonging to a collinear run of persistent targets.

    Groups persistent-cluster centroids into runs (consecutive gaps <=
    CHAIN_LINK_GAP_M), then flags a run as a fixed reef/shoal/causeway feature if
    it is long and thin (see _is_collinear). A compact anchorage blob has low
    aspect ratio and is NOT flagged, so anchored vessels are not swept in."""
    persistent = [c for c in clusters if len(c) >= MIN_PASSES_FOR_FIXED]
    n = len(persistent)
    if n < CHAIN_MIN_MEMBERS:
        return set()
    cents = [_centroid(c) for c in persistent]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _dist_m(cents[i], cents[j]) <= CHAIN_LINK_GAP_M:
                parent[find(i)] = find(j)
    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)

    members: set[int] = set()
    for idxs in comps.values():
        if len(idxs) >= CHAIN_MIN_MEMBERS and _is_collinear([cents[i] for i in idxs]):
            members.update(id(persistent[i]) for i in idxs)
    return members


def cluster_across_passes(passes: dict[str, list[dict]]) -> list[list[tuple[str, dict]]]:
    """Group detections from different passes that sit at the same location.

    Complete-linkage, not single-linkage: two clusters merge only if EVERY
    cross-pair is within CLUSTER_RADIUS_M, so a merged cluster's diameter can
    never exceed CLUSTER_DIAMETER_M. This prevents transitive chains (A-B and
    B-C close but A-C far being merged into one location). Two detections from
    the same pass are never placed in one cluster."""
    items = [(d, x) for d, dets in passes.items() for x in dets]
    cluster_of = {id(x): [(d, x)] for d, x in items}
    pairs = []
    for i, (da, a) in enumerate(items):
        for db, b in items[i + 1:]:
            if da == db:
                continue
            dist = _dist_m(a, b)
            if dist <= CLUSTER_RADIUS_M:
                pairs.append((dist, da, a, db, b))

    def can_merge(ca: list, cb: list) -> bool:
        # no shared pass, and complete-linkage: all cross distances within the cap
        if {d for d, _ in ca} & {d for d, _ in cb}:
            return False
        for _, xa in ca:
            for _, xb in cb:
                if _dist_m(xa, xb) > CLUSTER_DIAMETER_M:
                    return False
        return True

    for dist, da, a, db, b in sorted(pairs, key=lambda t: t[0]):
        ca, cb = cluster_of[id(a)], cluster_of[id(b)]
        if ca is cb or not can_merge(ca, cb):
            continue
        ca.extend(cb)
        for _, x in cb:
            cluster_of[id(x)] = ca
    seen, out = set(), []
    for c in cluster_of.values():
        if id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def _ais_evidence(det: dict, ais_source: str) -> tuple[str, str]:
    """Return (ais_evidence, human phrase) for one detection, keeping GFW-reported
    AIS distinct from an AIS match we computed ourselves.

    ais_source:
      "our_ais"      -> match_status is a real AIS match done by us (Tuticorin).
      "gfw_reported" -> "match" means GFW attributed (or did not) an AIS identity
                        to a nearby SAR record; it is GFW's attribution, not ours.
    """
    status = det.get("match_status")
    name = det.get("matched_name") or "unknown"
    mmsi = det.get("matched_mmsi")
    dist = det.get("match_distance_m")

    if ais_source == "our_ais":
        if status == "MATCHED":
            return "matched", f"AIS vessel matched by us nearby ({name}, {dist:.0f} m)" if dist is not None else f"AIS vessel matched by us ({name})"
        return "unmatched", "no AIS vessel within our match radius"

    # gfw_reported
    if status == "MATCHED":
        return "gfw_reported_ais", f"GFW's SAR record here carries an AIS identity ({name}, MMSI {mmsi}) -- GFW-reported, not an independent AIS match"
    if det.get("gfw_association", "associated") == "none":
        return "unknown", "no GFW SAR record within radius and no AIS checked by us -> AIS status unknown"
    return "gfw_reported_no_ais", "GFW's SAR record here has no AIS identity -- GFW-reported absence, not a verified no-AIS observation"


def score_detection(det: dict, cluster: list[tuple[str, dict]], n_passes_total: int,
                    corroborated_by: str | None = None, ais_source: str = "gfw_reported",
                    chain_member: bool = False) -> dict:
    """Return the evidence summary for one detection given its cross-pass cluster.

    Output keys: confidence_class, confidence, score_basis, ais_evidence, reasons.

    corroborated_by: name of another algorithm that flagged the same object (e.g.
    GFW's SAR-presence product). This is algorithmic agreement on the SAME
    Sentinel-1 imagery -- NOT an independent sensor -- so it lowers the contrast bar
    for "candidate" from STRONG_CONTRAST_DB to WEAK_CONTRAST_DB only as an EXPERIMENTAL
    heuristic (chosen from a few Gulf of Mannar crops, not validated against labels).
    """
    contrast, area = det["contrast_db"], det["area_px"]
    n_seen = len(cluster)
    mmsis = [x.get("matched_mmsi") for _, x in cluster if x.get("matched_mmsi")]
    top_mmsi, top_n = (Counter(mmsis).most_common(1)[0] if mmsis else (None, 0))
    ais_ev, ais_phrase = _ais_evidence(det, ais_source)
    reasons = [f"contrast {contrast:.1f} dB, {area} px"]

    # contrast term scales 0..1 between WEAK and ~20 dB; size term caps at 20 px
    strength = min(1.0, max(0.0, (contrast - 6.0) / 14.0)) * 0.8 + min(1.0, area / 20) * 0.2

    # VH/VV cross-pol: an elevated VH return is consistent with (not proof of) a
    # hard/metal target; its absence does NOT demote (small wooden/fibreglass
    # trawlers have weak VH too). Heuristic increment, uncalibrated.
    if det.get("vh_corroborated"):
        reasons.append("cross-pol (VH/VV) elevated vs local sea: consistent with a hard/metal target (not conclusive)")
        strength = min(1.0, strength + 0.1)

    base = {"score_basis": SCORE_BASIS, "ais_evidence": ais_ev}

    if n_seen >= MIN_PASSES_FOR_FIXED:
        reasons.append(f"same spot on {n_seen}/{n_passes_total} passes")
        same_ident = top_n / n_seen >= SAME_VESSEL_SHARE and ais_source == "our_ais"
        if same_ident and contrast >= STRONG_CONTRAST_DB - 3:
            reasons.append(f"same AIS vessel (MMSI {top_mmsi}) on {top_n} passes -> anchored-vessel hypothesis")
            return {"confidence_class": "ANCHORED_VESSEL", "confidence": round(60 + 40 * strength), "reasons": reasons, **base}
        if det.get("context_fixed"):
            reasons.append("falls on a charted fixed-infrastructure feature -> fixed object")
            return {"confidence_class": "FIXED_OBJECT", "confidence": round(10 + 20 * (1 - strength)),
                    "fixed_evidence": "charted", "reasons": reasons, **base}
        if chain_member:
            reasons.append("member of a collinear persistent chain (reef/shoal/causeway geometry, not a vessel formation) -> SUSPECTED fixed, not confirmed")
            reasons.append("heuristic, not charted: the pattern supports a reef/infrastructure explanation but does not confirm it -- stays reviewable")
            return {"confidence_class": "SUSPECTED_FIXED", "confidence": round(20 + 20 * strength),
                    "fixed_evidence": "chain_geometry", "reasons": reasons, **base}
        spread = _cluster_spread_m(cluster)
        if spread <= FIXED_POSITION_SPREAD_M:
            reasons.append(f"position-stable: max cross-pass spread {spread:.0f} m <= {FIXED_POSITION_SPREAD_M:.0f} m -> SUSPECTED fixed, not confirmed")
            reasons.append("heuristic, not charted: a vessel on a short scope or pinned by current could also stay within this radius -- stays reviewable")
            return {"confidence_class": "SUSPECTED_FIXED", "confidence": round(20 + 20 * strength),
                    "fixed_evidence": "position_stable", "reasons": reasons, **base}
        # persistent but position moves across passes: identity unresolved and it is
        # not pinned like a structure -- could be a dark ship swinging at anchor -> reviewable.
        if len(set(mmsis)) > 1:
            reasons.append(f"'matched' AIS identity differs between passes ({len(set(mmsis))} ships) -- coarse associations, not a stable identity")
        else:
            reasons.append("no consistent AIS identity across passes")
        reasons.append(f"persistent but position spreads {spread:.0f} m across passes (> {FIXED_POSITION_SPREAD_M:.0f} m): not pinned like a structure -- infrastructure vs anchored dark vessel unresolved -> review")
        return {"confidence_class": "PERSISTENT_UNIDENTIFIED", "confidence": round(35 + 25 * strength), "reasons": reasons, **base}

    if n_seen == 2:
        reasons.append("seen on 2 passes only; persistence inconclusive")

    if contrast < WEAK_CONTRAST_DB:
        reasons.append(f"contrast below {WEAK_CONTRAST_DB:.0f} dB evidence threshold: not separable from sea clutter at the current bar")
        return {"confidence_class": "CLUTTER", "confidence": round(30 * strength), "reasons": reasons, **base}

    min_contrast = WEAK_CONTRAST_DB if corroborated_by else STRONG_CONTRAST_DB
    if corroborated_by:
        reasons.append(f"also flagged by {corroborated_by} on the same Sentinel-1 imagery (shared-sensor algorithmic agreement, not independent confirmation; lowered bar is experimental)")
    if contrast >= min_contrast and area >= MIN_AREA_PX:
        reasons.append(ais_phrase)
        if ais_ev == "matched":
            return {"confidence_class": "VESSEL_CANDIDATE", "confidence": round(50 + 40 * strength), "reasons": reasons, **base}
        if ais_ev in ("gfw_reported_ais",):
            # GFW says a vessel with AIS is here; we did not match AIS ourselves.
            return {"confidence_class": "VESSEL_CANDIDATE", "confidence": round(45 + 35 * strength), "reasons": reasons, **base}
        if ais_ev in ("unmatched", "gfw_reported_no_ais"):
            # established no-AIS evidence (our own radius check, or GFW's reported absence)
            return {"confidence_class": "DARK_CANDIDATE", "confidence": round(40 + 40 * strength), "reasons": reasons, **base}
        # ais_ev == "unknown": AIS status was never established -> we cannot assert "dark".
        reasons.append("AIS status not established (no GFW record and no AIS checked by us): a strong target, but 'dark' cannot be asserted -> verify AIS")
        return {"confidence_class": "UNVERIFIED_TARGET", "confidence": round(35 + 35 * strength), "reasons": reasons, **base}

    reasons.append("bright but small or weak: not enough evidence either way")
    return {"confidence_class": "LOW_CONFIDENCE", "confidence": round(20 + 30 * strength), "reasons": reasons, **base}


def load_passes() -> dict[str, list[dict]]:
    def load(path: Path) -> list[dict]:
        feats = json.loads(path.read_text(encoding="utf-8"))["features"]
        return [dict(f["properties"], lon=f["geometry"]["coordinates"][0], lat=f["geometry"]["coordinates"][1]) for f in feats]

    passes = {"2026-01-18": load(DATA / "matched_detections.geojson")}
    for p in sorted(glob.glob(str(DATA / "multi_date" / "*_detections.geojson"))):
        passes[Path(p).name[:10]] = load(Path(p))
    return passes


def score_all(passes: dict[str, list[dict]], ais_source: str = "our_ais") -> list[dict]:
    """Tuticorin scoring: AIS here is our own match (ais_source='our_ais')."""
    clusters = cluster_across_passes(passes)
    # stable ids: clusters ordered by position, detections numbered within their pass
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    chains = find_chain_members(clusters)
    out, per_date = [], Counter()
    for cluster_id, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            res = score_detection(det, cluster, len(passes), ais_source=ais_source,
                                  chain_member=id(cluster) in chains)
            per_date[date] += 1
            out.append({**det, "id": f"{date}-{per_date[date]:02d}", "date": date,
                        "cluster_id": cluster_id, "cluster_size": len(cluster), **res})
    return out


def main() -> None:
    passes = load_passes()
    scored = score_all(passes)
    scored.sort(key=lambda d: d["id"])
    features = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
         "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")}}
        for d in scored
    ]
    provenance = {
        "generated_by": "src/confidence.py",
        "kind": "Tuticorin cross-pass scoring (our own AIS match)",
        "ais_source": "our_ais",
        "scoring_semantics": "PERSISTENT_UNIDENTIFIED, GFW!=AIS, heuristic score",
        "status": "ok",
        "n_features": len(features),
    }
    (DATA / "scored_detections.geojson").write_text(json.dumps({"type": "FeatureCollection", "provenance": provenance, "features": features}, indent=1), encoding="utf-8")

    print(f"{len(scored)} detections over {len(passes)} passes")
    print("Class counts:", dict(Counter(d["confidence_class"] for d in scored)))
    print("\n2026-01-18 vs Round 1 hand labels:")
    print(f"{'#':>3} {'hand label':17} {'auto class':21} {'conf':>4}  reasons")
    for d in scored:
        if d["date"] == "2026-01-18":
            print(f"{d['rank']:>3} {d['visual_class']:17} {d['confidence_class']:21} {d['confidence']:>4}  {'; '.join(d['reasons'][1:])}")


if __name__ == "__main__":
    main()
