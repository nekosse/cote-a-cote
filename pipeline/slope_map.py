#!/usr/bin/env python3
"""
"Quote ta cote" : carte interactive (Folium/Leaflet) des cotes cyclables de
Tours, a partir :
  - d'un export OSM de lignes (highway=*)      -> geom_tours.gpkg
  - d'un MNT IGN (RGE ALTI)                     -> alt_tours.tif

Le fond de carte n'affiche que les pentes significatives (>= premier seuil),
chaque continuite reelle (bout a bout) coloree par pente moyenne - sans filtre
de longueur, pour ne jamais faire disparaitre un vrai bout de cote. Le panneau
"Top cotes" ne liste que celles au-dessus de --climb-min-length (classement,
pas d'effet sur l'affichage carte). Clic sur la carte ou dans le panneau : la
cote est mise en surbrillance (halo) et un panneau de detail affiche pente
moyenne / longueur / denivele.

Chaque troncon OSM est d'abord decoupe en morceaux de longueur ~chunk-length.
La pente de chaque morceau est la pente d'une regression lineaire de
l'altitude le long du morceau (et non une moyenne de pentes instantanees), ce
qui evite d'amplifier le bruit du MNT sur de courtes distances. Le MNT est en
plus legerement lisse (gaussien) avant echantillonnage pour le meme motif.

Les ponts et tunnels reels (bridge=yes / tunnel=yes) ne suivent pas le
terrain : leur pente n'est pas calculable depuis un MNT, ils sont donc exclus
du regroupement en cotes.

Par defaut seuls les types de voies pertinents pour le velo sont gardes
(routes, pistes cyclables, chemins/trottoirs avec bicycle=yes/designated...)
(--all-highways desactive ce filtre).
"""
import argparse
import json
import sys
import urllib.parse

import folium
import folium.folium as _folium_core
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import rasterio
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from shapely.geometry import LineString, box
from shapely.ops import linemerge

SITE_TITLE = "Côte à côte"

# folium charge par defaut jQuery/Bootstrap/Awesome-Markers/FontAwesome, dont on
# ne se sert pas ici (pas de popup Bootstrap, pas d'icones de marqueur). On les
# retire pour ne garder que Leaflet : page plus legere et plus robuste (moins de
# CDN externes = moins de points de defaillance au chargement).
_folium_core._default_js[:] = [item for item in _folium_core._default_js if item[0] == "leaflet"]
_folium_core._default_css[:] = [item for item in _folium_core._default_css if item[0] == "leaflet_css"]

DEFAULT_THRESHOLDS = [3.0, 6.0, 9.0]  # bornes en % : <t0 vert, <t1 jaune, <t2 orange, >=t2 rouge
COLORS = ["#37b24d", "#f7b731", "#f2762e", "#e63946"]  # vert, jaune, orange, rouge
STRUCTURE_COLOR = "#8892a0"  # pont / tunnel : pente non calculable depuis le MNT
ACCENT_COLOR = "#2563eb"

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
)

LINE_WEIGHT = 3
LINE_OPACITY = 0.85

# types 'highway' pertinents pour le velo par defaut (routes/pistes/voies mixtes).
# footway/steps/pedestrian ne sont inclus que si bicycle=yes/designated/permissive/permit.
BIKE_HIGHWAYS = {
    "cycleway", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "residential", "living_street",
    "unclassified", "track", "service", "path",
}
BIKE_ALLOWED_VALUES = {"yes", "designated", "permissive", "permit"}
ALWAYS_EXCLUDE_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "busway",
    "corridor", "construction", "platform",
}


def is_bike_relevant(highway, bicycle):
    if highway in ALWAYS_EXCLUDE_HIGHWAYS:
        return False
    if bicycle == "no":
        return False
    if bicycle in BIKE_ALLOWED_VALUES:
        return True
    return highway in BIKE_HIGHWAYS


def load_dem(path, smooth_radius):
    ds = rasterio.open(path)
    band = ds.read(1).astype(float)
    nodata = ds.nodata
    if nodata is not None:
        band[band == nodata] = np.nan
    if smooth_radius > 0:
        res = (abs(ds.transform.a) + abs(ds.transform.e)) / 2
        sigma = smooth_radius / res
        mask = ~np.isnan(band)
        filled = np.where(mask, band, 0.0)
        smoothed_sum = gaussian_filter(filled, sigma=sigma)
        smoothed_weight = gaussian_filter(mask.astype(float), sigma=sigma)
        with np.errstate(invalid="ignore", divide="ignore"):
            band = np.where(smoothed_weight > 0, smoothed_sum / smoothed_weight, np.nan)
    return ds, band, nodata


def make_sampler(ds, band):
    transform = ds.transform
    inv = ~transform
    height, width = band.shape

    def sample(xs, ys):
        cols_f, rows_f = inv * (xs, ys)
        cols_i = np.floor(cols_f).astype(int)
        rows_i = np.floor(rows_f).astype(int)
        # une coordonnee en dehors de l'emprise du MNT (pas juste une case
        # nodata a l'interieur) doit renvoyer NaN, pas la valeur du pixel de
        # bord le plus proche - sinon un desalignement OSM/MNT (ex: reseau
        # routier legerement plus large que le MNT telecharge) produit des
        # altitudes fausses en silence plutot qu'un trou detectable.
        out_of_bounds = (cols_i < 0) | (cols_i >= width) | (rows_i < 0) | (rows_i >= height)
        cols = np.clip(cols_i, 0, width - 1)
        rows = np.clip(rows_i, 0, height - 1)
        z = band[rows, cols].astype(float)
        z[out_of_bounds] = np.nan
        return z

    return sample


def color_for_slope(pct, thresholds):
    for t, c in zip(thresholds, COLORS):
        if pct < t:
            return c
    return COLORS[-1]


def slope_bin(pct, thresholds):
    """Meme logique que color_for_slope, mais renvoie l'index dans COLORS
    plutot que la couleur elle-meme (pour encoder un profil compactement)."""
    for i, t in enumerate(thresholds):
        if pct < t:
            return i
    return len(COLORS) - 1


def is_structure(row, bridge_names, tunnel_names):
    name = row.get("name")
    if row.get("bridge") == "yes" or (name and name in bridge_names):
        return True
    if row.get("tunnel") == "yes" or (name and name in tunnel_names):
        return True
    return False


