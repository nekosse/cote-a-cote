# Côte à côte

Carte interactive des côtes cyclables, calculées automatiquement à partir
d'un export **[OpenStreetMap](https://www.openstreetmap.org/)** pré-traité
où chaque nœud porte déjà son altitude précise (tag `ele`) — pas de saisie
manuelle, pas de modèle numérique de terrain (MNT) à télécharger séparément.

Pour chaque tronçon de route, l'algorithme calcule la pente (régression
linéaire de l'altitude le long du tronçon, pas une simple moyenne de pentes
instantanées, pour ne pas amplifier le bruit), regroupe les tronçons en
pente continus en "côtes", et calcule pour chacune sa longueur, son
dénivelé positif, sa pente moyenne et un profil altimétrique coloré par
pente locale.

Site en ligne : **https://cote-a-cote-app.web.app** — 313 801 côtes,
France métropolitaine + Corse.

## Architecture

Le site est **100% statique, sans backend** : les côtes sont précalculées
une fois par un pipeline Python, encodées en tuiles vectorielles
([PMTiles](https://protomaps.com/pmtiles)), et servies telles quelles.
Le navigateur ne télécharge que les tuiles couvrant la zone visible (grâce
aux requêtes HTTP Range que PMTiles utilise), donc le coût ne grandit pas
avec la couverture géographique.

Deux architectures antérieures ont été essayées puis abandonnées :
- une architecture "live" (backend qui recalcule les côtes à la volée sur
  la zone visible du navigateur) — trop complexe à faire tenir à l'échelle
  nationale ;
- un premier pipeline statique qui téléchargeait un MNT (IGN RGE ALTI 5m)
  et interrogeait Overpass département par département — fonctionnel mais
  fragile (timeouts Overpass, bug de géocodage Nominatim sur "Savoie",
  Corse absente du catalogue RGE ALTI 5m) et lent (réseau + rate limiting).
  Remplacé par le pipeline actuel une fois qu'un export PBF avec altitude
  déjà intégrée par nœud est devenu disponible : plus de MNT à gérer, plus
  de requêtes réseau, couverture Corse incluse.

```
pipeline/   -> calcule les cotes (PBF pretraite -> GeoJSON), tuile par tuile
frontend/   -> carte MapLibre GL JS qui affiche les tuiles PMTiles
firebase_deploy/ -> config + fichiers deployes sur Firebase Hosting
```

### `pipeline/`

| Fichier | Rôle |
|---|---|
| `slope_map.py` | Bibliothèque coeur de l'algorithme (découpage en segments, calcul de pente, regroupement en côtes, profil altimétrique coloré par pente, export GeoJSON). Contient aussi un CLI (`python3 slope_map.py`) qui génère une carte Folium/Leaflet autonome pour une seule zone à partir d'un MNT+GeoPackage locaux (utile pour du debug rapide sans repasser par tout le pipeline PMTiles). |
| `build_from_pbf.py` | Pipeline principal. Lit un PBF déjà filtré aux tronçons routiers (`osmium tags-filter -o highways.pbf source.pbf w/highway`), construit un nuage de points (coordonnées + altitude déjà présente sur chaque nœud) utilisé comme source d'échantillonnage (KD-tree, remplace le MNT raster), découpe la France en tuiles géographiques (grille régulière en degrés, pas de découpage administratif), et calcule les côtes tuile par tuile via `build_segments`/`build_climbs` de `slope_map.py`, sans les modifier. Les tuiles sont indépendantes : traitées en parallèle sur plusieurs cœurs (`ProcessPoolExecutor`, démarrage `spawn` — `fork` fait planter la pile géospatiale, cf commentaire dans le fichier). Reprise automatique via `<out_dir>/tile_progress.json`. |
| `merge_departments.py` | Fusionne plusieurs GeoJSON de côtes en un seul, avec des `idx` uniques (`merge_departments.py in1.geojson in2.geojson ... out.geojson`). Le nom date du découpage par département de l'ancien pipeline mais fonctionne pareil avec les tuiles actuelles. |

Usage :

```bash
osmium tags-filter -o highways.pbf source_osrm_pretraite.pbf w/highway --overwrite
python3 pipeline/build_from_pbf.py highways.pbf frontend/by_tile
python3 pipeline/merge_departments.py frontend/by_tile/climbs_*.geojson frontend/climbs_france.geojson
```

Le PBF source doit avoir un tag `ele` (altitude, en mètres) sur la quasi
totalité de ses nœuds — c'est le cas d'un export OSRM pré-traité avec
altitude intégrée. Sans ce tag, il faut repasser par un MNT + `slope_map.py`
directement (cf le CLI mentionné ci-dessus).

### `frontend/`

| Fichier | Rôle |
|---|---|
| `interactive_departement.html` | Carte principale (MapLibre GL JS + PMTiles). Survol/clic sur une côte, profil altimétrique coloré par pente, sélection d'une zone par rectangle (limitée au niveau département pour rester fiable — cf le filtrage tippecanoe ci-dessous). C'est ce fichier qui est copié vers `firebase_deploy/public/index.html` au déploiement. |
| `range_server.py` | Serveur HTTP statique minimal supportant les requêtes Range, nécessaire pour tester les PMTiles en local (`http.server` standard ne les supporte pas). |

Les fichiers volumineux générés dans ce dossier (`climbs_*.geojson`,
`*.mbtiles`, `*.pmtiles`, `by_tile/`) ne sont **pas** versionnés (voir
`.gitignore`) — ils se régénèrent via le pipeline.

À noter : à bas niveau de zoom, `tippecanoe --drop-densest-as-needed`
masque une partie des côtes pour respecter les limites de taille des
tuiles vectorielles (~500 Ko / tuile) — c'est normal et volontaire, mais
c'est aussi pourquoi la sélection de zone n'est activée qu'à partir du
zoom département (zoom ≥ 9), pour ne jamais donner un classement faussé.

### `firebase_deploy/`

Config Firebase Hosting (site `cote-a-cote-app`, cible `app`) + le dossier
`public/` réellement déployé. Déploiement :

```bash
cd firebase_deploy && firebase deploy --only hosting:app
```

## Pipeline complet (reproduire depuis zéro)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

osmium tags-filter -o highways.pbf <source_osrm_pretraite.pbf> w/highway --overwrite
python3 pipeline/build_from_pbf.py highways.pbf frontend/by_tile
python3 pipeline/merge_departments.py frontend/by_tile/climbs_*.geojson frontend/climbs_france.geojson

cd frontend
tippecanoe -o climbs_france.mbtiles -zg --extend-zooms-if-still-dropping --drop-densest-as-needed -l climbs -f climbs_france.geojson --force
pmtiles convert climbs_france.mbtiles climbs_france.pmtiles --force

cp interactive_departement.html ../firebase_deploy/public/index.html
cp climbs_france.pmtiles ../firebase_deploy/public/
cd ../firebase_deploy && firebase deploy --only hosting:app
```

Nécessite en plus `osmium` (CLI, `brew install osmium-tool`), `tippecanoe`
et `pmtiles` (CLI) sur le PATH. Sur la France entière (~14M tronçons), le
calcul prend de l'ordre d'une heure sur une machine à 8 coeurs.
