#!/usr/bin/env python3
"""Pipeline de calcul des cotes a partir d'un PBF OSRM pretraite dont chaque
noeud porte deja son altitude (tag ele) - remplace l'ancien pipeline
MNT (RGE ALTI) + Overpass par departement (build_dem_mosaic.py,
fetch_osm_bbox.py, build_department.py, build_france.py) : plus besoin de
telecharger/fusionner de MNT ni d'interroger Overpass, tout est deja dans le
fichier source, ce qui couvre aussi la Corse (absente du catalogue RGE ALTI
5m utilise par l'ancien pipeline).

Strategie (deux passes sur le PBF, une seule fois pour toute la France) :

  1. Tous les noeuds avec un tag ele -> tableaux numpy (id, lon, lat, ele),
     projetes en Lambert-93. Sert a la fois a construire les geometries des
     troncons et de nuage de points pour l'echantillonnage d'altitude
     (KD-tree, remplace le raster + interpolation bilineaire de
     slope_map.make_sampler). Prealloue en tableaux numpy fixes (pas de
     listes Python) - accumuler ~116M valeurs dans des listes Python
     exploserait la memoire (~15-20 Go rien que pour les listes, avant meme
     la conversion en numpy).

  2. Tous les troncons "pertinents velo" (meme filtre is_bike_relevant que
     l'ancien pipeline) -> geometrie deja construite (Lambert-93) + tags,
     ecrits directement dans un fichier .jsonl par tuile geographique
     (grille reguliere en degres, pas de decoupage administratif - evite le
     bug Nominatim et n'a pas besoin de polygones de departement). Idem :
     on n'accumule PAS les ~10M+ troncons de toute la France en memoire,
     on les ecrit au fil de l'eau, tuile par tuile, sur disque.

Puis, tuile par tuile (comme avant, une tuile par departement), on relit son
.jsonl et on rappelle build_segments/build_climbs de slope_map.py SANS LES
MODIFIER - seule la source d'altitude change (KD-tree au lieu de raster).

Reprise : si les .jsonl par tuile existent deja (passes 1/2 terminees), elles
sont sautees ; `tile_progress.json` marque chaque tuile "done" pour sauter le
calcul des cotes deja fait au prochain lancement.

Usage: build_from_pbf.py <highways.pbf> <out_dir>
(le PBF doit deja etre filtre aux ways avec highway=* + noeuds references,
 ex: osmium tags-filter -o highways.pbf source.pbf w/highway)
"""
import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import osmium
import geopandas as gpd
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slope_map import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    build_climbs,
    build_segments,
    climbs_to_geojson,
    is_bike_relevant,
)

PBF_PATH, OUT_DIR = sys.argv[1:3]
TILES_DIR = os.path.join(OUT_DIR, "_tiles_jsonl")
PROGRESS_PATH = os.path.join(OUT_DIR, "tile_progress.json")

CHUNK_LENGTH = 80.0
SAMPLE_STEP = 15.0
MIN_LENGTH = 20.0
CLIMB_MIN_ELEV_GAIN = 15.0
MAX_SAMPLE_DIST_M = 60.0  # au-dela, aucun noeud connu proche : altitude non fiable

TILE_LON_SIZE = 1.0
TILE_LAT_SIZE = 1.0
KEEP_COLS = ["name", "highway", "bicycle", "bridge", "tunnel", "oneway", "access", "service"]
NODE_CAPACITY_HINT = 130_000_000  # marge au-dessus des ~116.5M noeuds mesures sur le PBF filtre France

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(TILES_DIR, exist_ok=True)