def build_segments(gdf, sample, chunk_length, sample_step, thresholds, min_length):
    # certains troncons d'un meme pont/tunnel (piste cyclable, trottoir...) n'ont
    # pas eux-memes le tag bridge/tunnel dans l'export OSM : on le deduit du nom
    # partage avec un troncon qui, lui, porte le tag.
    bridge_names = set(gdf.loc[gdf["bridge"] == "yes", "name"].dropna())
    tunnel_names = set(gdf.loc[gdf["tunnel"] == "yes", "name"].dropna())
    rows = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        length = geom.length
        if length < min_length:
            continue

        name = row.get("name") or ""
        highway = row.get("highway") or ""
        surface = row.get("surface")
        surface = surface if isinstance(surface, str) else ""

        if is_structure(row, bridge_names, tunnel_names):
            kind = "pont" if (row.get("bridge") == "yes" or name in bridge_names) else "tunnel"
            rows.append(
                {
                    "geometry": geom,
                    "name": name,
                    "highway": f"{highway} ({kind})",
                    "surface": surface,
                    "pente_pct": None,
                    "color": STRUCTURE_COLOR,
                    "bin": -1,
                }
            )
            continue

        n_samples = max(2, int(np.ceil(length / sample_step)) + 1)
        dists = np.linspace(0, length, n_samples)
        pts = [geom.interpolate(d) for d in dists]
        xs = np.array([p.x for p in pts])
        ys = np.array([p.y for p in pts])
        zs = sample(xs, ys)

        if np.isnan(zs).all():
            continue

        # chunk boundaries along the line, every ~chunk_length
        n_chunks = max(1, int(np.ceil(length / chunk_length)))
        chunk_edges = np.linspace(0, length, n_chunks + 1)

        for i in range(n_chunks):
            d0, d1 = chunk_edges[i], chunk_edges[i + 1]
            mask = (dists >= d0 - 1e-6) & (dists <= d1 + 1e-6)
            idx = np.where(mask)[0]
            if len(idx) < 2:
                continue
            sub_dists = dists[idx]
            sub_xs, sub_ys, sub_zs = xs[idx], ys[idx], zs[idx]

            valid = ~np.isnan(sub_zs)
            if valid.sum() < 2:
                continue

            # pente = regression lineaire de l'altitude le long du morceau,
            # plus robuste au bruit ponctuel du MNT qu'une moyenne de pentes
            # instantanees entre points consecutifs.
            slope_ratio = np.polyfit(sub_dists[valid], sub_zs[valid], 1)[0]
            slope_pct = abs(slope_ratio) * 100
            color = color_for_slope(slope_pct, thresholds)

            sub_line = LineString(zip(sub_xs, sub_ys))
            rows.append(
                {
                    "geometry": sub_line,
                    "name": name,
                    "highway": highway,
                    "surface": surface,
                    "pente_pct": round(float(slope_pct), 1),
                    "color": color,
                    "bin": COLORS.index(color),
                }
            )
    gdf_out = gpd.GeoDataFrame(rows, crs=gdf.crs)
    if len(gdf_out):
        # trie pour dessiner les cotes marquees par-dessus le plat (mise en avant visuelle)
        gdf_out = gdf_out.sort_values("bin").reset_index(drop=True)
    return gdf_out


# types "chaussee" preferes pour le calcul du D+ d'une cote : une grande avenue a
# souvent trottoirs + pistes cyclables + les deux sens de circulation mappes comme
# des lignes paralleles distinctes sous le meme nom ; les sommer donnerait un D+
# multiplie par autant de lignes paralleles. On ne garde que ces types si presents.
CORE_ROAD_TYPES = {
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "residential", "living_street", "unclassified",
}


def _flatten_lines(geoms):
    """Aplati une liste de LineString/MultiLineString en une liste de
    LineString seules (linemerge n'accepte pas un melange des deux)."""
    flat = []
    for g in geoms:
        if g.geom_type == "MultiLineString":
            flat.extend(g.geoms)
        elif g.geom_type == "LineString":
            flat.append(g)
    return flat


def _endpoints(geom):
    """Points de bout d'une (Multi)LineString : la ou une autre rue peut se
    raccorder bout a bout."""
    if geom.geom_type == "LineString":
        coords = list(geom.coords)
        return [coords[0], coords[-1]] if coords else []
    if geom.geom_type == "MultiLineString":
        pts = []
        for line in geom.geoms:
            coords = list(line.coords)
            if coords:
                pts.extend([coords[0], coords[-1]])
        return pts
    return []


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i, j):
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[ri] = rj


def _connected_runs(group, snap_tolerance=2.0, restrict_branching=True):
    """Regroupe les lignes d'un meme groupe par connectivite bout a bout :
    identifie les 'brins' continus - par exemple chaque sens d'une chaussee
    separee, ou le trottoir a cote de la route - avant deduplication.

    Quand restrict_branching est actif (par defaut), une jonction n'enchaine
    deux troncons que si EXACTEMENT deux se touchent a cet endroit - un
    endroit ou 3+ troncons se rejoignent (carrefour, ou plusieurs chaussees
    paralleles d'une meme avenue qui se frolent) est traite comme une vraie
    coupure plutot que fusionne. Sans cette restriction, un brin fini par
    "sauter" d'une chaussee a l'autre a chaque carrefour d'une avenue a
    plusieurs voies (ex: Avenue de la Tranchee), ce qui cree un effet de
    branches visible sur la carte. On la desactive uniquement pour le
    regroupement geographique large (name_cluster_distance) ou il n'y a pas
    de vraie topologie a proteger."""
    rows = list(group.itertuples())
    n = len(rows)
    if n == 0:
        return []
    uf = _UnionFind(n)
    pts, owners = [], []
    for i, row in enumerate(rows):
        for p in _endpoints(row.geometry):
            pts.append(p)
            owners.append(i)
    if pts:
        if restrict_branching:
            pt_uf = _UnionFind(len(pts))
            tree = cKDTree(np.array(pts))
            for a, b in tree.query_pairs(snap_tolerance):
                pt_uf.union(a, b)
            junctions = {}
            for idx in range(len(pts)):
                junctions.setdefault(pt_uf.find(idx), set()).add(owners[idx])
            for owners_set in junctions.values():
                if len(owners_set) == 2:
                    i, j = tuple(owners_set)
                    uf.union(i, j)
                # 1 seul = impasse, 3+ = vrai carrefour/embranchement : pas
                # de fusion.
        else:
            tree = cKDTree(np.array(pts))
            for a, b in tree.query_pairs(snap_tolerance):
                uf.union(owners[a], owners[b])
    clusters = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)
    return [[rows[i] for i in members] for members in clusters.values()]


def _longest_linear_part(geom):
    """Ramene un LineString/MultiLineString issu d'une difference() a un seul
    LineString (le plus long morceau) : une avenue qui redevient a chaussee
    unique laisse parfois un residu multi-parties apres soustraction de la
    portion deja couverte, dont les micro-bouts ne sont pas significatifs."""
    if geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        return geom
    if geom.geom_type == "MultiLineString":
        parts = [g for g in geom.geoms if g.length > 0]
        return max(parts, key=lambda g: g.length) if parts else None
    return None


