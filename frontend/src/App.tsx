import { useEffect, useMemo, useState } from "react";
import { api, type Area, type Cluster, type ConfClass, type Detection, type SimVessel } from "./api";
import { CLASSES, CLASS_ORDER } from "./classes";
import MapView, { type MapPoint } from "./MapView";
import AlertsPanel from "./AlertsPanel";
import DetailPanel from "./DetailPanel";
import AnalyzePanel from "./AnalyzePanel";

const DEFAULT_VISIBLE = new Set<ConfClass>(CLASS_ORDER.filter((c) => c !== "CLUTTER"));

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function shortDate(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${parseInt(d)} ${MONTHS[parseInt(m) - 1]}`;
}

export default function App() {
  const [areas, setAreas] = useState<Area[]>([]);
  const [areaId, setAreaId] = useState("tuticorin");
  const [date, setDate] = useState<string>("all");
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [dayDetections, setDayDetections] = useState<Detection[]>([]);
  const [alerts, setAlerts] = useState<Cluster[]>([]);
  const [visible, setVisible] = useState<Set<ConfClass>>(DEFAULT_VISIBLE);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [overlay, setOverlay] = useState(false);
  const [sim, setSim] = useState<string | null>(null);
  const [simVessels, setSimVessels] = useState<SimVessel[]>([]);
  const [showAnalyze, setShowAnalyze] = useState(false);
  const [liveAnalysis, setLiveAnalysis] = useState(false);

  useEffect(() => {
    api.areas().then(setAreas);
    api.simVessels().then(setSimVessels);
    api.health().then((h) => setLiveAnalysis(h.live_analysis)).catch(() => setLiveAnalysis(false));
  }, []);
  const area = areas.find((a) => a.id === areaId);

  const refreshAlerts = () => api.alerts(areaId, sim).then(setAlerts);
  useEffect(() => {
    setDate("all");
    setSelectedId(null);
    setSim(null);
  }, [areaId]);

  useEffect(() => {
    api.clusters(areaId, sim).then(setClusters);
    refreshAlerts();
  }, [areaId, sim]);

  useEffect(() => {
    if (date === "all") setDayDetections([]);
    else api.detections(areaId, date, sim).then(setDayDetections);
  }, [areaId, date, sim]);

  const points: MapPoint[] = useMemo(() => {
    if (date === "all") {
      return clusters.map((c) => ({
        id: c.best_detection_id, lon: c.lon, lat: c.lat,
        confidence_class: c.confidence_class, confidence: c.confidence, passes_seen: c.passes_seen, simulated: c.simulated,
      }));
    }
    return dayDetections.map((d) => ({
      id: d.id, lon: d.lon, lat: d.lat,
      confidence_class: d.confidence_class, confidence: d.confidence, passes_seen: 1, simulated: d.simulated,
    }));
  }, [clusters, dayDetections, date]);

  // Legend shows only classes actually present on the map now, so empty categories
  // (e.g. a class no dataset produces) don't appear with no dots.
  const presentClasses = useMemo(() => new Set(points.map((p) => p.confidence_class)), [points]);

  const simName = simVessels.find((v) => v.mmsi === sim)?.name ?? sim;

  const toggle = (c: ConfClass) => {
    const next = new Set(visible);
    if (next.has(c)) next.delete(c);
    else next.add(c);
    setVisible(next);
  };

  return (
    <div className="app">
      <header>
        <div className="brand">
          <h1>ERÉBUS</h1>
          <span>Dark-vessel detection · Sentinel-1 SAR + AIS</span>
        </div>
        <nav>
          {areas.map((a) => (
            <button key={a.id} className={a.id === areaId && !showAnalyze ? "active" : ""} onClick={() => { setAreaId(a.id); setShowAnalyze(false); }}>
              {a.name} <small>({a.detections})</small>
            </button>
          ))}
          {liveAnalysis ? (
            <button className={showAnalyze ? "active" : "analyze-btn"} onClick={() => setShowAnalyze(true)}>
              Run Analysis
            </button>
          ) : (
            <button
              className="analyze-btn"
              disabled
              style={{ opacity: 0.5, cursor: "not-allowed" }}
              title="Live analysis runs in the local build (needs Earth Engine credentials). This hosted demo serves precomputed results."
            >
              Run Analysis
            </button>
          )}
        </nav>
      </header>

      <div className="body">
        <section className="mapwrap">
          {area && (
            <MapView area={area} points={points} visible={visible} selectedId={selectedId} showOverlay={overlay} onSelect={setSelectedId} />
          )}

          <div className="toolbar">
            <div className="chips">
              <button className={date === "all" ? "active" : ""} onClick={() => setDate("all")}>All passes</button>
              {area?.passes.map((p) => (
                <button key={p} className={date === p ? "active" : ""} onClick={() => setDate(p)} title={p}>{shortDate(p)}</button>
              ))}
            </div>
            {date === "all" && area && area.passes.length > 1 ? (
              <span className="check stacking-hint" title="Detections from every pass are drawn on top of each other. A busy spot stacks many dates into what looks like one cluster. Pick a date to see a single pass.">
                ⓘ All {area.passes.length} passes stacked ({shortDate(area.passes[0])}–{shortDate(area.passes[area.passes.length - 1])}) · click a date to separate
              </span>
            ) : date !== "all" ? (
              <span className="check stacking-hint">Single pass · {shortDate(date)}</span>
            ) : null}
            {area?.id === "tuticorin" && (
              <label className="check">
                Simulate:&nbsp;
                <select value={sim ?? ""} onChange={(e) => setSim(e.target.value || null)}>
                  <option value="">none (real data)</option>
                  {simVessels.map((v) => (
                    <option key={v.mmsi} value={v.mmsi}>switch off AIS of {v.name}</option>
                  ))}
                </select>
              </label>
            )}
            {area?.overlay && (
              <label className="check">
                <input type="checkbox" checked={overlay} onChange={(e) => setOverlay(e.target.checked)} /> Radar overlay
              </label>
            )}
          </div>

          {sim && (
            <div className="simbanner">
              <span>
                <b>SIMULATION</b> · AIS record of {simName} removed to test detector. Radar detections are
                real; the AIS gap is not.
              </span>
              <button onClick={() => setSim(null)}>Exit</button>
            </div>
          )}

          <div className="legend">
            {CLASS_ORDER.filter((c) => presentClasses.has(c)).map((c) => (
              <button key={c} className={visible.has(c) ? "" : "off"} onClick={() => toggle(c)} title={CLASSES[c].blurb}>
                <span className="dot" style={{ background: CLASSES[c].color }} /> {CLASSES[c].label}
              </button>
            ))}
            <p className="small muted">Larger dot = more passes. Click for evidence.</p>
          </div>
        </section>

        {showAnalyze ? (
          <AnalyzePanel
            onComplete={(newAreaId) => {
              api.areas().then(setAreas);
              setAreaId(newAreaId);
              setShowAnalyze(false);
            }}
            onClose={() => setShowAnalyze(false)}
          />
        ) : selectedId ? (
          <DetailPanel id={selectedId} sim={sim} onClose={() => setSelectedId(null)} onReviewed={refreshAlerts} />
        ) : (
          <AlertsPanel alerts={alerts} selectedId={selectedId} onSelect={setSelectedId} />
        )}
      </div>
    </div>
  );
}