class NodeCollector(osmium.SimpleHandler):
    """Tous les noeuds avec un tag ele, dans des tableaux numpy preallouees
    (pas de listes Python - cf note en tete de fichier)."""

    def __init__(self, capacity):
        super().__init__()
        self.ids = np.empty(capacity, dtype=np.int64)
        self.lons = np.empty(capacity, dtype=np.float64)
        self.lats = np.empty(capacity, dtype=np.float64)
        self.eles = np.empty(capacity, dtype=np.float32)
        self.n = 0

    def _grow(self):
        cap = len(self.ids) * 2
        for attr, dtype in (("ids", np.int64), ("lons", np.float64), ("lats", np.float64), ("eles", np.float32)):
            arr = np.empty(cap, dtype=dtype)
            arr[: self.n] = getattr(self, attr)[: self.n]
            setattr(self, attr, arr)

    def node(self, n):
        ele = n.tags.get("ele")
        if ele is None:
            return
        try:
            e = float(ele)
        except ValueError:
            return
        if self.n >= len(self.ids):
            self._grow()
        i = self.n
        self.ids[i] = n.id
        self.lons[i] = n.location.lon
        self.lats[i] = n.location.lat
        self.eles[i] = e
        self.n += 1


class WayWriter(osmium.SimpleHandler):
    """Troncons pertinents velo, geometrie construite immediatement (via les
    tableaux noeuds deja en memoire) et ecrite dans le .jsonl de sa tuile -
    jamais accumules en memoire pour toute la France."""

    def __init__(self, node_ids, node_x, node_y, node_lons, node_lats, tiles_dir):
        super().__init__()
        self.node_ids = node_ids
        self.node_x = node_x
        self.node_y = node_y
        self.node_lons = node_lons
        self.node_lats = node_lats
        self.tiles_dir = tiles_dir
        self.handles = {}
        self.n_seen = 0
        self.n_kept = 0
        self.n_dropped = 0

    def _handle_for(self, tile_key):
        h = self.handles.get(tile_key)
        if h is None:
            name = f"lon{tile_key[0]:+03d}_lat{tile_key[1]:+03d}"
            h = open(os.path.join(self.tiles_dir, f"{name}.jsonl"), "a", encoding="utf-8")
            self.handles[tile_key] = h
        return h

    def way(self, w):
        self.n_seen += 1
        highway = w.tags.get("highway")
        bicycle = w.tags.get("bicycle")
        if not highway or not is_bike_relevant(highway, bicycle):
            return
        node_ids_w = np.fromiter((n.ref for n in w.nodes), dtype=np.int64, count=len(w.nodes))
        if len(node_ids_w) < 2:
            return
        idx = np.searchsorted(self.node_ids, node_ids_w)
        idx = np.clip(idx, 0, len(self.node_ids) - 1)
        valid = self.node_ids[idx] == node_ids_w
        if valid.sum() < 2:
            self.n_dropped += 1
            return
        idx = idx[valid]

        tile_key = (
            int(np.floor(self.node_lons[idx[0]] / TILE_LON_SIZE)),
            int(np.floor(self.node_lats[idx[0]] / TILE_LAT_SIZE)),
        )
        rec = {
            "osmid": str(w.id),
            "coords": list(zip(self.node_x[idx].tolist(), self.node_y[idx].tolist())),
        }
        for k in KEEP_COLS:
            v = w.tags.get(k)
            if v is not None:
                rec[k] = v
        self._handle_for(tile_key).write(json.dumps(rec) + "\n")
        self.n_kept += 1

    def close(self):
        for h in self.handles.values():
            h.close()


def load_progress():
    if os.path.exists(PROGRESS_PATH):
        return json.load(open(PROGRESS_PATH))
    return {}


def save_progress(progress):
    json.dump(progress, open(PROGRESS_PATH, "w"), indent=1)


def process_tile(jsonl_path, sample):
    rows = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            row = {k: rec.get(k) for k in KEEP_COLS}
            row["osmid"] = rec["osmid"]
            row["geometry"] = LineString(rec["coords"])
            rows.append(row)
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:2154")
    segments = build_segments(gdf, sample, CHUNK_LENGTH, SAMPLE_STEP, DEFAULT_THRESHOLDS, MIN_LENGTH)
    climbs = build_climbs(segments, DEFAULT_THRESHOLDS[0], sample, thresholds=DEFAULT_THRESHOLDS, snap_tolerance=2.0)
    climbs = [c for c in climbs if c["elev_gain_m"] > CLIMB_MIN_ELEV_GAIN]
    geojson = json.loads(climbs_to_geojson(climbs, gdf.crs, DEFAULT_THRESHOLDS)) if climbs else {
        "type": "FeatureCollection", "features": []
    }
    return len(rows), len(climbs), geojson