def _dedup_parallel(group, buffer_dist):
    """Parmi les lignes d'un meme nom, ecarte les portions qui sont surtout
    paralleles/superposees a une portion deja gardee (typiquement l'autre
    sens d'une chaussee separee, ou un trottoir a cote de la route), en ne
    gardant qu'un couloir d'espace par corridor.

    Le test de recouvrement (geometry.difference du buffer deja couvert) se
    fait troncon par troncon plutot que par brin entier : une decision au
    niveau du brin entier garde ou jette en bloc des troncons parfois
    seulement partiellement paralleles (avenue qui ne redevient a chaussee
    unique que sur une partie de son trace), ce qui laissait les deux
    chaussees visibles cote a cote a ces endroits (effet de branches sur une
    avenue a plusieurs voies, ex. Avenue de la Tranchee). Le test etant
    toujours purement geometrique (jamais un choix arbitraire entre deux
    troncons voisins sans rapport), il ne recree pas les trous artificiels
    que le decoupage par brin visait a l'origine a eviter."""
    rows = sorted(group.itertuples(), key=lambda r: -r.geometry.length)

    covered = None
    kept_geom_by_idx = {}
    for row in rows:
        geom = row.geometry
        if covered is None:
            kept_geom_by_idx[row.Index] = geom
            covered = geom.buffer(buffer_dist)
            continue
        overlap_len = geom.intersection(covered).length
        frac = overlap_len / geom.length if geom.length > 0 else 1.0
        if frac < 0.5:
            kept_geom_by_idx[row.Index] = geom
        else:
            remainder = _longest_linear_part(geom.difference(covered))
            if remainder is not None and remainder.length / geom.length >= 0.4:
                kept_geom_by_idx[row.Index] = remainder
        covered = covered.union(geom.buffer(buffer_dist))

    kept_indices = list(kept_geom_by_idx)
    result = group.loc[kept_indices].copy()
    result["geometry"] = [kept_geom_by_idx[i] for i in result.index]
    return result


def _merge_connected(items, snap_tolerance, points_fn=None):
    """Enchaine des 'items' (par defaut un par nom de rue) qui se prolongent
    bout a bout d'un nom a l'autre, pour qu'une cote qui traverse plusieurs
    rues successives (frequent : changement de nom en cours de montee)
    devienne une seule entree.

    points_fn(item) donne les points de connexion utilisables pour cet item.
    Une jonction n'enchaine que si EXACTEMENT deux items s'y touchent (l'un
    continue l'autre) - un carrefour ou 3 items ou plus se rejoignent est un
    vrai embranchement, jamais fusionne : c'est ce qui evite l'effet arbre."""
    if points_fn is None:
        points_fn = lambda it: _endpoints(it["geometry"])  # noqa: E731

    n = len(items)
    uf = _UnionFind(n)

    all_pts, owners = [], []
    for i, it in enumerate(items):
        for p in points_fn(it):
            all_pts.append(p)
            owners.append(i)

    if all_pts:
        pt_uf = _UnionFind(len(all_pts))
        tree = cKDTree(np.array(all_pts))
        for a, b in tree.query_pairs(snap_tolerance):
            pt_uf.union(a, b)

        junctions = {}
        for idx in range(len(all_pts)):
            junctions.setdefault(pt_uf.find(idx), set()).add(owners[idx])

        for owners_set in junctions.values():
            if len(owners_set) == 2:
                i, j = tuple(owners_set)
                uf.union(i, j)
            # 1 seul = impasse, 3+ = vrai carrefour/embranchement : pas de fusion

    clusters = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    merged = []
    for members in clusters.values():
        group = [items[i] for i in members]
        total_len = sum(b["length_m"] for b in group)
        elev_gain = sum(b["elev_gain_m"] for b in group)
        avg_grade = sum(b["avg_grade"] * b["length_m"] for b in group) / total_len

        by_name = {}
        for b in group:
            e = by_name.setdefault(b["name"], {"length_m": 0.0, "elev_gain_m": 0.0})
            e["length_m"] += b["length_m"]
            e["elev_gain_m"] += b["elev_gain_m"]
        anchor_name = max(by_name, key=lambda nm: by_name[nm]["elev_gain_m"])
        names = sorted(by_name, key=lambda nm: -by_name[nm]["length_m"])

        surfaces = {}
        for b in group:
            for sf, ln in b.get("surfaces", {}).items():
                surfaces[sf] = surfaces.get(sf, 0.0) + ln

        merged.append(
            {
                "name": anchor_name,
                "names": names,
                "surfaces": surfaces,
                "avg_grade": avg_grade,
                "length_m": total_len,
                "elev_gain_m": elev_gain,
                "geometry": (
                    linemerge(_flatten_lines([b["geometry"] for b in group]))
                    if len(group) > 1
                    else group[0]["geometry"]
                ),
            }
        )
    return merged


def _bridge_gaps(geometry, max_gap):
    """Relie par des segments droits les parties d'une (Multi)LineString qui
    sont proches (<= max_gap) mais pas topologiquement connectees - purement
    pour l'affichage. Sert a combler les trous laisses par de tres courts
    troncons d'origine exclus par --min-length (necessaire pour la fiabilite
    du calcul de pente sur de courtes distances, cf carrefours), sans changer
    ni les stats ni le seuil de fiabilite. Choisit les ponts les plus courts
    en premier (type Kruskal) pour ne relier que des parties reellement
    voisines."""
    if geometry.geom_type != "MultiLineString":
        return geometry
    parts = list(geometry.geoms)
    n = len(parts)
    if n < 2:
        return geometry

    endpoints = [(list(p.coords)[0], list(p.coords)[-1]) for p in parts]
    candidates = []
    for i in range(n):
        for j in range(i + 1, n):
            best = None
            for e1 in endpoints[i]:
                for e2 in endpoints[j]:
                    d = ((e1[0] - e2[0]) ** 2 + (e1[1] - e2[1]) ** 2) ** 0.5
                    if best is None or d < best[0]:
                        best = (d, e1, e2)
            if best and best[0] <= max_gap:
                candidates.append(best)
    candidates.sort(key=lambda c: c[0])

    uf = _UnionFind(n)
    # associe chaque extremite a son index de partie pour l'union-find
    endpoint_owner = {}
    for i, (e1, e2) in enumerate(endpoints):
        endpoint_owner[e1] = i
        endpoint_owner[e2] = i

    bridges = []
    for d, e1, e2 in candidates:
        i, j = endpoint_owner[e1], endpoint_owner[e2]
        if uf.find(i) != uf.find(j):
            uf.union(i, j)
            bridges.append(LineString([e1, e2]))

    if not bridges:
        return geometry
    return linemerge(parts + bridges)


