import type { Cluster } from "./api";
import { CLASSES } from "./classes";

interface Props {
  alerts: Cluster[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function dateSummary(dates: string[]): string {
  if (!dates.length) return "";
  const months = [...new Set(dates.map((d) => MONTHS[parseInt(d.split("-")[1]) - 1]))];
  return months.join(", ");
}

export default function AlertsPanel({ alerts, selectedId, onSelect }: Props) {
  const dark = alerts.filter((a) => a.confidence_class === "DARK_CANDIDATE").length;
  return (
    <aside className="panel">
      <h2>Review queue</h2>
      <p className="muted small">
        {alerts.length} location{alerts.length !== 1 ? "s" : ""}, most suspicious first.
        {dark > 0 && <> · <b style={{ color: "#ff4d4f" }}>{dark} dark candidate{dark !== 1 ? "s" : ""}</b></>}
      </p>
      {dark === 0 && (
        <p className="notice">
          No dark-vessel candidates. Every radar return has a nearby AIS vessel.
        </p>
      )}
      <ul className="alerts">
        {alerts.map((a) => {
          const info = CLASSES[a.confidence_class];
          return (
            <li key={a.cluster_id} className={a.best_detection_id === selectedId ? "selected" : ""} onClick={() => onSelect(a.best_detection_id)}>
              <span className="dot" style={{ background: info.color }} />
              <div>
                <b>{info.label}</b> <span className="conf">{a.confidence}</span>
                <div className="small muted">
                  {a.matched_names[0] ?? "no AIS match"} · {a.passes_seen} pass{a.passes_seen > 1 ? "es" : ""}{a.dates.length > 0 && ` · ${dateSummary(a.dates)}`}
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
