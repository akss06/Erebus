export type ConfClass =
  | "ANCHORED_VESSEL"
  | "VESSEL_CANDIDATE"
  | "DARK_CANDIDATE"
  | "UNVERIFIED_TARGET"
  | "PERSISTENT_UNIDENTIFIED"
  | "SUSPECTED_FIXED"
  | "LOW_CONFIDENCE"
  | "FIXED_OBJECT"
  | "CLUTTER";

export interface Area {
  id: string;
  name: string;
  center: [number, number];
  zoom: number;
  overlay: { image: string; bounds: [[number, number], [number, number]] } | null;
  detections: number;
  passes: string[];
  classes: Partial<Record<ConfClass, number>>;
}

export interface Detection {
  id: string;
  area_id: string;
  date: string;
  lon: number;
  lat: number;
  confidence_class: ConfClass;
  confidence: number;
  score_basis?: string;
  ais_evidence?: string;
  gfw_association?: string;
  fixed_evidence?: string;
  reasons: string[];
  contrast_db: number;
  area_px: number;
  shape_label: string;
  match_status: string;
  match_distance_m?: number;
  matched_name?: string;
  matched_mmsi?: string;
  matched_flag?: string;
  matched_type?: string;
  cluster_id?: number | string;
  cluster_size?: number;
  provenance: string;
  crop_url: string | null;
  simulated?: boolean;
  eos04?: Eos04Check;
}

// Same-day check against India's EOS-04 radar satellite (29 Aug 2026 pass only).
export interface Eos04Check {
  seen: boolean;
  distance_m: number;
  s1_time_utc: string;
  eos04_time_utc: string;
  crop_url: string | null;
}

export interface Review {
  detection_id: string;
  verdict: "confirm" | "reject" | "unsure";
  note: string;
}

export interface DetectionDetail extends Detection {
  other_passes: Detection[];
  review: Review | null;
}

export interface Cluster {
  cluster_id: string;
  area_id: string;
  lon: number;
  lat: number;
  confidence_class: ConfClass;
  confidence: number;
  passes_seen: number;
  dates: string[];
  best_detection_id: string;
  reasons: string[];
  matched_names: string[];
  provenance: string;
  simulated?: boolean;
  review?: Review | null;
}

// Backend base URL. Empty in dev (requests hit /api, proxied to localhost:8000 by
// vite.config); in production set VITE_API_BASE to the deployed backend origin.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export interface SimVessel {
  mmsi: string;
  name: string | null;
  detections: number;
}

const q = (sim: string | null) => (sim ? `&sim=${sim}` : "");

export interface AnalyzeResult {
  run_id: string;
}

export interface AnalyzeStatus {
  status: "starting" | "running" | "done" | "error";
  progress: number;
  log: string[];
  result: {
    total: number;
    dark_candidates: number;
    classes: Record<string, number>;
    coverage?: { requested: number; dates?: number; no_scene: number; download_failed: number; analyzed: number; no_sea: number; degraded: number; gfw_failed_dates?: number; detections_kept: number };
    gfw_failed_dates?: string[];
    partial?: boolean;
    data_unavailable?: boolean;
    note?: string;
  } | null;
  area_id: string | null;
  error: string | null;
}

export interface Health {
  status: string;
  detections: number;
  live_analysis: boolean;
}

export const api = {
  health: () => get<Health>("/api/health"),
  areas: () => get<Area[]>("/api/areas"),
  clusters: (area: string, sim: string | null = null) => get<Cluster[]>(`/api/clusters?area=${area}${q(sim)}`),
  detections: (area: string, date: string, sim: string | null = null) =>
    get<Detection[]>(`/api/detections?area=${area}&date=${date}${q(sim)}`),
  detection: (id: string, sim: string | null = null) => get<DetectionDetail>(`/api/detections/${id}?${q(sim).slice(1)}`),
  alerts: (area: string, sim: string | null = null) => get<Cluster[]>(`/api/alerts?area=${area}${q(sim)}`),
  simVessels: () => get<SimVessel[]>("/api/simulation/vessels"),
  review: async (r: Review): Promise<Review> => {
    const res = await fetch(`${API_BASE}/api/reviews`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(r),
    });
    if (!res.ok) throw new Error(`review -> ${res.status}`);
    return res.json();
  },
  analyze: async (region: [number, number, number, number], startDate: string, endDate: string, maxPerDate = 10): Promise<AnalyzeResult> => {
    const res = await fetch(`${API_BASE}/api/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region, start_date: startDate, end_date: endDate, max_per_date: maxPerDate }),
    });
    if (!res.ok) throw new Error(`analyze -> ${res.status}`);
    return res.json();
  },
  analyzeStatus: (runId: string) => get<AnalyzeStatus>(`/api/analyze/${runId}`),
};
