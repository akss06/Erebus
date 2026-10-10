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
  const [boot, setBoot] = useState<"loading" | "ready" | "error">("loading");
  const [slow, setSlow] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setSlow(false);
    // free-tier backend can be asleep: after a few seconds, tell the user we're waking it
    const slowTimer = setTimeout(() => { if (!cancelled) setSlow(true); }, 4000);
    api.areas()
      .then((a) => { if (!cancelled) { setAreas(a); setBoot("ready"); } })
      .catch(() => { if (!cancelled) setBoot("error"); })
      .finally(() => clearTimeout(slowTimer));
    api.simVessels().then((v) => { if (!cancelled) setSimVessels(v); }).catch(() => {});
    api.health().then((h) => { if (!cancelled) setLiveAnalysis(h.live_analysis); }).catch(() => { if (!cancelled) setLiveAnalysis(false); });
    return () => { cancelled = true; clearTimeout(slowTimer); };
  }, [reloadKey]);

  const retryBoot = () => { setBoot("loading"); setReloadKey((k) => k + 1); };
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
      {boot !== "ready" && (
        <div className="boot-overlay">
          <div className="boot-card">
            {boot === "error" ? (
              <>
                <b>Can't reach the backend</b>
                <p>The API didn't respond. On the free tier it may still be starting up — give it a moment and retry.</p>
                <button onClick={retryBoot}>Retry</button>
              </>
            ) : (
              <>
                <div className="spinner" />
                <b>{slow ? "Waking the backend…" : "Loading…"}</b>
                {slow && <p>The free-tier server was asleep; the first load can take 30–60s.</p>}
              </>
            )}
          </div>
        </div>
      )}
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
          {/* Only where the backend can actually run it (local build with Earth Engine credentials). */}
          {liveAnalysis && (
            <button className={showAnalyze ? "active" : "analyze-btn"} onClick={() => setShowAnalyze(true)}>
              Run Analysis
            </button>
          )}
          <a className="gh-link" href="https://github.com/akss06/Erebus" target="_blank" rel="noopener noreferrer" title="Source code on GitHub">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="currentColor">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
            </svg>
            Source code
          </a>
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
