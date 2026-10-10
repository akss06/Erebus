"""Focused regression tests for the audit fixes (AUDIT_CHECKLIST §1, §5, §6, §8).

Run:  python -m pytest tests/test_audit_fixes.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import confidence as C
import detect_v3 as D


# ---------------------------------------------------------------- §8 numerical

def test_local_mean_no_nan_propagation():
    """NaN in intensity must not poison the box-filtered local mean (§8.1)."""
    intensity = np.ones((40, 40), float)
    intensity[0, 0] = np.nan          # a single non-finite pixel
    clutter = np.ones((40, 40), bool)
    lm, enough = D._local_clutter_mean(intensity, clutter, min_count=1)
    # the center is far from the NaN and from edges; must be finite and ~1.0
    assert np.isfinite(lm[20, 20])
    assert abs(lm[20, 20] - 1.0) < 1e-9
    # NaN must not have spread across the array
    assert np.isfinite(lm[20:30, 20:30]).all()


def test_local_mean_insufficient_support():
    """Fewer than min_count clutter pixels in the ring -> NaN + not-enough (§1.4)."""
    intensity = np.ones((60, 60), float)
    clutter = np.zeros((60, 60), bool)
    clutter[0, 0] = True              # one lone clutter pixel, nowhere near center
    lm, enough = D._local_clutter_mean(intensity, clutter, min_count=12)
    assert not enough[30, 30]
    assert np.isnan(lm[30, 30])


def test_fit_insufficient_samples():
    """Too few samples -> inf alpha + 'insufficient' (§1.4, §8.2)."""
    alpha, diag = D._fit_shape_alpha(np.ones(50), 1e-5)
    assert not np.isfinite(alpha)
    assert diag["dist"] == "insufficient"


def test_fit_reports_distribution_and_alpha():
    """A valid clutter sample yields a finite alpha and recorded provenance (§1.3)."""
    rng = np.random.default_rng(1)
    x = rng.gamma(2.0, 0.5, size=50000)
    norm = x / x.mean()
    alpha, diag = D._fit_shape_alpha(norm, 1e-5)
    assert np.isfinite(alpha) and alpha > 1
    assert diag["dist"] in ("gengamma", "gamma", "empirical")
    assert diag["n_samples"] == norm.size
    assert diag["pfa_nominal"] == 1e-5


def test_all_masked_tile_is_degraded_not_zero():
    """An all-masked / no-sea tile returns an explicit status, not a silent [] (§8.2)."""
    db = np.full((50, 50), -20.0)
    sea = np.zeros((50, 50), bool)
    dets, diag = D.detect_blobs_v3(db, sea)
    assert dets == []
    assert diag["status"] == "insufficient_data"


# ---------------------------------------------------------------- §6 masking

def test_pixel_metres_metric_vs_geographic():
    from rasterio.transform import Affine
    from rasterio.crs import CRS
    tf = Affine(10.0, 0, 500000, 0, -10.0, 1000000)
    y, x = D._pixel_metres(tf, CRS.from_epsg(32644), 9.0)
    assert abs(y - 10.0) < 1e-6 and abs(x - 10.0) < 1e-6
    tfg = Affine(0.0001, 0, 79.0, 0, -0.0001, 9.0)
    yg, xg = D._pixel_metres(tfg, CRS.from_epsg(4326), 9.0)
    assert 10 < yg < 12 and 10 < xg < 12   # ~0.0001 deg ~ 11 m near the equator


def test_jrc_undeclared_fill_is_no_data_not_land(tmp_path):
    """Open-ocean JRC downloads carry -128 without a nodata tag; that must read as
    'no JRC data', never as low water occurrence (which masked offshore tiles as land)."""
    import rasterio
    from rasterio.transform import from_origin
    tif = tmp_path / "jrc.tif"
    occ = np.full((20, 20), -128, dtype="int8")
    occ[:, :5] = 100                      # a strip of real permanent water
    tf = from_origin(78.0, 9.0, 0.001, 0.001)
    with rasterio.open(tif, "w", driver="GTiff", height=20, width=20, count=1,
                       dtype="int8", crs="EPSG:4326", transform=tf) as dst:
        dst.write(occ, 1)
    water, has = D._jrc_water_on_grid(None, tf, (20, 20), "EPSG:4326", tif)
    assert not has[:, 8:].any()           # fill -> no data (so it cannot mark land)
    assert water[:, :4].all()


# ---------------------------------------------------------------- §5 clustering

def _det(lon, lat):
    return {"lon": lon, "lat": lat, "contrast_db": 20.0, "area_px": 10}


def test_complete_linkage_blocks_transitive_chain():
    """A-B ~90 m and B-C ~90 m but A-C ~180 m must NOT collapse into one cluster (§5.1)."""
    # three points on a line, each ~90 m apart (0.0008 deg lat ~ 88 m)
    passes = {
        "d1": [_det(79.0, 9.0000)],
        "d2": [_det(79.0, 9.0008)],
        "d3": [_det(79.0, 9.0016)],
    }
    clusters = C.cluster_across_passes(passes)
    sizes = sorted(len(c) for c in clusters)
    # B merges with one neighbour at most; A-C (>100 m) can never share a cluster
    assert max(sizes) <= 2
    for cl in clusters:
        coords = [(d["lon"], d["lat"]) for _, d in cl]
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                assert C._dist_m({"lon": coords[i][0], "lat": coords[i][1]},
                                 {"lon": coords[j][0], "lat": coords[j][1]}) <= C.CLUSTER_DIAMETER_M


def test_same_pass_never_merged():
    passes = {"d1": [_det(79.0, 9.0), _det(79.0, 9.0001)]}
    clusters = C.cluster_across_passes(passes)
    assert all(len(c) == 1 for c in clusters)


# ---------------------------------------------------------------- §3/§4 semantics

def test_persistent_but_spread_out_is_unidentified_not_fixed():
    # same ~100 m cluster but the position moves across passes (spread > threshold):
    # not pinned like a structure -> stays reviewable, never promoted to FIXED.
    d1, d2, d3 = _det(79, 9.0), _det(79, 9.0003), _det(79, 9.0006)  # ~33 m, ~66 m apart
    cluster = [("d1", d1), ("d2", d2), ("d3", d3)]
    det = dict(d1, match_status="UNMATCHED", gfw_association="none")
    assert C._cluster_spread_m(cluster) > C.FIXED_POSITION_SPREAD_M
    res = C.score_detection(det, cluster, 3, ais_source="gfw_reported")
    assert res["confidence_class"] == "PERSISTENT_UNIDENTIFIED"
    assert res["score_basis"].startswith("heuristic")


def test_collinear_chain_is_suspected_fixed_but_blob_is_not():
    # eight persistent clusters in a straight ~1.6 km E-W line (reef/shoal geometry):
    # every member -> SUSPECTED_FIXED (heuristic, reviewable), NOT confirmed FIXED_OBJECT.
    line_clusters = [[(f"c{i}", d) for d in [_det(79.0 + i * 0.002, 9.0)] * 3] for i in range(8)]
    chains = C.find_chain_members(line_clusters)
    assert len(chains) == 8
    det = dict(_det(79.0, 9.0), match_status="UNMATCHED", gfw_association="none")
    res = C.score_detection(det, line_clusters[0], 8, ais_source="gfw_reported",
                            chain_member=id(line_clusters[0]) in chains)
    assert res["confidence_class"] == "SUSPECTED_FIXED"
    assert res["fixed_evidence"] == "chain_geometry"
    # a compact 3x3 blob of persistent clusters (an anchorage) is NOT a chain
    blob = [[(f"b{i}{j}", d) for d in [_det(79.0 + i * 0.0005, 9.0 + j * 0.0005)] * 3]
            for i in range(3) for j in range(3)]
    assert C.find_chain_members(blob) == set()


def test_position_stable_cluster_is_suspected_not_confirmed_fixed():
    # pinned across passes -> SUSPECTED_FIXED (heuristic, reviewable), not FIXED_OBJECT.
    cluster = [("d1", _det(79, 9)), ("d2", _det(79, 9)), ("d3", _det(79, 9))]
    det = dict(_det(79, 9), match_status="UNMATCHED", gfw_association="none")
    res = C.score_detection(det, cluster, 3, ais_source="gfw_reported")
    assert res["confidence_class"] == "SUSPECTED_FIXED"
    assert res["fixed_evidence"] == "position_stable"


def test_unknown_ais_is_unverified_not_dark():
    # strong single-pass target with no GFW record and no AIS checked -> AIS unknown.
    # cannot be called "dark"; must be UNVERIFIED_TARGET.
    cluster = [("d1", _det(79, 9))]
    det = dict(_det(79, 9), contrast_db=20.0, area_px=12, match_status="UNMATCHED", gfw_association="none")
    res = C.score_detection(det, cluster, 1, ais_source="gfw_reported")
    assert res["ais_evidence"] == "unknown"
    assert res["confidence_class"] == "UNVERIFIED_TARGET"


def test_gfw_reported_no_ais_is_dark_candidate():
    # strong single-pass target, GFW record present but no MMSI -> established no-AIS -> DARK.
    cluster = [("d1", _det(79, 9))]
    det = dict(_det(79, 9), contrast_db=20.0, area_px=12, match_status="UNMATCHED", gfw_association="associated")
    res = C.score_detection(det, cluster, 1, ais_source="gfw_reported")
    assert res["ais_evidence"] == "gfw_reported_no_ais"
    assert res["confidence_class"] == "DARK_CANDIDATE"


def test_context_fixed_allows_fixed_object():
    cluster = [("d1", _det(79, 9)), ("d2", _det(79, 9)), ("d3", _det(79, 9))]
    det = dict(_det(79, 9), match_status="UNMATCHED", context_fixed=True)
    res = C.score_detection(det, cluster, 3)
    assert res["confidence_class"] == "FIXED_OBJECT"


def test_gfw_reported_ais_is_not_called_our_match():
    cluster = [("d1", _det(79, 9))]
    det = dict(_det(79, 9), match_status="MATCHED", matched_mmsi="123", matched_name="X",
               match_distance_m=200.0, gfw_association="associated")
    res = C.score_detection(det, cluster, 1, corroborated_by="GFW's SAR-presence algorithm",
                            ais_source="gfw_reported")
    assert res["ais_evidence"] == "gfw_reported_ais"
    joined = " ".join(res["reasons"]).lower()
    assert "gfw-reported" in joined and "independent" in joined