def _trunk_path(geometry, sample):
    """Ne garde que le plus court chemin entre le point le plus bas et le
    point le plus haut de la geometrie (par altitude MNT), et jette le
    reste.

    Malgre la deduplication des chaussees paralleles (_dedup_parallel), un
    residu localise (ecart entre les deux chaussees superieur au buffer de
    dedup) peut subsister et se faire relier par _bridge_gaps : le trace
    fait alors un petit crochet/boucle hors de son axe principal avant de
    revenir. Une cote va d'un point bas a un point haut ; le plus court
    chemin altimetrique entre ces deux extremites suit le trace principal et
    evite naturellement un tel crochet (par definition plus long que d'aller
    tout droit), sans avoir a deviner un seuil de distance qui marcherait
    pour toutes les avenues (leur largeur de terre-plein varie trop d'un
    endroit a l'autre pour un seuil global fiable). Choisir les extremites
    par altitude plutot que par pure distance geographique evite aussi qu'un
    residu situe tout au bout de la cote (donc geographiquement extreme) ne
    soit pris a tort pour une des deux extremites du trace."""
    parts = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]

    def key(pt):
        return (round(pt[0], 1), round(pt[1], 1))

    G = nx.Graph()
    for part in parts:
        coords = list(part.coords)
        for i in range(len(coords) - 1):
            a, b = key(coords[i]), key(coords[i + 1])
            seg = LineString([coords[i], coords[i + 1]])
            if a == b:
                continue
            if not G.has_edge(a, b) or seg.length < G[a][b]["length"]:
                G.add_edge(a, b, length=seg.length, geometry=seg)

    if G.number_of_nodes() < 2:
        return geometry

    nodes = list(G.nodes)
    xs = np.array([n[0] for n in nodes])
    ys = np.array([n[1] for n in nodes])
    zs = sample(xs, ys)
    valid = ~np.isnan(zs)
    if valid.sum() < 2:
        return geometry
    valid_idx = np.where(valid)[0]
    a = nodes[valid_idx[np.argmin(zs[valid_idx])]]
    b = nodes[valid_idx[np.argmax(zs[valid_idx])]]
    if a == b:
        return geometry

    try:
        path_nodes = nx.shortest_path(G, a, b, weight="length")
    except nx.NetworkXNoPath:
        return geometry
    segs = [G[u][v]["geometry"] for u, v in zip(path_nodes[:-1], path_nodes[1:])]
    return linemerge(segs) if len(segs) > 1 else segs[0]


def _elevation_profile(geometry, sample, thresholds, n_points=20):
    """Echantillonne l'altitude a n_points repartis regulierement le long de
    la geometrie finale d'une cote (utilisee pour le mini-graphique de
    denivele au clic), et la pente locale de chaque segment entre deux
    points (pour colorer le graphique comme la carte : vert facile, rouge
    dur). Sur une geometrie encore MultiLineString (trunk_path n'a pas
    trouve de chemin, cas rare), on se rabat sur le plus long morceau
    plutot que d'echouer.

    Le sens de parcours de la geometrie (donc du profil) depend de l'ordre
    dans lequel linemerge/_merge_connected ont assemble les troncons - pas
    forcement du bas vers le haut. On force donc toujours l'affichage du
    point le plus bas vers le point le plus haut, quel que soit ce sens
    d'origine, pour qu'un graphique ne parte jamais "a l'envers"."""
    if geometry.geom_type == "MultiLineString":
        geometry = max(geometry.geoms, key=lambda g: g.length)
    total_len = geometry.length
    if total_len <= 0:
        return {"elevations": [], "grades": []}
    dists = np.linspace(0, total_len, n_points)
    pts = [geometry.interpolate(d) for d in dists]
    xs = np.array([p.x for p in pts])
    ys = np.array([p.y for p in pts])
    zs = sample(xs, ys)
    elevations = [round(float(z), 1) if not np.isnan(z) else None for z in zs]

    valid = [v for v in elevations if v is not None]
    if len(valid) >= 2 and valid[0] > valid[-1]:
        elevations = elevations[::-1]

    seg_dist = total_len / (n_points - 1) if n_points > 1 else total_len
    grades = []
    for z0, z1 in zip(elevations[:-1], elevations[1:]):
        if z0 is None or z1 is None or seg_dist <= 0:
            grades.append(0)
            continue
        pct = abs(z1 - z0) / seg_dist * 100
        grades.append(slope_bin(pct, thresholds))

    return {"elevations": elevations, "grades": grades}