# Construites une fois par processus par _init_worker (methode de demarrage
# "spawn", pas "fork" - cf note dans _init_worker).
_TREE = None
_NODE_ELES = None


class _EleCollector(osmium.SimpleHandler):
    """Comme NodeCollector, mais sans les ids : chaque processus n'a besoin
    ici que de coordonnees+altitude pour interroger le KD-tree, pas de faire
    de lookup par id (ca, c'est uniquement necessaire une fois, cote parent,
    pour construire la geometrie des troncons)."""

    def __init__(self, capacity):
        super().__init__()
        self.lons = np.empty(capacity, dtype=np.float32)
        self.lats = np.empty(capacity, dtype=np.float32)
        self.eles = np.empty(capacity, dtype=np.float32)
        self.n = 0

    def _grow(self):
        cap = len(self.lons) * 2
        for attr in ("lons", "lats", "eles"):
            arr = np.empty(cap, dtype=np.float32)
            arr[: self.n] = getattr(self, attr)[: self.n]
            setattr(self, attr, arr)

    def node(self, n):
        ele = n.tags.get("ele")
        if ele is None:
            return
        try:
            e = float(ele)
        except ValueError:
            return
        if self.n >= len(self.lons):
            self._grow()
        i = self.n
        self.lons[i] = n.location.lon
        self.lats[i] = n.location.lat
        self.eles[i] = e
        self.n += 1


def _init_worker(pbf_path):
    """Execute une fois au demarrage de chaque processus du pool : reconstruit
    localement le KD-tree d'altitude. On utilise la methode de demarrage
    "spawn" (processus Python neuf, pas un fork() du parent) - un premier
    essai avec fork() plantait les processus enfants (BrokenProcessPool),
    tres probablement les bibliotheques C de la pile geospatiale (PROJ/GEOS
    via pyproj/shapely) et/ou les threads BLAS de numpy/scipy ne survivent
    pas a un fork() une fois deja inities dans le parent - un probleme connu
    de cet ecosysteme, raison pour laquelle macOS utilise "spawn" par defaut
    (on l'avait explicitement force a "fork" pour beneficier du
    copy-on-write, ce qui a reintroduit exactement ce probleme).
    Chaque worker relit donc les noeuds du PBF pour son propre KD-tree -
    redondant avec la passe faite dans le process principal, mais rapide
    (quelques minutes) et fait une seule fois au demarrage du pool, pas par
    tuile."""
    global _TREE, _NODE_ELES
    ec = _EleCollector(NODE_CAPACITY_HINT)
    ec.apply_file(pbf_path)
    lons, lats, eles = ec.lons[: ec.n], ec.lats[: ec.n], ec.eles[: ec.n]
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    x, y = tr.transform(lons, lats)
    _TREE = cKDTree(np.column_stack([x.astype(np.float32), y.astype(np.float32)]))
    _NODE_ELES = eles


def _sample_global(xs, ys):
    pts = np.column_stack([xs, ys])
    dist, idx = _TREE.query(pts, k=1, workers=1)
    z = _NODE_ELES[idx].astype(float)
    z[dist > MAX_SAMPLE_DIST_M] = np.nan
    return z


def _process_tile_worker(fname):
    tile_name = fname[: -len(".jsonl")]
    t5 = time.time()
    try:
        n_ways, n_climbs, geojson = process_tile(os.path.join(TILES_DIR, fname), _sample_global)
        out_path = os.path.join(OUT_DIR, f"climbs_{tile_name}.geojson")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f)
        return tile_name, "done", n_ways, n_climbs, None, time.time() - t5
    except Exception as e:
        return tile_name, "error", 0, 0, str(e), time.time() - t5


