import { useEffect, useState } from "react";
import { api, type DetectionDetail, type Review } from "./api";
import { CLASSES } from "./classes";

interface Props {
  id: string;
  sim: string | null;
  onClose: () => void;
  onReviewed: () => void;
}

const VERDICTS: { value: Review["verdict"]; label: string }[] = [
  { value: "confirm", label: "Confirm" },
  { value: "reject", label: "Reject" },
  { value: "unsure", label: "Unsure" },
];

export default function DetailPanel({ id, sim, onClose, onReviewed }: Props) {
  const [d, setD] = useState<DetectionDetail | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setD(null);
    api.detection(id, sim).then((x) => {
      setD(x);
      setNote(x.review?.note ?? "");
    });
  }, [id, sim]);

  if (!d) return <aside className="panel"><p className="muted">Loading…</p></aside>;
  const info = CLASSES[d.confidence_class];

  const submit = async (verdict: Review["verdict"]) => {
    setSaving(true);
    const review = await api.review({ detection_id: d.id, verdict, note });
    setD({ ...d, review });
    setSaving(false);
    onReviewed();
  };

  return (
    <aside className="panel">
      <button className="link" onClick={onClose}>← Back to review queue</button>
      <h2>
        <span className="dot" style={{ background: info.color }} /> {info.label}
      </h2>
      <p className="muted">{info.blurb}</p>

      <div className="stat-row">
        <div><b>{d.confidence}</b><span>confidence /100</span></div>
        <div><b>{d.contrast_db.toFixed(1)} dB</b><span>contrast</span></div>
        <div><b>{d.area_px}</b><span>pixels</span></div>
      </div>

      {d.crop_url ? (
        <figure>
          <img src={d.crop_url} alt="Radar crop around the detection" />
          <figcaption>Sentinel-1 radar crop, detection at the centre ({d.date})</figcaption>
        </figure>
      ) : (
        <p className="muted small">No evidence crop saved for this detection yet.</p>
      )}

      <h3>Why this class</h3>
      <ul>{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>

      <h3>AIS check</h3>
      {d.match_status === "MATCHED" ? (
        <p>
          Nearest AIS vessel: <b>{d.matched_name || "unknown"}</b>
          {d.matched_flag ? ` (${d.matched_flag})` : ""}, {d.match_distance_m?.toFixed(0)} m away.
        </p>
      ) : (
        <p>No AIS vessel within the match radius.</p>
      )}
      <p className="muted small">
        AIS comes from Global Fishing Watch: daily and ~1 km resolution, so it cannot prove a ship was broadcasting at the
        exact moment of the pass.
      </p>

      {d.other_passes.length > 0 && (
        <>
          <h3>Same location on other passes</h3>
          <table>
            <tbody>
              {d.other_passes.map((p) => (
                <tr key={p.id}>
                  <td>{p.date}</td>
                  <td>{p.contrast_db.toFixed(1)} dB</td>
                  <td>{p.matched_name || "no AIS"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h3>Analyst review</h3>
      <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note" maxLength={500} />
      <div className="verdicts">
        {VERDICTS.map((v) => (
          <button key={v.value} disabled={saving} className={d.review?.verdict === v.value ? "active" : ""} onClick={() => submit(v.value)}>
            {v.label}
          </button>
        ))}
      </div>

      <p className={d.simulated ? "provenance sim" : "provenance"}>Provenance: {d.provenance}</p>
    </aside>
  );
}
