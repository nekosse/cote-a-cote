#!/usr/bin/env python3
"""Fusionne les dalles .asc d'un export RGE ALTI 5m en un seul GeoTIFF. Les
.asc IGN n'embarquent pas leur CRS (EPSG:2154 / Lambert-93 d'apres la
nomenclature du produit) : on le declare explicitement en enveloppant
chaque dalle dans un MemoryFile avant fusion.

Usage: build_dem_mosaic.py <tiles_glob> <out_path>"""
import glob
import sys
import time

import rasterio
from rasterio.io import MemoryFile
from rasterio.merge import merge

TILES_GLOB, OUT_PATH = sys.argv[1:3]
CRS = "EPSG:2154"

paths = sorted(glob.glob(TILES_GLOB))
print(f"{len(paths)} dalles trouvees", flush=True)

t0 = time.time()
memfiles = []
datasets = []
for i, p in enumerate(paths):
    with rasterio.open(p) as src:
        profile = src.profile.copy()
        profile["crs"] = CRS
        data = src.read()
    mf = MemoryFile()
    ds = mf.open(**profile)
    ds.write(data)
    memfiles.append(mf)
    datasets.append(ds)
    if (i + 1) % 50 == 0:
        print(f"  {i + 1}/{len(paths)} dalles chargees", flush=True)

print(f"Toutes les dalles chargees en {time.time() - t0:.0f}s, fusion...", flush=True)
t1 = time.time()
mosaic, out_transform = merge(datasets)
print(f"Fusion terminee en {time.time() - t1:.0f}s, shape={mosaic.shape}", flush=True)

out_profile = datasets[0].profile.copy()
out_profile.update(
    driver="GTiff",
    height=mosaic.shape[1],
    width=mosaic.shape[2],
    transform=out_transform,
    compress="deflate",
    predictor=3,
    tiled=True,
    blockxsize=256,
    blockysize=256,
)

with rasterio.open(OUT_PATH, "w", **out_profile) as dst:
    dst.write(mosaic)

for ds in datasets:
    ds.close()
for mf in memfiles:
    mf.close()

print(f"Ecrit {OUT_PATH}", flush=True)
