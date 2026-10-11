"""
Combine the teammates' blind-labelling answers (erebus-labels-<name>.json, downloaded
from Erebus-Blind-Labelling.html) with blind_key.json.

Reports, for the review queue and for the hidden clutter controls separately: each
labeller's ship / not / can't-tell shares, the majority view per image, and agreement
(mean pairwise % agreement and Fleiss' kappa over images every labeller answered).
Writes audit/results/blind_labelling.json.

Run from the project root:  python -P -E audit/labelling/score_labels.py <folder with label files>
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "audit" / "results" / "blind_labelling.json"
CATS = ("ship", "not", "unsure")


def fleiss_kappa(rows: list[Counter], n: int) -> float | None:
    """rows: per-image category counts, each summing to n raters."""
    if len(rows) < 2 or n < 2:
        return None
    p_j = [sum(r[c] for r in rows) / (len(rows) * n) for c in CATS]
    p_i = [(sum(r[c] ** 2 for c in CATS) - n) / (n * (n - 1)) for r in rows]
    p_bar, p_e = sum(p_i) / len(p_i), sum(p * p for p in p_j)
    return None if p_e == 1 else round((p_bar - p_e) / (1 - p_e), 3)


def majority(c: Counter) -> str:
    top = c.most_common()
    if not top or (len(top) > 1 and top[0][1] == top[1][1]):
        return "split"
    return top[0][0]


def summarise(items: list[dict], raters: dict[str, dict]) -> dict:
    codes = [i["code"] for i in items]
    per_rater = {}
    for name, labels in raters.items():
        got = [labels[c] for c in codes if c in labels]
        per_rater[name] = {"answered": len(got), **{c: round(got.count(c) / len(got), 3) if got else None for c in CATS}}
    counts = {c: Counter(labels[c] for labels in raters.values() if c in labels) for c in codes}
    maj = Counter(majority(counts[c]) for c in codes if counts[c])
    full = [counts[c] for c in codes if sum(counts[c].values()) == len(raters)]
    pairs = [sum(raters[a][c] == raters[b][c] for c in codes if c in raters[a] and c in raters[b]) /
             max(1, sum(c in raters[a] and c in raters[b] for c in codes))
             for a, b in combinations(raters, 2)]
    return {"n_images": len(codes), "per_labeller": per_rater,
            "majority": {k: maj.get(k, 0) for k in (*CATS, "split")},
            "mean_pairwise_agreement": round(sum(pairs) / len(pairs), 3) if pairs else None,
            "fleiss_kappa": fleiss_kappa(full, len(raters)), "kappa_images": len(full)}


def main(folder: Path) -> None:
    key = json.loads((HERE / "blind_key.json").read_text(encoding="utf-8"))
    raters: dict[str, dict] = {}
    for f in sorted(folder.glob("erebus-labels-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        labels = {c: v for c, v in d.get("labels", {}).items() if v in CATS}
        name = d.get("name") or f.stem
        while name in raters:
            name += "_"
        raters[name] = labels
    if not raters:
        sys.exit(f"no erebus-labels-*.json files in {folder}")

    queue = [i for i in key if i["set"] == "queue"]
    result = {"labellers": list(raters), "queue": summarise(queue, raters),
              "clutter_controls": summarise([i for i in key if i["set"] == "clutter_control"], raters),
              "queue_by_class": {cls: summarise([i for i in queue if i["class"] == cls], raters)
                                 for cls in sorted({i["class"] for i in queue})}}
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for part in ("queue", "clutter_controls"):
        s = result[part]
        print(f"{part}: {s['n_images']} images, majority {s['majority']}, "
              f"agreement {s['mean_pairwise_agreement']}, Fleiss kappa {s['fleiss_kappa']}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
