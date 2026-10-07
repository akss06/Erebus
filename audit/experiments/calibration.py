"""
EXPERIMENT (AUDIT §1.5): is the GG-CFAR nominal P_fa = 1e-5 achieved?

Reproduces the reported Gamma(shape=2, scale=0.5) trimming counterexample across
seeds and compares the detector's default fit (trim top 1% -> gengamma MLE ->
extrapolate to 1-pfa) against alternatives. We fit on a training sample, normalise
by its mean (as the detector normalises intensity by the local clutter mean), get
alpha, then measure the EXCEEDANCE of a fresh held-out sample above alpha.

This is a synthetic calibration check on known i.i.d. clutter -- NOT a field
false-alarm rate. Target contamination, background heterogeneity and spatial
correlation are not modelled here.

Run:  python audit/experiments/calibration.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import gamma, gengamma

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
import detect_v3 as D  # noqa: E402

PFA = 1e-5
N_TRAIN = 200_000
N_HELD = 5_000_000
SEEDS = range(5)
SHAPE, SCALE = 2.0, 0.5


def alpha_default(norm):          # the detector's real code path
    a, _ = D._fit_shape_alpha(norm, PFA)
    return a


def alpha_fit(norm, dist, trim):
    x = norm[np.isfinite(norm) & (norm > 0)]
    if trim:
        x = x[x <= np.quantile(x, 0.99)]
    if x.size > 8000:
        x = np.random.default_rng(0).choice(x, 8000, replace=False)
    params = dist.fit(x, floc=0)
    return float(dist.ppf(1 - PFA, *params[:-2], loc=0, scale=params[-1]))


def alpha_empirical(norm):
    return float(np.quantile(norm, 1 - PFA))


METHODS = {
    "trim+gengamma (detector default)": alpha_default,
    "gengamma_no_trim": lambda n: alpha_fit(n, gengamma, False),
    "gamma_no_trim": lambda n: alpha_fit(n, gamma, False),
    "empirical_quantile": alpha_empirical,
}


def main():
    results = {m: [] for m in METHODS}
    for seed in SEEDS:
        rng = np.random.default_rng(seed)
        train = rng.gamma(SHAPE, SCALE, size=N_TRAIN)
        held = rng.gamma(SHAPE, SCALE, size=N_HELD)
        mean_train = train.mean()
        norm_train = train / mean_train
        norm_held = held / mean_train
        for name, fn in METHODS.items():
            try:
                a = fn(norm_train)
                exc = float(np.mean(norm_held > a))
                results[name].append({"seed": seed, "alpha": a, "achieved_pfa": exc})
            except Exception as e:
                results[name].append({"seed": seed, "error": str(e)})

    summary = {}
    for name, runs in results.items():
        exc = [r["achieved_pfa"] for r in runs if "achieved_pfa" in r]
        summary[name] = {
            "mean_achieved_pfa": float(np.mean(exc)) if exc else None,
            "std_achieved_pfa": float(np.std(exc)) if exc else None,
            "ratio_to_nominal": float(np.mean(exc) / PFA) if exc else None,
        }

    out = {"nominal_pfa": PFA, "clutter": f"Gamma(shape={SHAPE}, scale={SCALE})",
           "n_train": N_TRAIN, "n_held": N_HELD, "seeds": list(SEEDS),
           "per_method": summary, "raw": results}
    (ROOT / "audit" / "results" / "calibration.json").write_text(json.dumps(out, indent=2))

    print(f"Nominal P_fa = {PFA:.0e}   clutter = Gamma({SHAPE},{SCALE})   "
          f"N_held={N_HELD:,} x {len(list(SEEDS))} seeds\n")
    print(f"{'method':38} {'mean achieved P_fa':>20} {'x nominal':>12}")
    for name, s in summary.items():
        if s["mean_achieved_pfa"] is not None:
            print(f"{name:38} {s['mean_achieved_pfa']:>20.2e} {s['ratio_to_nominal']:>11.1f}x")


if __name__ == "__main__":
    main()
