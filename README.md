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

Site en ligne : **https://cote-a-cote-app.web.app** — 509 944 côtes,
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
| `build_from_pbf.py` | Pipeline principal. Lit un PBF déjà filtré aux tronçons routiers (`osmium tags-filter -o highways.pbf source.pbf w/highway`), lit tous les nœuds (coordonnées + altitude déjà présente), puis écrit les tronçons « compatibles vélo » dans un fichier par tuile géographique (grille régulière d'1° × 1°, pas de découpage administratif), **avec l'altitude de chacun de leurs sommets**. Chaque tuile est ensuite traitée indépendamment : l'altitude est échantillonnée par un KD-tree *local à la tuile* (plus aucun index global de 116 M de nœuds à recharger par processus, c'était ce qui saturait la RAM), puis `build_segments` / `build_climbs` de `slope_map.py` calculent les côtes. Les tuiles sont traitées en parallèle (`ProcessPoolExecutor`, démarrage `spawn` — `fork` fait planter la pile géospatiale ; 4 processus, ~1-2 Go chacun). Reprise automatique via `<out_dir>/tile_progress.json`. **Les tronçons sans nom sont inclus par défaut** (`--named-only` pour les exclure). |
| `merge_departments.py` | Fusionne plusieurs GeoJSON de côtes en un seul, avec des `idx` uniques (`merge_departments.py in1.geojson in2.geojson ... out.geojson`). Le nom date du découpage par département de l'ancien pipeline mais fonctionne pareil avec les tuiles actuelles. |

**Côtes et revêtement — règles**

- *Rues nommées* : regroupées par nom de rue et continuité, voies parallèles dédoublonnées, replats de raccord comptés dans les stats.
- *Tronçons sans nom* (pistes, sentiers, voies de service…) : chaînés en « brins » de bout en bout (on coupe aux carrefours), les portions en pente séparées de moins de 60 m forment une même côte, les fragments de moins de 150 m sont écartés. Nommés « Piste sans nom », « Sentier sans nom », etc.
- *Revêtement* : le tag OSM `surface` est propagé jusqu'à la côte (part de longueur par revêtement). Quand il manque, un revêtement **probable** est déduit du type de voie (routes → asphalte ; `track` selon `tracktype`, sinon terre ; `path` / `bridleway` → terre ; voies de service et `footway` non devinés) et marqué `~` dans les données, « (probable) » dans la fiche. Rien n'est affiché si moins de 30 % de la côte est renseignée ou déduite.
- Les `footway` sans `bicycle=yes`/`designated` restent exclus du calcul.

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
| `interactive_departement.html` | Carte principale (MapLibre GL JS + PMTiles). Survol/clic sur une côte (tap tolérant au doigt), profil altimétrique coloré par pente, revêtement, filtre global par revêtement (pastilles en haut : Asphalte, Pavés, Gravier, Terre, Autre, Non renseigné — appliqué au revêtement principal de la côte), mise en page responsive (listes en bottom sheet sur mobile), sélecteur de fond plan / satellite (orthophotos IGN, libres et sans clé), **lien partageable par côte** (`#c=lat,lon,longueur,D+,nom&bm=sat|plan&f=asphalt,dirt` : le lien reprend aussi le fond de plan et les filtres de revêtement actifs ; le bouton « Partager la côte » de la fiche copie le lien (toast de confirmation), qui rouvre directement la côte ; l'identifiant n'utilise pas l'`idx` interne, qui change à chaque recalcul), sélection d'une zone par rectangle, à la souris ou au doigt (limitée au niveau département pour rester fiable — cf le filtrage tippecanoe ci-dessous). C'est ce fichier qui est copié vers `firebase_deploy/public/index.html` au déploiement. |
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
# nom versionne (ex: climbs_france_AAAA-MM-JJ.pmtiles) a reporter dans le html : le fichier est mis en
# cache 1h par les navigateurs, remplacer un fichier de meme nom casse la lecture PMTiles
cp climbs_france.pmtiles ../firebase_deploy/public/climbs_france_AAAA-MM-JJ.pmtiles
cd ../firebase_deploy && firebase deploy --only hosting:app
```

Nécessite en plus `osmium` (CLI, `brew install osmium-tool`), `tippecanoe`
et `pmtiles` (CLI) sur le PATH. Sur la France entière (~14M tronçons), le
calcul prend de l'ordre de 3 heures sur une machine à 8 cœurs / 16 Go (dont ~8 min pour écrire les tuiles). Pour interrompre un run, tuer aussi les processus `multiprocessing.spawn` (`pkill -f multiprocessing.spawn`), sinon ils restent en vie en orphelins avec leur mémoire.
