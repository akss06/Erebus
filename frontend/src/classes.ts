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
    blurb: "Looks like a ship on radar and no AIS vessel is nearby. Needs human verification before any claim.",
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
  FIXED_OBJECT: {
    label: "Fixed object",
    color: "#8c8c8c",
    blurb: "Same spot on several passes with no consistent AIS vessel: a pipeline, cable or shoal, not a ship.",
  },
  CLUTTER: {
    label: "Sea clutter",
    color: "#434343",
    blurb: "Too faint to tell apart from ordinary sea noise.",
  },
};

export const CLASS_ORDER: ConfClass[] = [
  "DARK_CANDIDATE",
  "VESSEL_CANDIDATE",
  "ANCHORED_VESSEL",
  "LOW_CONFIDENCE",
  "FIXED_OBJECT",
  "CLUTTER",
];