def build_climbs(
    segments, min_grade, sample, thresholds=None, dedup_buffer=15.0, snap_tolerance=2.0,
    visual_gap_bridge=60.0, name_cluster_distance=250.0, climb_gap_distance=200.0,
):
    """Pour chaque nom de rue : filtre les types "chaussee" preferes, puis
    deduplique les brins paralleles (_dedup_parallel) sur TOUS les troncons
    du nom (pas seulement ceux en pente) - evite qu'une avenue a chaussees
    separees (trottoirs + pistes cyclables + 2 sens) ne multiplie son D+, et
    (fixe) travaille au niveau du brin continu pour ne pas creer de trous
    artificiels en piochant tantot un sens tantot l'autre.

    Un meme nom de rue tres courant (ex: "Rue de la Mairie") existe souvent
    plusieurs fois dans des communes differentes, sans aucun rapport entre
    elles. On regroupe donc d'abord les troncons d'un nom par proximite
    reelle (_connected_runs avec name_cluster_distance, largement plus
    genereux que la tolerance topologique stricte, pour ponter les vrais
    trous d'une meme rue sans fusionner deux rues homonymes eloignees de
    plusieurs centaines de metres) : chaque groupe de proximite devient sa
    propre "cote" identite, meme si plusieurs partagent le meme nom affiche.

    Au sein d'un groupe, toutes les pieces deduppees (TOUTES pentes
    confondues) sont fusionnees en une seule geometrie (linemerge) : un
    replat ou un court raccord entre deux portions grimpantes reste donc
    visible et connecte a l'ecran. Les stats (longueur/pente/D+) ne comptent
    que les portions reellement en pente (>= min_grade).

    Pour enchainer avec la rue suivante (changement de nom en cours de
    montee), seuls les 2 points de connexion du brin le plus long du groupe
    sont exposes (pas toutes les extremites internes) : un groupe qui
    contiendrait encore plusieurs morceaux deconnectes ne peut alors jamais
    agir comme un "hub" reliant entre elles des rues sans rapport.
    _merge_connected ne fusionne une jonction que si EXACTEMENT deux groupes
    s'y rejoignent - un vrai carrefour a 3 branches ou plus n'est jamais
    fusionne, ce qui evite l'effet arbre.

    Une meme route peut aussi rester UNE SEULE "location" (topologiquement
    continue, donc pas separee par name_cluster_distance) sur plusieurs
    kilometres alors qu'elle ne grimpe que par endroits (ex: une route
    departementale de campagne qui ondule sur 20km, avec quelques cotes
    courtes espacees de plusieurs centaines de metres a plusieurs
    kilometres) : sans sous-decoupage, toute la location serait affichee
    comme une seule cote geante alors que ses stats (longueur/D+) ne
    compteraient que les quelques centaines de metres reellement en pente -
    incoherence flagrante entre le trace affiche et les chiffres. On
    sous-regroupe donc les troncons EN PENTE par proximite reelle
    (climb_gap_distance, plus stricte que name_cluster_distance) : chaque
    sous-groupe devient sa propre cote, avec un trace limite a ses troncons
    en pente (+ un pontage court des micro-trous) plutot qu'a route entiere.

    Renvoie TOUTES les cotes trouvees, quelle que soit leur taille : le
    filtre de longueur minimale (pour le panneau classement) se fait a part,
    en aval, pour ne jamais faire disparaitre de vraies pentes de la carte
    juste parce qu'elles ne forment pas une "grosse" cote."""
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS
    named = segments[(segments["name"] != "") & segments["pente_pct"].notna()]
    if named.empty:
        return []

    name_climbs = []
    for name, group in named.groupby("name"):
        if group["highway"].isin(CORE_ROAD_TYPES).any():
            group = group[group["highway"].isin(CORE_ROAD_TYPES)]
        deduped = _dedup_parallel(group, dedup_buffer)

        for location_rows in _connected_runs(deduped, name_cluster_distance, restrict_branching=False):
            location = gpd.GeoDataFrame(location_rows, crs=deduped.crs)

            climbing = location[location["pente_pct"] >= min_grade]
            if climbing.empty:
                continue

            for climb_rows in _connected_runs(climbing, climb_gap_distance, restrict_branching=False):
                climb = gpd.GeoDataFrame(climb_rows, crs=climbing.crs)

                # le troncon affiche - et les stats - incluent aussi les
                # morceaux sous le seuil de pente (le "vert") qui relient
                # deux portions en pente de CE regroupement : un court replat
                # entre deux montees ne les separe pas assez pour former deux
                # cotes distinctes (cf climb_gap_distance), donc on le
                # parcourt bel et bien en grimpant cette cote - il doit
                # compter dans la longueur/le D+, pas seulement apparaitre a
                # l'ecran.
                climb_reach = climb.geometry.union_all().buffer(climb_gap_distance)
                display_rows = location[location.geometry.intersects(climb_reach)]

                disp_lengths = display_rows.geometry.length
                total_len = disp_lengths.sum()
                if total_len <= 0:
                    continue
                surfaces = disp_lengths[display_rows["surface"] != ""].groupby(display_rows["surface"]).sum().to_dict()
                avg_grade = (display_rows["pente_pct"] * disp_lengths).sum() / total_len
                elev_gain = (display_rows["pente_pct"] / 100 * disp_lengths).sum()

                all_geoms = list(display_rows.geometry)
                geometry = linemerge(all_geoms) if len(all_geoms) > 1 else all_geoms[0]
                geometry = _bridge_gaps(geometry, visual_gap_bridge)
                geometry = _trunk_path(geometry, sample)

                runs = _connected_runs(climb, snap_tolerance)
                longest_run_geoms = [r.geometry for r in max(runs, key=lambda run: sum(r.geometry.length for r in run))]
                longest_run_geom = linemerge(longest_run_geoms) if len(longest_run_geoms) > 1 else longest_run_geoms[0]
                connectors = _endpoints(longest_run_geom)[:2]

                name_climbs.append(
                    {
                        "name": name,
                        "avg_grade": float(avg_grade),
                        "length_m": float(total_len),
                        "elev_gain_m": float(elev_gain),
                        "geometry": geometry,
                        "surfaces": surfaces,
                        "connectors": connectors,
                    }
                )

    climbs = _merge_connected(name_climbs, snap_tolerance, points_fn=lambda it: it["connectors"])

    for c in climbs:
        union_wgs84 = gpd.GeoSeries([c["geometry"]], crs=segments.crs).to_crs(4326)
        b = union_wgs84.total_bounds  # minx, miny, maxx, maxy
        c["bounds"] = [[b[1], b[0]], [b[3], b[2]]]  # [[south, west], [north, east]]
        c["surface"] = _encode_surface(c.get("surfaces", {}), c["length_m"])
        c["avg_grade"] = round(c["avg_grade"], 1)
        c["length_m"] = round(c["length_m"])
        c["elev_gain_m"] = round(c["elev_gain_m"])
        profile = _elevation_profile(c["geometry"], sample, thresholds)
        c["profile"] = profile["elevations"]
        c["profile_grades"] = profile["grades"]

    climbs.sort(key=lambda c: c["elev_gain_m"], reverse=True)
    for i, c in enumerate(climbs):
        c["idx"] = i
    return climbs


SURFACE_MIN_KNOWN_SHARE = 0.3  # en dessous, trop peu de troncons renseignes pour conclure
SURFACE_MIN_SHARE = 0.1  # un revetement ne ressort que s'il couvre au moins 10% du renseigne


def _encode_surface(surfaces, total_length):
    """Revetement dominant d'une cote, en "valeur:pct,valeur:pct" (part du
    longueur RENSEIGNEE, pas de la longueur totale : beaucoup de tronçons OSM
    n'ont pas de tag surface, ne pas les compter evite de tout tirer vers
    "inconnu"). Chaine vide si moins de 30% de la cote est renseignee -
    mieux vaut ne rien afficher qu'une conclusion tiree de quelques metres.
    Plusieurs valeurs de surface peuvent etre separees par ';' dans OSM
    (ex: "asphalt;paving_stones") : on garde la premiere."""
    known = {}
    for sf, ln in surfaces.items():
        key = sf.split(";")[0].strip().lower()
        if key:
            known[key] = known.get(key, 0.0) + ln
    known_len = sum(known.values())
    if total_length <= 0 or known_len / total_length < SURFACE_MIN_KNOWN_SHARE:
        return ""
    ranked = sorted(known.items(), key=lambda kv: -kv[1])
    parts = [(k, round(100 * v / known_len)) for k, v in ranked if v / known_len >= SURFACE_MIN_SHARE]
    return ",".join(f"{k}:{pct}" for k, pct in parts[:3])


def _encode_profile(profile):
    """Encode le profil (liste d'altitudes) en chaine compacte "v,v,v,..." -
    les tuiles vectorielles (MVT) n'acceptent que des proprietes scalaires,
    pas de tableaux ni d'objets imbriques."""
    return ",".join("" if v is None else str(v) for v in profile)


