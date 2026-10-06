import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AnalyzeStatus } from "./api";

interface Props {
  onComplete: (areaId: string) => void;
  onClose: () => void;
}

const PRESETS: Record<string, { label: string; region: [number, number, number, number] }> = {
  gulf_mannar: { label: "Gulf of Mannar", region: [78.0, 8.2, 80.0, 10.3] },
  palk_strait: { label: "Palk Strait", region: [79.0, 9.5, 80.5, 10.5] },
  lakshadweep: { label: "Lakshadweep Sea", region: [71.0, 8.0, 74.0, 12.0] },
};

export default function AnalyzePanel({ onComplete, onClose }: Props) {
  const [preset, setPreset] = useState("gulf_mannar");
  const [region, setRegion] = useState(PRESETS.gulf_mannar.region);
  const [startDate, setStartDate] = useState("2026-09-10");
  const [endDate, setEndDate] = useState("2026-10-04");
  const [maxPerDate, setMaxPerDate] = useState(10);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<AnalyzeStatus | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<number | null>(null);

  const handlePreset = (key: string) => {
    setPreset(key);
    if (PRESETS[key]) setRegion(PRESETS[key].region);
  };

  const startAnalysis = useCallback(async () => {
    setRunning(true);
    setLogs([]);
    setStatus(null);
    try {
      const { run_id } = await api.analyze(region, startDate, endDate, maxPerDate);

      const es = new EventSource(`/api/analyze/${run_id}/stream`);
      es.onmessage = (e) => {
        const data: AnalyzeStatus = JSON.parse(e.data);
        setStatus(data);
        if (data.log.length) setLogs((prev) => [...prev, ...data.log]);
        if (data.status === "done" || data.status === "error") {
          es.close();
          if (data.status === "done" && data.area_id) {
            onComplete(data.area_id);
          }
        }
      };
      es.onerror = () => {
        es.close();
        pollRef.current = window.setInterval(async () => {
          try {
            const s = await api.analyzeStatus(run_id);
            setStatus(s);
            if (s.log.length) setLogs((prev) => [...prev, ...s.log]);
            if (s.status === "done" || s.status === "error") {
              clearInterval(pollRef.current!);
              if (s.status === "done" && s.area_id) onComplete(s.area_id);
            }
          } catch { /* retry */ }
        }, 2000);
      };
    } catch (err) {
      setStatus({ status: "error", progress: 0, log: [], result: null, area_id: null, error: String(err) });
    }
  }, [region, startDate, endDate, maxPerDate, onComplete]);

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  const done = status?.status === "done";
  const error = status?.status === "error";

  return (
    <aside className="panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2>Run Analysis</h2>
        <button className="link" onClick={onClose} style={{ paddingBottom: 0 }}>Close</button>
      </div>

      {!running ? (
        <>
          <p className="muted small">Run the detection pipeline on a new region and date range. Requires Earth Engine and GFW credentials.</p>

          <h3>Region preset</h3>
          <div className="chips" style={{ marginBottom: 8 }}>
            {Object.entries(PRESETS).map(([k, v]) => (
              <button key={k} className={preset === k ? "active" : ""} onClick={() => handlePreset(k)}>
                {v.label}
              </button>
            ))}
          </div>

          <h3>Region bounds</h3>
          <div className="coord-grid">
            {(["West", "South", "East", "North"] as const).map((lbl, i) => (
              <label key={lbl} className="small">
                {lbl}
                <input
                  type="number" step="0.1"
                  value={region[i]}
                  onChange={(e) => { const r = [...region] as [number, number, number, number]; r[i] = parseFloat(e.target.value) || 0; setRegion(r); setPreset(""); }}
                />
              </label>
            ))}
          </div>

          <h3>Date range</h3>
          <div style={{ display: "flex", gap: 8 }}>
            <label className="small" style={{ flex: 1 }}>
              Start
              <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} style={{ width: "100%" }} />
            </label>
            <label className="small" style={{ flex: 1 }}>
              End
              <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} style={{ width: "100%" }} />
            </label>
          </div>

          <h3>Max tiles per date</h3>
          <input type="number" min={1} max={30} value={maxPerDate} onChange={(e) => setMaxPerDate(parseInt(e.target.value) || 10)} />

          <button className="active" style={{ width: "100%", marginTop: 16, padding: "10px 16px", fontSize: 15 }} onClick={startAnalysis}>
            Run Analysis
          </button>
        </>
      ) : (
        <>
          <div className="progress-bar" style={{ margin: "12px 0" }}>
            <div className="progress-fill" style={{ width: `${status?.progress ?? 0}%` }} />
          </div>
          <p className="small muted">{status?.progress ?? 0}% — {status?.status ?? "starting"}</p>

          <div ref={logRef} className="log-box">
            {logs.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>

          {done && status?.result && (
            <div className="notice" style={{ marginTop: 12, background: "#0a2e1a", borderColor: "#1a6b3a" }}>
              <b>Analysis complete</b>
              <br />
              {status.result.total} detections, {status.result.dark_candidates} dark-vessel candidates.
              <br />
              Results loaded — select the new area tab to explore.
            </div>
          )}

          {error && (
            <div className="notice" style={{ marginTop: 12, background: "#2e0a0a", borderColor: "#6b1a1a" }}>
              <b>Analysis failed</b>
              <br />
              {status?.error}
            </div>
          )}

          {(done || error) && (
            <button style={{ width: "100%", marginTop: 12 }} onClick={() => { setRunning(false); setStatus(null); setLogs([]); }}>
              {done ? "Run another" : "Try again"}
            </button>
          )}
        </>
      )}
    </aside>
  );
}
