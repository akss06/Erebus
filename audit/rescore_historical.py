"""
Conservative semantic re-score of the FROZEN baseline detections (AUDIT §3, §4, §5, §9).

This does NOT re-run any detector. It reads the already-produced scored GeoJSON
(detection geometry + radar features are left exactly as the baseline detector
emitted them), re-clusters with the complete-linkage diameter cap, and re-applies
the corrected confidence semantics:
  - PERSISTENT_UNIDENTIFIED instead of persistence-inferred FIXED_OBJECT,
  - GFW association kept distinct from AIS evidence (ais_source="gfw_reported"),
  - GFW agreement labelled as shared-sensor, not independent,
  - heuristic score_basis recorded.

It is a relabelling of historical results, explicitly marked as such -- not a
claim that the detector changed or improved. Baseline hashes are in
audit/baseline_manifest.json; original files are recoverable from git 549ed4a.

Usage:  python audit/rescore_historical.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import confidence as C  # noqa: E402

DATA = ROOT / "data"

# total acquisition passes attempted over each area (for the "n/N passes" text);
# recent used the 8 RECENT_DATES. Gulf's attempts aren't recorded offline, so we
# fall back to the distinct dates present (noted as a display-only approximation).
N_PASSES = {"scored_recent.geojson": 8}


def rescore(fname: str) -> Counter:
    path = DATA / fname
    data = json.loads(path.read_text(encoding="utf-8"))
    dets = []
    for f in data["features"]:
        p = dict(f["properties"])
        p["lon"], p["lat"] = f["geometry"]["coordinates"]
        dets.append(p)

    by_date: dict[str, list[dict]] = {}
    for d in dets:
        if d.get("gfw_association") is None:
            d["gfw_association"] = "associated" if d.get("match_distance_m") is not None else "none"
        by_date.setdefault(d["date"], []).append(d)

    # best-effort historical ambiguity: one AIS identity attributed to several
    # detections on one date -> the coarse GFW record can't be a confident identity.
    for day in by_date.values():
        mc = Counter(d.get("matched_mmsi") for d in day if d.get("matched_mmsi"))
        for d in day:
            if d.get("matched_mmsi") and mc[d["matched_mmsi"]] > 1 and d["gfw_association"] == "associated":
                d["gfw_association"] = "ambiguous"

    clusters = C.cluster_across_passes(by_date)
    clusters.sort(key=lambda c: (round(c[0][1]["lat"], 4), round(c[0][1]["lon"], 4)))
    n_passes = N_PASSES.get(fname, len(by_date))

    # cross-pass spread of persistent clusters, to calibrate FIXED_POSITION_SPREAD_M
    spreads = sorted(C._cluster_spread_m(c) for c in clusters if len(c) >= C.MIN_PASSES_FOR_FIXED)
    if spreads:
        thr = C.FIXED_POSITION_SPREAD_M
        n_le = sum(s <= thr for s in spreads)
        qs = [spreads[min(len(spreads) - 1, int(q * len(spreads)))] for q in (0.0, 0.25, 0.5, 0.75, 1.0)]
        print(f"  {fname}: {len(spreads)} persistent clusters; spread m "
              f"[min/p25/med/p75/max] = {'/'.join(f'{q:.0f}' for q in qs)}; "
              f"{n_le} <= {thr} m -> position-stable (FIXED_OBJECT)")

    chains = C.find_chain_members(clusters)
    out = []
    for cid, cluster in enumerate(clusters, start=1):
        for date, det in sorted(cluster, key=lambda m: m[0]):
            corrob = "GFW's SAR-presence algorithm" if det.get("gfw_association") in ("associated", "ambiguous") else None
            res = C.score_detection(det, cluster, n_passes, corroborated_by=corrob,
                                    ais_source="gfw_reported", chain_member=id(cluster) in chains)
            det.update(cluster_id=cid, cluster_size=len(cluster), **res)
            out.append(det)

    out.sort(key=lambda d: d["id"])
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
              "properties": {k: v for k, v in d.items() if k not in ("lon", "lat")}} for d in out]
    provenance = {
        "generated_by": "audit/rescore_historical.py",
        "kind": "semantic re-score of frozen baseline detections (NOT a detector re-run)",
        "baseline_detector_git_rev": "549ed4a",
        "scoring_semantics": "confidence.py v-audit (PERSISTENT_UNIDENTIFIED, GFW!=AIS, heuristic score)",
        "ais_source": "gfw_reported",
        "status": "ok",
        "n_features": len(feats),
    }
    path.write_text(json.dumps({"type": "FeatureCollection", "provenance": provenance, "features": feats}, indent=1), encoding="utf-8")
    return Counter(d["confidence_class"] for d in out)


def main() -> None:
    for fname in ("scored_recent.geojson", "scored_gulf.geojson"):
        counts = rescore(fname)
        print(f"{fname}: {dict(counts)}")


if __name__ == "__main__":
    main()
