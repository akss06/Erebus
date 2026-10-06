import type { Cluster } from "./api";
import { CLASSES } from "./classes";

interface Props {
  alerts: Cluster[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export default function AlertsPanel({ alerts, selectedId, onSelect }: Props) {
  const dark = alerts.filter((a) => a.confidence_class === "DARK_CANDIDATE").length;
  return (
    <aside className="panel">
      <h2>Review queue</h2>
      <p className="muted">
        {alerts.length} locations to review, most suspicious first. Fixed objects and sea clutter are excluded.
      </p>
      {dark === 0 && (
        <p className="notice">
          No dark-vessel candidates in this area. Every bright ship-like return has an AIS vessel nearby.
        </p>
      )}
      <ul className="alerts">
        {alerts.map((a) => {
          const info = CLASSES[a.confidence_class];
          return (
            <li key={a.cluster_id} className={a.best_detection_id === selectedId ? "selected" : ""} onClick={() => onSelect(a.best_detection_id)}>
              <span className="dot" style={{ background: info.color }} />
              <div>
                <b>{info.label}</b> <span className="muted">· {a.confidence}/100</span>
                <div className="small muted">
                  {a.matched_names[0] ?? "no AIS match"} · seen on {a.passes_seen} pass{a.passes_seen > 1 ? "es" : ""}
                </div>
              </div>
              {a.review && <span className={`badge ${a.review.verdict}`}>{a.review.verdict}</span>}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
