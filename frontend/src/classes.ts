import type { ConfClass } from "./api";

interface ClassInfo {
  label: string;
  color: string;
  blurb: string;
}

// Plain-language names; the blurb is what a judge reads to understand the class.
export const CLASSES: Record<ConfClass, ClassInfo> = {
  DARK_CANDIDATE: {
    label: "Dark vessel candidate",
    color: "#ff4d4f",
    blurb: "Looks like a ship on radar with no matching AIS signal -- either none within our radius, or GFW reports no AIS here (a reported absence, not a verified one). Needs human verification before any claim.",
  },
  UNVERIFIED_TARGET: {
    label: "Unverified target",
    color: "#fa8c16",
    blurb: "Strong, ship-sized radar return, but AIS status was never established (not checked / no reference). A candidate for review -- it cannot be called 'dark' without checking AIS.",
  },
  VESSEL_CANDIDATE: {
    label: "Likely vessel",
    color: "#36cfc9",
    blurb: "Bright, ship-sized return seen on one pass, with an AIS vessel nearby.",
  },
  ANCHORED_VESSEL: {
    label: "Ship at anchor",
    color: "#52c41a",
    blurb: "Same spot on several passes and the same AIS vessel every time.",
  },
  LOW_CONFIDENCE: {
    label: "Low confidence",
    color: "#faad14",
    blurb: "Bright but small or weak. Not enough evidence either way.",
  },
  PERSISTENT_UNIDENTIFIED: {
    label: "Persistent, unidentified",
    color: "#9254de",
    blurb: "Same spot on several passes with no consistent AIS identity. Could be fixed infrastructure OR a dark ship anchored for weeks -- persistence alone cannot tell them apart. Stays in the review queue.",
  },
  SUSPECTED_FIXED: {
    label: "Suspected fixed / reef",
    color: "#596e8c",
    blurb: "Pattern consistent with fixed infrastructure or a reef/shoal -- a collinear persistent chain, or a return pinned to one spot across passes -- but NOT confirmed. Could still be vessels. Stays in the review queue; this is a suspected label, not a charted one.",
  },
  FIXED_OBJECT: {
    label: "Fixed object (charted)",
    color: "#8c8c8c",
    blurb: "Confirmed fixed infrastructure from a charted feature (not inferred from persistence or geometry): a pipeline, platform or shoal, not a ship.",
  },
  CLUTTER: {
    label: "Sea clutter",
    color: "#434343",
    blurb: "Too faint to tell apart from ordinary sea noise.",
  },
};

export const CLASS_ORDER: ConfClass[] = [
  "DARK_CANDIDATE",
  "UNVERIFIED_TARGET",
  "PERSISTENT_UNIDENTIFIED",
  "SUSPECTED_FIXED",
  "VESSEL_CANDIDATE",
  "ANCHORED_VESSEL",
  "LOW_CONFIDENCE",
  "FIXED_OBJECT",
  "CLUTTER",
];
