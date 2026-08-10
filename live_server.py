#!/usr/bin/env python3
"""
Prototype d'architecture "live" pour Cote a cote : au lieu de pre-calculer
une carte statique pour toute une zone, un backend recoit la bbox visible du
navigateur, ne lit que les donnees (MNT + troncons OSM) qui la couvrent, et
relance l'algorithme existant (build_segments/build_climbs de slope_map.py)
a la volee.

But du prototype : verifier que l'algorithme "tient" en interactif (temps de
reponse) et que le decoupage par bbox ne casse pas le regroupement des cotes
(d'ou la marge appliquee autour de la bbox demandee), avant d'envisager de
scaler la source de donnees a tout un departement/la France.

Lance avec :
    uvicorn live_server:app --reload --port 8000
"""
import json
import time

import geopandas as gpd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pyproj import Transformer

from slope_map import (
    CORE_ROAD_TYPES,
    DEFAULT_THRESHOLDS,
    build_climbs,
    build_segments,
    climbs_to_geojson,
    is_bike_relevant,
    load_dem,
    make_sampler,
)

GEOM_PATH = "/Users/leo/dev/tours-slope-map/geom_tours_osm_extended.gpkg"
DEM_PATH = "/Users/leo/dev/tours-slope-map/alt_tours_extended.tif"

# marge (m) ajoutee autour de la bbox demandee : une cote a cheval sur le
# bord de l'ecran a besoin de voir un peu au-dela pour que le regroupement
# par nom / la deduplication des chaussees paralleles restent corrects.
QUERY_MARGIN_M = 400.0
CLIMB_MIN_ELEV_GAIN = 15.0

print(f"Chargement MNT ({DEM_PATH})...")
_ds, _band, _nodata = load_dem(DEM_PATH, smooth_radius=10.0)
_sample = make_sampler(_ds, _band)
_dem_bounds_2154 = _ds.bounds
_to_2154 = Transformer.from_crs("EPSG:4326", _ds.crs, always_xy=True)
print(f"MNT pret, crs={_ds.crs}, bounds={_dem_bounds_2154}")

app = FastAPI(title="Cote a cote - live")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _bbox_to_2154(minx, miny, maxx, maxy):
    corners = [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]
    xs, ys = zip(*[_to_2154.transform(lon, lat) for lon, lat in corners])
    return min(xs), min(ys), max(xs), max(ys)


@app.get("/api/climbs")
def get_climbs(
    minx: float = Query(...), miny: float = Query(...),
    maxx: float = Query(...), maxy: float = Query(...),
):
    t0 = time.time()
    bx0, by0, bx1, by1 = _bbox_to_2154(minx, miny, maxx, maxy)
    bx0 -= QUERY_MARGIN_M
    by0 -= QUERY_MARGIN_M
    bx1 += QUERY_MARGIN_M
    by1 += QUERY_MARGIN_M

    # bbox hors de l'emprise du MNT disponible : rien a calculer ici (demo
    # limitee a la zone de Tours pour l'instant, cf discussion sur le
    # passage a l'echelle).
    db = _dem_bounds_2154
    if bx1 < db.left or bx0 > db.right or by1 < db.bottom or by0 > db.top:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": {"out_of_coverage": True}})

    t_read0 = time.time()
    gdf = gpd.read_file(GEOM_PATH, layer="ways", bbox=(bx0, by0, bx1, by1))
    t_read = time.time() - t_read0

    if gdf.empty:
        return JSONResponse({"type": "FeatureCollection", "features": [], "meta": {"n_ways": 0}})

    gdf = gdf[gdf["highway"].notna() & (gdf["highway"] != "")]
    gdf = gdf[gdf.apply(lambda r: is_bike_relevant(r.get("highway"), r.get("bicycle")), axis=1)]

    t_seg0 = time.time()
    segments = build_segments(gdf, _sample, chunk_length=80.0, sample_step=15.0,
                               thresholds=DEFAULT_THRESHOLDS, min_length=20.0)
    t_seg = time.time() - t_seg0

    t_climb0 = time.time()
    climbs = build_climbs(segments, DEFAULT_THRESHOLDS[0], _sample, snap_tolerance=2.0)
    climbs = [c for c in climbs if c["elev_gain_m"] > CLIMB_MIN_ELEV_GAIN]
    t_climb = time.time() - t_climb0

    geojson = json.loads(climbs_to_geojson(climbs, segments.crs, DEFAULT_THRESHOLDS)) if climbs else {
        "type": "FeatureCollection", "features": []
    }
    geojson["meta"] = {
        "n_ways": len(gdf), "n_segments": len(segments), "n_climbs": len(climbs),
        "t_total_ms": round((time.time() - t0) * 1000),
        "t_read_ms": round(t_read * 1000),
        "t_segments_ms": round(t_seg * 1000),
        "t_climbs_ms": round(t_climb * 1000),
    }
    return JSONResponse(geojson)


PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Cote a cote - live</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  #status {
    position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
    background: rgba(255,255,255,0.95); padding: 6px 14px; border-radius: 8px;
    font: 13px -apple-system, sans-serif; z-index: 9999; box-shadow: 0 2px 8px rgba(0,0,0,.2);
  }
</style>
</head>
<body>
<div id="status">deplacez la carte...</div>
<div id="map"></div>
<script>
  const map = L.map("map").setView([47.394, 0.686], 14);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    subdomains: "abcd", maxZoom: 20,
  }).addTo(map);

  let layer = null;
  const statusEl = document.getElementById("status");
  let debounceTimer = null;

  function styleFn(feature) {
    return { color: feature.properties.color, weight: 3, opacity: 0.85 };
  }

  function refresh() {
    const b = map.getBounds();
    const url = `/api/climbs?minx=${b.getWest()}&miny=${b.getSouth()}&maxx=${b.getEast()}&maxy=${b.getNorth()}`;
    statusEl.textContent = "calcul en cours...";
    const t0 = performance.now();
    fetch(url).then(r => r.json()).then(data => {
      const clientMs = Math.round(performance.now() - t0);
      if (layer) map.removeLayer(layer);
      layer = L.geoJSON(data, { style: styleFn }).addTo(map);
      const m = data.meta || {};
      if (m.out_of_coverage) {
        statusEl.textContent = "hors zone de donnees disponible (demo limitee a Tours)";
      } else {
        statusEl.textContent =
          `${m.n_climbs ?? 0} cotes . ${m.n_ways ?? 0} troncons . serveur ${m.t_total_ms ?? "?"}ms (lecture ${m.t_read_ms}ms + calcul ${m.t_segments_ms + m.t_climbs_ms}ms) . aller-retour ${clientMs}ms`;
      }
    }).catch(err => {
      statusEl.textContent = "erreur: " + err;
    });
  }

  map.on("moveend", function() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refresh, 300);
  });

  refresh();
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
