#!/usr/bin/env python3
"""Recupere les troncons OSM (highway=*) sur l'emprise (bbox) d'un GeoTIFF
donne. Utilise l'emprise du MNT plutot qu'un nom de lieu geocode via
Nominatim : "Savoie, France" resout par exemple vers la Haute-Savoie
("Upper Savoy") - l'emprise du fichier telecharge est la seule source
fiable pour garantir que OSM et MNT couvrent la meme zone.

Usage: fetch_osm_bbox.py <dem.tif> <out.gpkg>"""
import sys
import time

import geopandas as gpd
import osmnx as ox
import rasterio
from pyproj import Transformer
from shapely.geometry import box

DEM_PATH = sys.argv[1]
OUT_PATH = sys.argv[2]
KEEP_COLS = ["osmid", "name", "highway", "bicycle", "bridge", "tunnel", "oneway", "access", "service", "geometry"]

ox.settings.timeout = 900
ox.settings.use_cache = False  # chaque bbox n'est interrogee qu'une fois ; le
# cache disque d'osmnx grossissait sans limite (6+ Go apres ~25 departements)
# pour rien puisqu'on ne repasse jamais sur la meme requete
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
