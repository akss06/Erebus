import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { API_BASE, type Area, type ConfClass } from "./api";
import { CLASSES, CLASS_ORDER } from "./classes";

export interface MapPoint {
  id: string; // detection id to open on click
  lon: number;
  lat: number;
  confidence_class: ConfClass;
  confidence: number;
  passes_seen: number;
  simulated?: boolean;
}

interface Props {
  area: Area;
  points: MapPoint[];
  visible: Set<ConfClass>;
  selectedId: string | null;
  showOverlay: boolean;
  onSelect: (id: string) => void;
}

// MapLibre's types cannot express a spread-built "match", hence the cast.
const colorExpr = ["match", ["get", "confidence_class"], ...CLASS_ORDER.flatMap((c) => [c, CLASSES[c].color]), "#999"] as unknown as maplibregl.ExpressionSpecification;

function toGeoJSON(points: MapPoint[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: points.map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.lon, p.lat] },
      properties: { id: p.id, confidence_class: p.confidence_class, confidence: p.confidence, passes_seen: p.passes_seen, simulated: !!p.simulated },
    })),
  };
}

export default function MapView({ area, points, visible, selectedId, showOverlay, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const ready = useRef(false);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    const map = new maplibregl.Map({
      container: container.current!,
      style: {
        version: 8,
        sources: {
          osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OpenStreetMap" },
        },
        layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-brightness-max": 0.55, "raster-saturation": -0.6 } }],
      },
      center: area.center,
      zoom: area.zoom,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.on("load", () => {
      map.addSource("points", { type: "geojson", data: toGeoJSON([]) });
      map.addLayer({
        id: "points",
        type: "circle",
        source: "points",
        paint: {
          "circle-color": colorExpr,
          "circle-radius": ["+", 5, ["*", 4, ["min", 4, ["get", "passes_seen"]]]],
          "circle-opacity": 0.85,
          "circle-stroke-width": ["case", ["get", "simulated"], 3.5, 1.5],
          "circle-stroke-color": ["case", ["get", "simulated"], "#ffd666", "#0b1220"],
        },
      });
      map.addSource("selected", { type: "geojson", data: toGeoJSON([]) });
      map.addLayer({
        id: "selected",
        type: "circle",
        source: "selected",
        paint: { "circle-radius": 18, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 2.5, "circle-stroke-color": "#ffffff" },
      });
      map.on("click", "points", (e) => {
        const id = e.features?.[0]?.properties?.id;
        if (id) onSelectRef.current(String(id));
      });
      map.on("mouseenter", "points", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "points", () => (map.getCanvas().style.cursor = ""));
      ready.current = true;
      map.fire("erebus-ready");
    });
    mapRef.current = map;
    // The container is sized by flex layout after first paint; keep the canvas in step with it.
    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container.current!);
    return () => {
      observer.disconnect();
      map.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Run `fn` once the style has loaded (immediately if it already has).
  const whenReady = (fn: (m: maplibregl.Map) => void) => {
    const map = mapRef.current;
    if (!map) return;
    if (ready.current) fn(map);
    else map.once("erebus-ready", () => fn(map));
  };

  useEffect(() => {
    whenReady((map) => map.flyTo({ center: area.center, zoom: area.zoom, duration: 800 }));
  }, [area.id]);

  useEffect(() => {
    whenReady((map) => {
      (map.getSource("points") as maplibregl.GeoJSONSource).setData(toGeoJSON(points.filter((p) => visible.has(p.confidence_class))));
    });
  }, [points, visible]);

  useEffect(() => {
    whenReady((map) => {
      const sel = points.filter((p) => p.id === selectedId);
      (map.getSource("selected") as maplibregl.GeoJSONSource).setData(toGeoJSON(sel));
      if (sel[0]) map.easeTo({ center: [sel[0].lon, sel[0].lat], duration: 500 });
    });
  }, [selectedId, points]);

  useEffect(() => {
    whenReady((map) => {
      if (map.getLayer("sar")) map.removeLayer("sar");
      if (map.getSource("sar")) map.removeSource("sar");
      if (!showOverlay || !area.overlay) return;
      const [[s, w], [n, e]] = area.overlay.bounds;
      map.addSource("sar", { type: "image", url: API_BASE + area.overlay.image, coordinates: [[w, n], [e, n], [e, s], [w, s]] });
      map.addLayer({ id: "sar", type: "raster", source: "sar", paint: { "raster-opacity": 0.8 } }, "points");
    });
  }, [area.id, showOverlay]);

  return <div ref={container} className="map" />;
}
