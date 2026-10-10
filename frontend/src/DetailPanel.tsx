import { useEffect, useState } from "react";
import { api, API_BASE, type DetectionDetail, type Eos04Check, type Review } from "./api";
import { CLASSES } from "./classes";

interface Props {
  id: string;
  sim: string | null;
  onClose: () => void;
  onReviewed: () => void;
}

const VERDICTS: { value: Review["verdict"]; label: string }[] = [
  { value: "confirm", label: "Looks like a ship" },
  { value: "reject", label: "Not a ship" },
  { value: "unsure", label: "Can't tell" },
];

// Classes where a missing AIS signal is the point -- show the "lead, not proof" caution.
const NEEDS_CAUTION = new Set(["DARK_CANDIDATE", "UNVERIFIED_TARGET", "PERSISTENT_UNIDENTIFIED"]);

function aisSentence(d: DetectionDetail): string {
  const who = `${d.matched_name || "an unnamed ship"}${d.matched_flag ? ` (${d.matched_flag})` : ""}`;
  switch (d.ais_evidence) {
    case "matched":
      return `Yes: ${who} was broadcasting AIS ${d.match_distance_m?.toFixed(0)} m away.`;
    case "gfw_reported_ais":
      return `Yes: Global Fishing Watch links this spot to ${who}, a ship broadcasting AIS.`;
    case "gfw_reported_no_ais":
      return "No: Global Fishing Watch saw something here too, but no AIS-broadcasting ship was linked to it.";
    case "unmatched":
      return "No: no AIS-broadcasting ship was found nearby.";
    default:
      return "Unknown: AIS was not checked at this spot.";
  }
}

function eos04Sentence(e: Eos04Check): string {
  return e.seen
    ? `Yes: India's EOS-04 radar satellite (ISRO) passed about 4 minutes later and also saw an object here, ${e.distance_m} m from where Sentinel-1 saw it. That shows something physical was there, not what it is.`
    : "No: India's EOS-04 radar satellite (ISRO) passed about 4 minutes later and saw nothing within 100 m. The object may have moved, been too small for EOS-04's coarser image, or been a false alarm.";
}

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
  const brighter = Math.round(10 ** (d.contrast_db / 10));
  const across = Math.round(Math.sqrt(d.area_px) * 10);   // 10 m pixels
  const passes = d.other_passes.length + 1;

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
      {d.simulated && <p className="provenance sim">{d.provenance}</p>}

      <h2>
        <span className="dot" style={{ background: info.color }} /> {info.label}
      </h2>
      <p>{info.blurb}</p>

      {d.crop_url && (
        <figure>
          <img src={API_BASE + d.crop_url} alt="Satellite radar image around the detection" />
          <figcaption>Satellite radar image, {d.date}. The detected object is circled in red.</figcaption>
        </figure>
      )}

      <dl className="facts">
        <dt>What the satellite saw</dt>
        <dd>A bright object roughly {across} m across, about {brighter}× brighter than the sea around it.</dd>
        <dt>Is a ship broadcasting its position (AIS) here?</dt>
        <dd>{aisSentence(d)}</dd>
        <dt>Seen here before?</dt>
        <dd>
          {passes > 1
            ? `Yes, at this spot on ${passes} satellite passes (${[d.date, ...d.other_passes.map((p) => p.date)].sort().join(", ")}).`
            : "No, only on this one satellite pass."}
        </dd>
        {d.eos04 && (
          <>
            <dt>Seen by a second satellite?</dt>
            <dd>{eos04Sentence(d.eos04)}</dd>
          </>
        )}
      </dl>

      {d.eos04?.crop_url && (
        <figure>
          <img src={API_BASE + d.eos04.crop_url} alt="ISRO EOS-04 radar image of the same spot" />
          <figcaption>
            ISRO EOS-04 radar image of the same spot, {d.eos04.eos04_time_utc} UTC (Sentinel-1: {d.eos04.s1_time_utc} UTC).
            The red circle marks where Sentinel-1 saw the object.
          </figcaption>
        </figure>
      )}

      {NEEDS_CAUTION.has(d.confidence_class) && (
        <p className="caution">
          A missing AIS signal is a reason to check, not proof of wrongdoing. Many small fishing boats don't carry AIS at all.
        </p>
      )}

      <h3>Your review</h3>
      <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note" maxLength={500} />
      <div className="verdicts">
        {VERDICTS.map((v) => (
          <button key={v.value} disabled={saving} className={d.review?.verdict === v.value ? "active" : ""} onClick={() => submit(v.value)}>
            {v.label}
          </button>
        ))}
      </div>

      <details className="tech">
        <summary>Technical details</summary>
        <p className="small">
          Ranking score <b>{d.confidence}/100</b> (orders the review queue; not a probability) · contrast{" "}
          <b>{d.contrast_db.toFixed(1)} dB</b> · <b>{d.area_px}</b> pixels (10 m each)
        </p>
        <ul className="small">{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        {d.other_passes.length > 0 && (
          <table className="small">
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
        )}
        <p className="small muted">
          Global Fishing Watch detects ships from the same Sentinel-1 images, so its agreement is a second algorithm, not an
          independent sensor. Its AIS data is daily and ~1 km coarse.
          {d.gfw_association === "ambiguous" && " Here one GFW record covers several detections, so the identity is not certain."}
        </p>
        {!d.simulated && <p className="small muted">Source: {d.provenance} · ID {d.id}</p>}
      </details>
    </aside>
  );
}