def climbs_to_geojson(climbs, crs, thresholds):
    """Construit le FeatureCollection (WGS84) affiche sur la carte : une ligne
    fusionnee par cote, coloree selon sa pente moyenne."""
    gdf = gpd.GeoDataFrame(
        [{"idx": c["idx"], "name": c["name"], "avg_grade": c["avg_grade"],
          "length_m": c["length_m"], "elev_gain_m": c["elev_gain_m"],
          "surface": c["surface"],
          "profile": _encode_profile(c["profile"]),
          "profile_grades": _encode_profile(c["profile_grades"]), "geometry": c["geometry"]}
         for c in climbs],
        crs=crs,
    ).to_crs(4326)
    gdf["color"] = gdf["avg_grade"].apply(lambda g: color_for_slope(g, thresholds))
    return gdf.to_json()


BASE_STYLE = f"""
<style>
  .leaflet-control-zoom a {{
    border-radius: 8px !important;
    font-family: {FONT_STACK};
  }}
  .leaflet-control-zoom {{
    border: none !important;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.18) !important;
  }}
  .qtc-panel {{
    font-family: {FONT_STACK};
    background: rgba(255, 255, 255, 0.94);
    backdrop-filter: blur(6px);
    border-radius: 14px;
    box-shadow: 0 4px 24px rgba(15, 23, 42, 0.16);
    color: #1f2937;
  }}
  .qtc-brand {{
    position: fixed; top: 85px; left: 15px; z-index: 9999;
    padding: 10px 16px;
    font-weight: 700;
    font-size: 15px;
    letter-spacing: -0.01em;
  }}
  .qtc-brand span {{ color: {ACCENT_COLOR}; }}
  .qtc-rank {{
    flex: none; width: 22px; height: 22px; border-radius: 50%;
    color: white; font-size: 11px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
  }}
  #climbs-list li {{
    display: flex; align-items: center; gap: 10px;
    padding: 7px 16px; cursor: pointer; transition: background 0.12s;
  }}
  #climbs-list li:hover {{ background: rgba(37, 99, 235, 0.07); }}
  #climbs-list li.qtc-active {{ background: rgba(37, 99, 235, 0.14); }}
  .qtc-climb-name {{ font-weight: 600; font-size: 13px; line-height: 1.25; }}
  .qtc-climb-stats {{ color: #6b7280; font-size: 11.5px; margin-top: 1px; }}
  #climb-detail {{
    position: fixed; bottom: 24px; right: 15px; z-index: 10000;
    width: 300px; padding: 20px 22px;
    border-left: 6px solid transparent;
    box-shadow: 0 14px 45px rgba(15, 23, 42, 0.32) !important;
    opacity: 0; transform: translateY(18px) scale(0.96);
    pointer-events: none;
    transition: opacity 0.22s ease, transform 0.22s ease;
  }}
  #climb-detail.qtc-show {{
    opacity: 1; transform: translateY(0) scale(1);
    pointer-events: auto;
  }}
  #climb-detail-close {{
    position: absolute; top: 12px; right: 14px; cursor: pointer;
    color: #9ca3af; font-size: 20px; line-height: 1;
  }}
  #climb-detail-close:hover {{ color: #374151; }}
  .qtc-detail-name {{
    font-size: 19px; font-weight: 800; letter-spacing: -0.01em;
    padding-right: 20px; line-height: 1.2;
  }}
  .qtc-detail-via {{
    font-size: 11.5px; color: #6b7280; margin: 3px 0 14px; line-height: 1.35;
  }}
  .qtc-detail-stats {{ display: flex; gap: 22px; }}
  .qtc-detail-stat-value {{ font-size: 27px; font-weight: 800; line-height: 1; }}
  .qtc-detail-stat-label {{
    font-size: 11px; color: #6b7280; margin-top: 5px;
    text-transform: uppercase; letter-spacing: 0.03em;
  }}
  .qtc-profile {{ margin-top: 16px; }}
  .qtc-profile svg {{ width: 100%; height: 56px; display: block; }}
  .qtc-profile-range {{
    display: flex; justify-content: space-between;
    font-size: 10.5px; color: #9ca3af; margin-top: 2px;
  }}
</style>
"""


