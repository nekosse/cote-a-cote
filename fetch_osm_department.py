#!/usr/bin/env python3
"""Recupere les troncons OSM (highway=*) pour tout le departement Indre-et-
Loire (37) et les sauvegarde dans un GeoPackage, avec le meme schema de
colonnes que geom_tours_osm_extended.gpkg (pour reutiliser slope_map.py sans
modification)."""
import sys
import time

import geopandas as gpd
import osmnx as ox

OUT_PATH = "/Users/leo/dev/tours-slope-map/geom_indre_et_loire_osm.gpkg"
KEEP_COLS = ["osmid", "name", "highway", "bicycle", "bridge", "tunnel", "oneway", "access", "service", "geometry"]

ox.settings.timeout = 900
ox.settings.log_console = True
ox.settings.max_query_area_size = 50 * 1000 * 1000 * 1000  # 50 000 km2, largement au-dessus du departement

print("Geocodage de la limite administrative...", flush=True)
boundary = ox.geocode_to_gdf("Indre-et-Loire, France")
polygon = boundary.geometry.iloc[0]
print(f"Limite obtenue, bounds={polygon.bounds}", flush=True)

t0 = time.time()
print("Requete Overpass (highway=*) sur tout le departement...", flush=True)
gdf = ox.features_from_polygon(polygon, tags={"highway": True})
print(f"{len(gdf)} objets recuperes en {time.time() - t0:.0f}s", flush=True)

# features_from_polygon renvoie points/lignes/polygones melanges (ex: highway
# sur un noeud isole, ou une place mappee en polygone) : seules les lignes
# nous interessent, comme dans le pipeline existant.
gdf = gdf[gdf.geom_type.isin(["LineString", "MultiLineString"])].copy()
print(f"{len(gdf)} lignes apres filtrage du type de geometrie", flush=True)

gdf = gdf.reset_index()  # osmid/element_type deviennent des colonnes
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
