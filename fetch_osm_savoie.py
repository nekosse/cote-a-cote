#!/usr/bin/env python3
"""Recupere les troncons OSM (highway=*) pour le departement Savoie (73).

Important : on n'utilise PAS ox.geocode_to_gdf("Savoie, France") - Nominatim
resout cette requete vers "Upper Savoy" (Haute-Savoie, departement 74) et
pas la Savoie (73). On interroge donc directement l'emprise du MNT RGE ALTI
(deja telechargee et confirmee correcte pour le departement 73), convertie
en WGS84, ce qui garantit que la couverture OSM correspond a la couverture
altimetrique."""
import time

import geopandas as gpd
import osmnx as ox
import rasterio
from pyproj import Transformer
from shapely.geometry import box

DEM_PATH = "/Users/leo/dev/tours-slope-map/alt_savoie.tif"
OUT_PATH = "/Users/leo/dev/tours-slope-map/geom_savoie_osm.gpkg"
KEEP_COLS = ["osmid", "name", "highway", "bicycle", "bridge", "tunnel", "oneway", "access", "service", "geometry"]

ox.settings.timeout = 900
ox.settings.log_console = True
ox.settings.max_query_area_size = 50 * 1000 * 1000 * 1000

ds = rasterio.open(DEM_PATH)
t = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
minlon, minlat = t.transform(ds.bounds.left, ds.bounds.bottom)
maxlon, maxlat = t.transform(ds.bounds.right, ds.bounds.top)
print(f"Emprise MNT (WGS84): lon {minlon:.4f}-{maxlon:.4f} lat {minlat:.4f}-{maxlat:.4f}", flush=True)
polygon = box(minlon, minlat, maxlon, maxlat)

t0 = time.time()
print("Requete Overpass (highway=*) sur l'emprise MNT...", flush=True)
gdf = ox.features_from_polygon(polygon, tags={"highway": True})
print(f"{len(gdf)} objets recuperes en {time.time() - t0:.0f}s", flush=True)

gdf = gdf[gdf.geom_type.isin(["LineString", "MultiLineString"])].copy()
print(f"{len(gdf)} lignes apres filtrage du type de geometrie", flush=True)

gdf = gdf.reset_index()
if "osmid" not in gdf.columns and "id" in gdf.columns:
    gdf = gdf.rename(columns={"id": "osmid"})

for col in KEEP_COLS:
    if col not in gdf.columns:
        gdf[col] = None

gdf = gdf[KEEP_COLS]
gdf["osmid"] = gdf["osmid"].astype(str)

print(f"Ecriture {OUT_PATH}...", flush=True)
gdf.to_file(OUT_PATH, layer="ways", driver="GPKG")
print("Termine.", flush=True)
