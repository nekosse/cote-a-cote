#!/usr/bin/env python3
"""Ajoute `bot` / `top` (extremites basse et haute, "lon,lat") a des GeoJSON de cotes
DEJA calcules, sans relancer tout le pipeline (~3 h sur la France).

Les cotes calculees par build_from_pbf.py depuis que slope_map._climb_ends existe ont deja
ces proprietes ; ce script sert a rattraper un jeu de donnees plus ancien. Il refait la meme
chose que _climb_ends : les deux bouts du trace (le plus long morceau si MultiLineString),
classes par altitude, l'altitude etant celle du noeud OSM le plus proche (<= 150 m) du PBF
source, comme pour l'echantillonnage du pipeline.

Une extremite sans noeud a moins de 150 m, ou deux altitudes trop proches pour trancher, donne
bot/top vides pour cette cote : elle est simplement ignoree par la proposition d'itineraire.

Usage: add_endpoints.py <highways.pbf> <in_dir> <out_dir>
  <in_dir>  : dossier de climbs_*.geojson (WGS84)
  <out_dir> : dossier de sortie (memes noms de fichiers)
"""
import glob
import json
import os
import sys
import time

import numpy as np
import osmium
from scipy.spatial import cKDTree

PBF_PATH, IN_DIR, OUT_DIR = sys.argv[1:4]
# Les tracés des cotes sont faits de points interpoles tous les 15 m (pas de noeuds OSM) : sur une
# route rurale a noeuds espaces, une extremite peut etre a >60 m du noeud le plus proche. On
# tolere 150 m, et on ecarte la cote si les deux altitudes sont trop proches pour les ordonner.
MAX_DIST_M = 150.0
MIN_DELTA_M = 3.0
CHUNK = 3_000_000
# projection equirectangulaire locale (suffisante pour une tolerance de 60 m sur la France)
LAT0 = np.radians(46.5)
KX = 111320.0 * np.cos(LAT0)
KY = 111320.0

os.makedirs(OUT_DIR, exist_ok=True)


def longest_line(geom):
    if geom["type"] == "LineString":
        return geom["coordinates"]
    lines = geom["coordinates"]

    def length(line):
        return sum(abs(b[0] - a[0]) * KX + abs(b[1] - a[1]) * KY for a, b in zip(line[:-1], line[1:]))

    return max(lines, key=length)


def to_xy(lon, lat):
    return np.column_stack([np.asarray(lon, dtype=np.float64) * KX, np.asarray(lat, dtype=np.float64) * KY])


class NodeFeeder(osmium.SimpleHandler):
    """Lit les noeuds du PBF par paquets et met a jour, pour chaque extremite, l'altitude du
    noeud le plus proche (a moins de MAX_DIST_M)."""

    def __init__(self, tree, n_points):
        super().__init__()
        self.tree = tree
        self.best_dist = np.full(n_points, np.inf)
        self.best_ele = np.full(n_points, np.nan, dtype=np.float32)
        self.lon = np.empty(CHUNK, dtype=np.float32)
        self.lat = np.empty(CHUNK, dtype=np.float32)
        self.ele = np.empty(CHUNK, dtype=np.float32)
        self.n = 0
        self.seen = 0

    def node(self, n):
        e = n.tags.get("ele")
        if e is None:
            return
        try:
            z = float(e)
        except ValueError:
            return
        i = self.n
        self.lon[i] = n.location.lon
        self.lat[i] = n.location.lat
        self.ele[i] = z
        self.n += 1
        if self.n == CHUNK:
            self.flush()

    def flush(self):
        if not self.n:
            return
        m = self.n
        dist, idx = self.tree.query(to_xy(self.lon[:m], self.lat[:m]), distance_upper_bound=MAX_DIST_M, workers=-1)
        ok = np.isfinite(dist)
        d, k, z = dist[ok], idx[ok], self.ele[:m][ok]
        if len(d):
            # plusieurs noeuds peuvent viser la meme extremite : on garde le plus proche
            order = np.lexsort((d, k))
            k, d, z = k[order], d[order], z[order]
            first = np.r_[True, k[1:] != k[:-1]]
            k, d, z = k[first], d[first], z[first]
            better = d < self.best_dist[k]
            self.best_dist[k[better]] = d[better]
            self.best_ele[k[better]] = z[better]
        self.seen += m
        self.n = 0


def main():
    t0 = time.time()
    files = sorted(glob.glob(os.path.join(IN_DIR, "climbs_*.geojson")))
    print(f"{len(files)} fichiers", flush=True)

    # 1. extremites de toutes les cotes
    ends, spans = [], []  # ends : [(lon, lat), ...] 2 par cote ; spans : (fichier, nb de cotes)
    for path in files:
        feats = json.load(open(path, encoding="utf-8"))["features"]
        for f in feats:
            line = longest_line(f["geometry"])
            ends.append(line[0][:2])
            ends.append(line[-1][:2])
        spans.append(len(feats))
    ends = np.asarray(ends, dtype=np.float64)
    print(f"{len(ends) // 2} cotes, {len(ends)} extremites ({time.time()-t0:.0f}s)", flush=True)

    # 2. altitude de chaque extremite = noeud OSM le plus proche
    tree = cKDTree(to_xy(ends[:, 0], ends[:, 1]))
    feeder = NodeFeeder(tree, len(ends))
    feeder.apply_file(PBF_PATH)
    feeder.flush()
    ele = feeder.best_ele
    print(f"{feeder.seen} noeuds lus, {int(np.isfinite(feeder.best_dist).sum())}/{len(ends)} extremites "
          f"avec altitude ({time.time()-t0:.0f}s)", flush=True)

    # 3. ecriture : bot/top selon l'altitude
    fmt = lambda p: f"{p[0]:.5f},{p[1]:.5f}"  # noqa: E731
    pos = n_ok = n_swap = 0
    for path, count in zip(files, spans):
        data = json.load(open(path, encoding="utf-8"))
        for f in data["features"]:
            p0, p1 = ends[pos], ends[pos + 1]
            z0, z1 = ele[pos], ele[pos + 1]
            pos += 2
            if np.isnan(z0) or np.isnan(z1) or abs(z0 - z1) < MIN_DELTA_M:
                f["properties"]["bot"] = f["properties"]["top"] = ""
                continue
            if z0 > z1:
                p0, p1 = p1, p0
                n_swap += 1
            f["properties"]["bot"], f["properties"]["top"] = fmt(p0), fmt(p1)
            n_ok += 1
        with open(os.path.join(OUT_DIR, os.path.basename(path)), "w", encoding="utf-8") as out:
            json.dump(data, out)
    total = len(ends) // 2
    print(f"bot/top ajoutes a {n_ok}/{total} cotes ({100 * n_ok / total:.1f} %), "
          f"{n_swap} inversees (la ligne partait du haut) en {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
