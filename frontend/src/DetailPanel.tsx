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
      {d.fixed_evidence && (
        <p className="muted small">
          {d.fixed_evidence === "charted" ? <>Fixed-object basis: <b>charted fixed-infrastructure feature (confirmed)</b></>
            : d.fixed_evidence === "chain_geometry" ? <><b>Suspected</b> fixed/reef basis: collinear persistent chain (reef/shoal/causeway geometry) &mdash; not confirmed, still reviewable</>
            : <><b>Suspected</b> fixed basis: position-stable across passes (heuristic, not charted) &mdash; not confirmed, still reviewable</>}
        </p>
      )}

      <div className="stat-row">
        <div title={d.score_basis ?? "heuristic evidence/ranking score, not a calibrated probability"}>
          <b>{d.confidence}</b><span>evidence score /100</span>
        </div>
        <div><b>{d.contrast_db.toFixed(1)} dB</b><span>contrast</span></div>
        <div><b>{d.area_px}</b><span>pixels</span></div>
      </div>
      <p className="muted small">
        The score is a heuristic ranking of evidence (radar strength, size, persistence, AIS), not a probability that this is a vessel.
      </p>

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

      <h3>AIS &amp; GFW evidence</h3>
      {d.ais_evidence === "matched" ? (
        <p>
          AIS vessel matched by us: <b>{d.matched_name || "unknown"}</b>
          {d.matched_flag ? ` (${d.matched_flag})` : ""}, {d.match_distance_m?.toFixed(0)} m away.
        </p>
      ) : d.ais_evidence === "gfw_reported_ais" ? (
        <p>
          GFW's SAR record here carries an AIS identity: <b>{d.matched_name || "unknown"}</b>
          {d.matched_flag ? ` (${d.matched_flag})` : ""}
          {d.match_distance_m != null ? `, ${d.match_distance_m.toFixed(0)} m away` : ""}. This is GFW-reported, not an
          independent AIS match by us.
        </p>
      ) : d.ais_evidence === "gfw_reported_no_ais" ? (
        <p>GFW's SAR record here has no AIS identity (GFW-reported absence, not a verified no-AIS observation).</p>
      ) : d.ais_evidence === "unmatched" ? (
        <p>No AIS vessel within our match radius.</p>
      ) : (
        <p>AIS status unknown (no GFW record within radius and no AIS checked by us).</p>
      )}
      {d.gfw_association && (
        <p className="muted small">
          GFW SAR association: <b>{d.gfw_association}</b>
          {d.gfw_association === "ambiguous" ? " — one coarse GFW record covers several detections, so the identity is not confident." : ""}
        </p>
      )}
      <p className="muted small">
        GFW's SAR-presence product is derived from the same Sentinel-1 imagery (shared-sensor algorithmic agreement, not an
        independent sensor). Its AIS is daily and ~1 km, so it cannot prove a ship was broadcasting at the moment of the pass.
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