def main():
    t0 = time.time()

    print("Lecture des noeuds (avec altitude)...", flush=True)
    nc = NodeCollector(NODE_CAPACITY_HINT)
    nc.apply_file(PBF_PATH)
    node_ids = nc.ids[: nc.n]
    node_lons = nc.lons[: nc.n]
    node_lats = nc.lats[: nc.n]
    node_eles = nc.eles[: nc.n]
    del nc
    print(f"  {len(node_ids)} noeuds en {time.time()-t0:.0f}s", flush=True)
    assert np.all(np.diff(node_ids) > 0), "noeuds non tries par id - hypothese invalide"

    print("Reprojection Lambert-93 (EPSG:2154)...", flush=True)
    t1 = time.time()
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    node_x, node_y = tr.transform(node_lons, node_lats)
    node_x = node_x.astype(np.float32)
    node_y = node_y.astype(np.float32)
    print(f"  fait en {time.time()-t1:.0f}s", flush=True)

    tiles_already_built = any(f.endswith(".jsonl") for f in os.listdir(TILES_DIR))
    if not tiles_already_built:
        print("Lecture + ecriture des troncons par tuile...", flush=True)
        t3 = time.time()
        ww = WayWriter(node_ids, node_x, node_y, node_lons, node_lats, TILES_DIR)
        ww.apply_file(PBF_PATH)
        ww.close()
        print(f"  {ww.n_kept}/{ww.n_seen} troncons bike-relevant ({ww.n_dropped} ecartes, noeud manquant) "
              f"en {len(ww.handles)} tuiles, {time.time()-t3:.0f}s", flush=True)
        del ww
    else:
        print("Tuiles .jsonl deja presentes - ecriture sautee (reprise).", flush=True)

    del node_ids, node_lons, node_lats, node_x, node_y, node_eles

    progress = load_progress()
    tile_files = sorted(f for f in os.listdir(TILES_DIR) if f.endswith(".jsonl"))
    pending = [f for f in tile_files if progress.get(f[: -len(".jsonl")], {}).get("status") != "done"]
    print(f"\n{len(tile_files)} tuiles au total, {len(pending)} a traiter", flush=True)

    # Tuiles independantes -> traitees en parallele. Chaque worker reconstruit
    # son propre KD-tree au demarrage (cf _init_worker) ; on limite le nombre
    # de workers pour ne pas multiplier une structure de plusieurs Go par
    # worker au point de saturer la RAM (16 Go sur cette machine).
    n_workers = min(4, max(1, (os.cpu_count() or 4) - 1))
    print(f"Traitement sur {n_workers} processus en parallele (spawn, chacun reconstruit son KD-tree)...", flush=True)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx, initializer=_init_worker, initargs=(PBF_PATH,)) as ex:
        futures = {ex.submit(_process_tile_worker, f): f for f in pending}
        for i, fut in enumerate(as_completed(futures)):
            tile_name, status, n_ways, n_climbs, err, dt = fut.result()
            if status == "done":
                progress[tile_name] = {"status": "done", "n_climbs": n_climbs, "n_ways": n_ways}
                print(f"[{i+1}/{len(pending)}] {tile_name}: {n_ways} troncons -> {n_climbs} cotes en {dt:.0f}s", flush=True)
            else:
                progress[tile_name] = {"status": "error", "error": err}
                print(f"[{i+1}/{len(pending)}] {tile_name}: ERREUR {err}", flush=True)
            save_progress(progress)

    done = sum(1 for v in progress.values() if v.get("status") == "done")
    total_climbs = sum(v.get("n_climbs", 0) for v in progress.values() if v.get("status") == "done")
    print(f"\n=== TERMINE : {done}/{len(tile_files)} tuiles, {total_climbs} cotes au total en {time.time()-t0:.0f}s ===", flush=True)


if __name__ == "__main__":
    main()
