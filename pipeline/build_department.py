#!/usr/bin/env python3
"""Genere le GeoJSON des cotes pour tout le departement Indre-et-Loire, en
reutilisant l'algorithme de slope_map.py (build_segments/build_climbs) sans
le modifier. Le MNT departemental etant beaucoup plus gros que celui de
Tours, on le charge en float32 (pas float64) pour limiter la memoire."""
import json
import os
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio
from scipy.ndimage import gaussian_filter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slope_map import (  # noqa: E402
    CORE_ROAD_TYPES,
    DEFAULT_THRESHOLDS,
    build_climbs,
    build_segments,
    climbs_to_geojson,
    is_bike_relevant,
)

GEOM_PATH, DEM_PATH, OUT_GEOJSON = sys.argv[1:4]

CHUNK_LENGTH = 80.0
SAMPLE_STEP = 15.0
MIN_LENGTH = 20.0
SMOOTH_RADIUS = 10.0
CLIMB_MIN_ELEV_GAIN = 15.0


def load_dem_f32(path, smooth_radius):
    ds = rasterio.open(path)
    print(f"MNT: shape={ds.shape}, dtype={ds.dtypes[0]}", flush=True)
    band = ds.read(1).astype(np.float32)
    nodata = ds.nodata
    if nodata is not None:
        band[band == nodata] = np.nan
    if smooth_radius > 0:
        res = (abs(ds.transform.a) + abs(ds.transform.e)) / 2
        sigma = smooth_radius / res
        mask = ~np.isnan(band)
        filled = np.where(mask, band, np.float32(0.0))
        smoothed_sum = gaussian_filter(filled, sigma=sigma)
        smoothed_weight = gaussian_filter(mask.astype(np.float32), sigma=sigma)
        with np.errstate(invalid="ignore", divide="ignore"):
            band = np.where(smoothed_weight > 0, smoothed_sum / smoothed_weight, np.nan).astype(np.float32)
    return ds, band, nodata


def make_sampler_f32(ds, band):
    transform = ds.transform
    inv = ~transform
    height, width = band.shape

    def sample(xs, ys):
        cols_f, rows_f = inv * (xs, ys)
        cols_i = np.floor(cols_f).astype(int)
        rows_i = np.floor(rows_f).astype(int)
        out_of_bounds = (cols_i < 0) | (cols_i >= width) | (rows_i < 0) | (rows_i >= height)
        cols = np.clip(cols_i, 0, width - 1)
        rows = np.clip(rows_i, 0, height - 1)
        z = band[rows, cols].astype(float)
        z[out_of_bounds] = np.nan
        return z

    return sample


def main():
    t0 = time.time()
    print(f"Lecture MNT: {DEM_PATH}", flush=True)
    ds, band, nodata = load_dem_f32(DEM_PATH, SMOOTH_RADIUS)
    sample = make_sampler_f32(ds, band)
    print(f"MNT pret en {time.time() - t0:.0f}s", flush=True)

    print(f"Lecture geometries: {GEOM_PATH}", flush=True)
    gdf = gpd.read_file(GEOM_PATH, layer="ways")
    if gdf.crs != ds.crs:
        print(f"Reprojection {gdf.crs} -> {ds.crs}", flush=True)
        gdf = gdf.to_crs(ds.crs)

    gdf = gdf[gdf["highway"].notna() & (gdf["highway"] != "")]
    gdf = gdf[gdf.apply(lambda r: is_bike_relevant(r.get("highway"), r.get("bicycle")), axis=1)]
    print(f"{len(gdf)} troncons apres filtrage", flush=True)

    t1 = time.time()
    print("Calcul des pentes...", flush=True)
    segments = build_segments(gdf, sample, CHUNK_LENGTH, SAMPLE_STEP, DEFAULT_THRESHOLDS, MIN_LENGTH)
    print(f"{len(segments)} segments colores generes en {time.time() - t1:.0f}s", flush=True)

    t2 = time.time()
    print("Regroupement des cotes...", flush=True)
    climbs = build_climbs(segments, DEFAULT_THRESHOLDS[0], sample, thresholds=DEFAULT_THRESHOLDS, snap_tolerance=2.0)
    climbs = [c for c in climbs if c["elev_gain_m"] > CLIMB_MIN_ELEV_GAIN]
    print(f"{len(climbs)} cotes (D+ > {CLIMB_MIN_ELEV_GAIN}m) en {time.time() - t2:.0f}s", flush=True)

    geojson = json.loads(climbs_to_geojson(climbs, segments.crs, DEFAULT_THRESHOLDS)) if climbs else {
        "type": "FeatureCollection", "features": []
    }
    with open(OUT_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"Ecrit {OUT_GEOJSON} ({len(climbs)} cotes) en {time.time() - t0:.0f}s total", flush=True)


if __name__ == "__main__":
    main()
