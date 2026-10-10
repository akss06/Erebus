import type { ConfClass } from "./api";

interface ClassInfo {
  label: string;
  color: string;
  blurb: string;
}

// Plain-language names; the blurb is what a non-expert reads to understand the class.
// "Unmatched" is never called a dark vessel: a missing AIS signal is a lead, not proof.
export const CLASSES: Record<ConfClass, ClassInfo> = {
  DARK_CANDIDATE: {
    label: "No AIS match: needs review",
    color: "#ff4d4f",
    blurb: "Looks like a ship on radar, but no ship broadcasting its position was found here. A person needs to check it before drawing any conclusion.",
  },
  UNVERIFIED_TARGET: {
    label: "AIS not checked: needs review",
    color: "#fa8c16",
    blurb: "A strong, ship-sized radar return, but we had no AIS data for this spot, so we can't say whether it was broadcasting.",
  },
  VESSEL_CANDIDATE: {
    label: "Likely vessel",
    color: "#36cfc9",
    blurb: "Looks like a ship on radar, and a ship broadcasting its position (AIS) was nearby.",
  },
  ANCHORED_VESSEL: {
    label: "Ship at anchor",
    color: "#52c41a",
    blurb: "The same AIS-identified ship, seen at this spot on several satellite passes.",
  },
  LOW_CONFIDENCE: {
    label: "Low confidence",
    color: "#faad14",
    blurb: "Something bright but small or faint. Not enough to say either way.",
  },
  PERSISTENT_UNIDENTIFIED: {
    label: "Seen repeatedly, unidentified",
    color: "#9254de",
    blurb: "Something at this spot on several passes, with no AIS identity. It could be a structure or a ship anchored for a long time; radar alone can't tell them apart.",
  },
  SUSPECTED_FIXED: {
    label: "Possibly fixed object / reef",
    color: "#596e8c",
    blurb: "Behaves like a fixed object or reef (it never moves, or lines up with others). Not confirmed, so it stays in the review queue.",
  },
  FIXED_OBJECT: {
    label: "Fixed object (charted)",
    color: "#8c8c8c",
    blurb: "A structure shown on official charts, such as a platform or pipeline. Not a ship.",
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