def build_map(bounds, thresholds, out_path, climbs, ranked_climbs, climbs_geojson):
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    m = folium.Map(
        location=center, zoom_start=13, tiles="CartoDB Positron",
        prefer_canvas=True, zoom_control=False,
    )
    m.get_root().header.add_child(folium.Element(f"<title>{SITE_TITLE}</title>"))
    favicon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<text y=".9em" font-size="90">\U0001f6b4</text></svg>'
    )
    favicon_href = "data:image/svg+xml," + urllib.parse.quote(favicon_svg)
    m.get_root().header.add_child(folium.Element(f'<link rel="icon" href="{favicon_href}">'))
    m.get_root().header.add_child(folium.Element(BASE_STYLE))

    def style_function(feature):
        return {"color": feature["properties"]["color"], "weight": LINE_WEIGHT, "opacity": LINE_OPACITY}

    climbs_layer = folium.GeoJson(
        climbs_geojson,
        style_function=style_function,
    ).add_to(m)

    m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

    map_name = m.get_name()
    layer_name = climbs_layer.get_name()

    # differe au "load" : injecte tel quel, ce script s'executerait avant la
    # ligne "var {map_name} = L.map(...)" plus bas dans le meme bloc <script>
    # (l'ordre d'ajout aux sections branca ne suit pas l'ordre du HTML final),
    # ce qui levait une exception JS bloquant tout le reste (tuiles, traces).
    m.get_root().script.add_child(
        folium.Element(
            f'window.addEventListener("load", function() {{'
            f'L.control.zoom({{ position: "bottomright" }}).addTo({map_name});'
            f"}});"
        )
    )

    # filet de securite : certains navigateurs calculent la taille du conteneur
    # de carte apres le premier fitBounds, ce qui bloque le zoom a 0. On refait
    # l'operation une fois la page/les styles charges.
    fix_js = f"""
    <script>
    window.addEventListener("load", function() {{
      setTimeout(function() {{
        {map_name}.invalidateSize();
        {map_name}.fitBounds([[{bounds[1]}, {bounds[0]}], [{bounds[3]}, {bounds[2]}]]);
      }}, 250);
    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(fix_js))

    brand_html = f'<div class="qtc-panel qtc-brand">&#128692; <span>{SITE_TITLE}</span></div>'
    m.get_root().html.add_child(folium.Element(brand_html))

    legend_html = f"""
    <div class="qtc-panel" style="position: fixed; bottom: 20px; left: 15px; z-index: 9999;
                padding: 12px 16px; font-size: 13px;">
      <div style="font-weight: 700; margin-bottom: 8px;">Pente</div>
      <div style="display:flex; align-items:center; gap:6px; margin:3px 0;">
        <span style="width:14px; height:14px; border-radius:4px; background:{COLORS[0]}; display:inline-block;"></span>
        &lt; {thresholds[0]:g}%
      </div>
      <div style="display:flex; align-items:center; gap:6px; margin:3px 0;">
        <span style="width:14px; height:14px; border-radius:4px; background:{COLORS[1]}; display:inline-block;"></span>
        {thresholds[0]:g}&ndash;{thresholds[1]:g}%
      </div>
      <div style="display:flex; align-items:center; gap:6px; margin:3px 0;">
        <span style="width:14px; height:14px; border-radius:4px; background:{COLORS[2]}; display:inline-block;"></span>
        {thresholds[1]:g}&ndash;{thresholds[2]:g}%
      </div>
      <div style="display:flex; align-items:center; gap:6px; margin:3px 0;">
        <span style="width:14px; height:14px; border-radius:4px; background:{COLORS[3]}; display:inline-block;"></span>
        &gt; {thresholds[2]:g}%
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    detail_html = """
    <div id="climb-detail" class="qtc-panel">
      <span id="climb-detail-close">&times;</span>
      <div id="climb-detail-body"></div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(detail_html))

    climbs_for_js = [{k: v for k, v in c.items() if k != "geometry"} for c in climbs]
    for c in climbs_for_js:
        c["color"] = color_for_slope(c["avg_grade"], thresholds)
    climbs_json = json.dumps(climbs_for_js, ensure_ascii=False)

    ranked_for_js = [{k: v for k, v in c.items() if k != "geometry"} for c in ranked_climbs]
    for c in ranked_for_js:
        c["color"] = color_for_slope(c["avg_grade"], thresholds)

    rows_html = "".join(
        f'<li data-idx="{c["idx"]}">'
        f'<span class="qtc-rank" style="background:{c["color"]}">{i + 1}</span>'
        f'<div class="qtc-climb-info">'
        f'<div class="qtc-climb-name">{c["name"]}</div>'
        f'<div class="qtc-climb-stats">{c["avg_grade"]:.1f}% &middot; {c["length_m"]} m &middot; '
        f'<b>+{c["elev_gain_m"]} m</b></div>'
        f"</div></li>"
        for i, c in enumerate(ranked_for_js)
    )
    total_elev_gain = round(sum(c["elev_gain_m"] for c in climbs))
    climbs_html = f"""
    <div id="climbs-panel" class="qtc-panel" style="position: fixed; top: 15px; right: 15px; z-index: 9999;
                width: 250px; max-height: 72vh; display: flex; flex-direction: column; overflow: hidden;">
      <div id="climbs-header" style="padding: 12px 16px; cursor: pointer;
                  border-bottom: 1px solid rgba(0,0,0,0.06);">
        <div style="display: flex; align-items: center; justify-content: space-between; font-weight: 700; font-size: 14px;">
          <span>&#9968;&#65039; Top c&ocirc;tes</span>
          <span id="climbs-chevron" style="transition: transform 0.2s;">&#9650;</span>
        </div>
        <div style="font-size: 12px; color: #6b7280; margin-top: 2px;">
          {len(climbs)} c&ocirc;tes &middot; &Sigma; D+ <b style="color:#1f2937;">{total_elev_gain} m</b>
        </div>
      </div>
      <ul id="climbs-list" style="list-style: none; margin: 0; padding: 6px 0; overflow-y: auto;">
        {rows_html}
      </ul>
    </div>
    """
    m.get_root().html.add_child(folium.Element(climbs_html))

    interaction_js = f"""
    <script>
    window.addEventListener("load", function() {{
      const climbsData = {climbs_json};
      const byIdx = {{}};
      climbsData.forEach(function(c) {{ byIdx[c.idx] = c; }});

      const map = {map_name};
      const climbsLayer = {layer_name};
      const listEl = document.getElementById("climbs-list");
      const headerEl = document.getElementById("climbs-header");
      const chevronEl = document.getElementById("climbs-chevron");
      const detailEl = document.getElementById("climb-detail");
      const detailBodyEl = document.getElementById("climb-detail-body");
      const detailCloseEl = document.getElementById("climb-detail-close");

      let haloLayer = null;
      let selectedIdx = null;
      const featureLayers = {{}};
      climbsLayer.eachLayer(function(layer) {{
        featureLayers[layer.feature.properties.idx] = layer;
      }});

      function clearSelection() {{
        if (haloLayer) {{ map.removeLayer(haloLayer); haloLayer = null; }}
        listEl.querySelectorAll("li").forEach(function(li) {{ li.classList.remove("qtc-active"); }});
        detailEl.classList.remove("qtc-show");
        selectedIdx = null;
      }}

      const PROFILE_COLORS = {json.dumps(COLORS)};

      function profileSvg(profile, grades, fallbackColor) {{
        const vals = (profile || []).filter(function(v) {{ return v !== null && v !== undefined; }});
        if (vals.length < 2) return "";
        const w = 260, h = 64, pad = 2;
        const min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
        const range = Math.max(max - min, 1);
        const pts = vals.map(function(v, i) {{
          const x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
          const y = h - pad - ((v - min) / range) * (h - 2 * pad);
          return [x, y];
        }});
        const ptsStr = pts.map(function(p) {{ return p[0].toFixed(1) + "," + p[1].toFixed(1); }}).join(" ");
        const area = ptsStr + " " + (w - pad) + "," + (h - pad) + " " + pad + "," + (h - pad);
        let segs = "";
        for (let i = 0; i < pts.length - 1; i++) {{
          const c = (grades && PROFILE_COLORS[grades[i]]) || fallbackColor;
          segs += '<line x1="' + pts[i][0].toFixed(1) + '" y1="' + pts[i][1].toFixed(1) +
            '" x2="' + pts[i + 1][0].toFixed(1) + '" y2="' + pts[i + 1][1].toFixed(1) +
            '" stroke="' + c + '" stroke-width="2.5" stroke-linecap="round"></line>';
        }}
        return '<div class="qtc-profile">' +
          '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
          '<polygon points="' + area + '" fill="#9ca3af" opacity="0.14"></polygon>' + segs +
          "</svg>" +
          '<div class="qtc-profile-range"><span>' + Math.round(min) + ' m</span><span>' + Math.round(max) + ' m</span></div>' +
          "</div>";
      }}

      function selectClimb(idx, opts) {{
        opts = opts || {{}};
        const c = byIdx[idx];
        if (!c) return;
        if (haloLayer) {{ map.removeLayer(haloLayer); haloLayer = null; }}
        const layer = featureLayers[idx];
        if (layer) {{
          haloLayer = L.geoJSON(layer.feature, {{
            style: {{ color: c.color, weight: 13, opacity: 0.32, lineCap: "round" }},
          }}).addTo(map);
          haloLayer.bringToBack();
        }}
        listEl.querySelectorAll("li").forEach(function(li) {{
          li.classList.toggle("qtc-active", parseInt(li.dataset.idx, 10) === idx);
        }});
        if (!opts.skipFly) {{
          map.flyToBounds(c.bounds, {{ padding: [60, 60], maxZoom: 16 }});
        }}
        detailEl.style.borderLeftColor = c.color;
        const viaHtml = c.names && c.names.length > 1
          ? '<div class="qtc-detail-via">via ' + c.names.join(', ') + '</div>'
          : '<div style="margin-bottom:14px;"></div>';
        detailBodyEl.innerHTML =
          '<div class="qtc-detail-name">' + c.name + '</div>' +
          viaHtml +
          '<div class="qtc-detail-stats">' +
          '<div><div class="qtc-detail-stat-value" style="color:' + c.color + ';">' + c.avg_grade.toFixed(1) + '%</div>' +
          '<div class="qtc-detail-stat-label">pente moy.</div></div>' +
          '<div><div class="qtc-detail-stat-value">' + c.length_m + '<span style="font-size:14px;"> m</span></div>' +
          '<div class="qtc-detail-stat-label">longueur</div></div>' +
          '<div><div class="qtc-detail-stat-value">+' + c.elev_gain_m + '<span style="font-size:14px;"> m</span></div>' +
          '<div class="qtc-detail-stat-label">d&eacute;niv.</div></div>' +
          '</div>' +
          profileSvg(c.profile, c.profile_grades, c.color);
        detailEl.classList.add("qtc-show");
        selectedIdx = idx;
      }}

      listEl.querySelectorAll("li").forEach(function(li) {{
        li.addEventListener("click", function() {{
          selectClimb(parseInt(li.dataset.idx, 10));
        }});
      }});

      climbsLayer.eachLayer(function(layer) {{
        const idx = layer.feature.properties.idx;
        layer.on("click", function() {{ selectClimb(idx); }});
        layer.on("mouseover", function() {{
          if (idx !== selectedIdx) layer.setStyle({{ weight: {LINE_WEIGHT + 3} }});
        }});
        layer.on("mouseout", function() {{
          if (idx !== selectedIdx) layer.setStyle({{ weight: {LINE_WEIGHT} }});
        }});
      }});

      headerEl.addEventListener("click", function() {{
        const collapsed = listEl.style.display === "none";
        listEl.style.display = collapsed ? "block" : "none";
        chevronEl.style.transform = collapsed ? "rotate(0deg)" : "rotate(180deg)";
      }});

      detailCloseEl.addEventListener("click", clearSelection);
    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(interaction_js))

    m.save(out_path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--geom", default="/Users/leo/dev/tours-slope-map/geom_tours_osm_extended.gpkg")
    p.add_argument("--geom-layer", default="ways")
    p.add_argument("--dem", default="/Users/leo/dev/tours-slope-map/alt_tours_extended.tif")
    p.add_argument("--out", default="/Users/leo/dev/tours-slope-map/tours_slope_map.html")
    p.add_argument("--chunk-length", type=float, default=80.0, help="longueur des morceaux colores (m)")
    p.add_argument("--sample-step", type=float, default=15.0, help="pas d'echantillonnage altimetrique (m)")
    p.add_argument(
        "--min-length",
        type=float,
        default=20.0,
        help="ignore les troncons plus courts (m) - sous ~20m le bruit du MNT 5m domine le signal",
    )
    p.add_argument(
        "--smooth-radius",
        type=float,
        default=10.0,
        help="rayon (m) du lissage gaussien applique au MNT avant echantillonnage, pour reduire le bruit (0 = desactive)",
    )
    p.add_argument(
        "--thresholds",
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="bornes de pente en %% separees par des virgules, ex: 3,6,9",
    )
    p.add_argument(
        "--all-highways",
        action="store_true",
        help="desactive le filtre velo et garde tous les types 'highway' (trottoirs, escaliers, etc.)",
    )
    p.add_argument(
        "--top-climbs", type=int, default=0, help="limite le nombre de cotes affichees (0 = toutes)"
    )
    p.add_argument(
        "--climb-min-length",
        type=float,
        default=80.0,
        help="longueur cumulee minimale (m) pour qu'une rue apparaisse dans le panneau",
    )
    p.add_argument(
        "--climb-min-elev-gain",
        type=float,
        default=15.0,
        help="D+ minimal (m) pour qu'une cote soit affichee (carte et panneau)",
    )
    p.add_argument(
        "--climb-snap-tolerance",
        type=float,
        default=2.0,
        help="tolerance (m) pour considerer que deux troncons partagent un vrai noeud topologique",
    )
    args = p.parse_args()

    thresholds = [float(t) for t in args.thresholds.split(",")]

    print(f"Lecture MNT: {args.dem}")
    ds, band, nodata = load_dem(args.dem, args.smooth_radius)
    sample = make_sampler(ds, band)

    print(f"Lecture geometries: {args.geom} (layer={args.geom_layer})")
    gdf = gpd.read_file(args.geom, layer=args.geom_layer)
    if gdf.crs != ds.crs:
        print(f"Reprojection {gdf.crs} -> {ds.crs}")
        gdf = gdf.to_crs(ds.crs)

    gdf = gdf[gdf["highway"].notna() & (gdf["highway"] != "")]
    if not args.all_highways:
        gdf = gdf[
            gdf.apply(lambda r: is_bike_relevant(r.get("highway"), r.get("bicycle")), axis=1)
        ]
    print(f"{len(gdf)} troncons apres filtrage")

    print("Calcul des pentes...")
    segments = build_segments(gdf, sample, args.chunk_length, args.sample_step, thresholds, args.min_length)
    print(f"{len(segments)} segments colores generes")

    print("Regroupement des cotes...")
    climbs = build_climbs(
        segments, thresholds[0], sample, thresholds=thresholds,
        snap_tolerance=args.climb_snap_tolerance,
    )
    climbs = [c for c in climbs if c["elev_gain_m"] > args.climb_min_elev_gain]
    print(f"{len(climbs)} pentes affichees sur la carte (D+ > {args.climb_min_elev_gain}m)")
    if not climbs:
        print("Aucune cote trouvee avec ces reglages, rien a afficher.")
        return

    ranked_climbs = [c for c in climbs if c["length_m"] >= args.climb_min_length]
    ranked_climbs.sort(key=lambda c: c["elev_gain_m"], reverse=True)
    if args.top_climbs and args.top_climbs > 0:
        ranked_climbs = ranked_climbs[: args.top_climbs]
    print(f"{len(ranked_climbs)} cotes classees dans le panneau")

    climbs_geojson = climbs_to_geojson(climbs, segments.crs, thresholds)
    bounds_wgs84 = gpd.GeoSeries([box(*segments.total_bounds)], crs=segments.crs).to_crs(4326).total_bounds

    print(f"Ecriture carte: {args.out}")
    build_map(bounds_wgs84, thresholds, args.out, climbs, ranked_climbs, climbs_geojson)
    print("Termine.")


if __name__ == "__main__":
    sys.exit(main())
