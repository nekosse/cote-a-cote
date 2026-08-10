# Côte à côte

Carte interactive des côtes cyclables, calculées automatiquement à partir de
données publiques (pas de saisie manuelle) :

- **[IGN RGE ALTI 5m](https://geoservices.ign.fr/rgealti)** — modèle
  numérique de terrain (altitude tous les 5m)
- **[OpenStreetMap](https://www.openstreetmap.org/)** — tracé des routes,
  pistes et chemins (`highway=*`)

Pour chaque tronçon de route, l'algorithme calcule la pente (régression
linéaire de l'altitude le long du tronçon, pas une simple moyenne de pentes
instantanées, pour ne pas amplifier le bruit du MNT), regroupe les tronçons
en pente continus en "côtes", et calcule pour chacune sa longueur, son
dénivelé positif, sa pente moyenne et un profil altimétrique coloré par
pente locale.

Site en ligne : **https://cote-a-cote-app.web.app**

## Architecture

Le site est **100% statique, sans backend** : les côtes sont précalculées
une fois par un pipeline Python, encodées en tuiles vectorielles
([PMTiles](https://protomaps.com/pmtiles)), et servies telles quelles.
Le navigateur ne télécharge que les tuiles couvrant la zone visible (grâce
aux requêtes HTTP Range que PMTiles utilise), donc le coût ne grandit pas
avec la couverture géographique — c'est ce qui permet de viser toute la
France (et au-delà) sans jamais avoir à faire tourner de serveur de calcul
en production.

Une première architecture "live" (backend qui recalcule les côtes à la
volée sur la zone visible) a été prototypée puis abandonnée au profit de
cette approche statique, plus simple à faire tenir à l'échelle nationale.

```
pipeline/   -> calcule les cotes (MNT + OSM -> GeoJSON), departement par departement
frontend/   -> carte MapLibre GL JS qui affiche les tuiles PMTiles
firebase_deploy/ -> config + fichiers deployes sur Firebase Hosting
```

### `pipeline/`

| Fichier | Rôle |
|---|---|
| `slope_map.py` | Bibliothèque coeur de l'algorithme (découpage en segments, calcul de pente, regroupement en côtes, profil altimétrique colore par pente, export GeoJSON). Contient aussi un CLI (`python3 slope_map.py`) qui génère une carte Folium/Leaflet autonome pour une seule zone (utile pour du debug rapide sans repasser par tout le pipeline PMTiles). |
| `build_dem_mosaic.py` | Fusionne les dalles `.asc` d'un export RGE ALTI en un seul GeoTIFF (`build_dem_mosaic.py <tiles_glob> <out.tif>`). |
| `fetch_osm_bbox.py` | Récupère les tronçons OSM (`highway=*`) sur l'emprise d'un GeoTIFF donné (`fetch_osm_bbox.py <dem.tif> <out.gpkg>`). Utilise l'emprise du MNT plutôt qu'un géocodage par nom — Nominatim résout par exemple "Savoie, France" vers la Haute-Savoie. |
| `build_department.py` | Calcule les côtes pour un département à partir de son GeoPackage OSM + son GeoTIFF, écrit un GeoJSON (`build_department.py <osm.gpkg> <dem.tif> <out.geojson>`). |
| `build_france.py` | Orchestre les trois scripts ci-dessus pour tous les départements du catalogue RGE ALTI. Télécharge, traite puis supprime immédiatement chaque MNT/export OSM intermédiaire (ne garde que le petit GeoJSON par département). Reprise automatique via `france_progress.json` (départements déjà `done` sautés) ; les erreurs sont logguées et sautées, sans bloquer le run. |
| `merge_departments.py` | Fusionne plusieurs GeoJSON de côtes en un seul, avec des `idx` uniques (`merge_departments.py in1.geojson in2.geojson ... out.geojson`). |
| `france_status_loop.py` | Boucle d'affichage périodique de l'avancement du run France entière (lit `france_progress.json` + `france_build.log`). |
| `rge_alti_catalog.json` | Catalogue département → URL de téléchargement RGE ALTI (IGN). |

Voir [RESUME.md](RESUME.md) pour l'état d'avancement du run France entière
et la commande exacte pour le reprendre.

### `frontend/`

| Fichier | Rôle |
|---|---|
| `interactive_departement.html` | Carte principale (MapLibre GL JS + PMTiles). Survol/clic sur une côte, profil altimétrique coloré par pente, sélection d'une zone par rectangle (limitée au niveau département pour rester fiable — cf le filtrage tippecanoe ci-dessous). C'est ce fichier qui est copié vers `firebase_deploy/public/index.html` au déploiement. |
| `range_server.py` | Serveur HTTP statique minimal supportant les requêtes Range, nécessaire pour tester les PMTiles en local (`http.server` standard ne les supporte pas). |

Les fichiers volumineux générés dans ce dossier (`climbs_*.geojson`,
`*.mbtiles`, `*.pmtiles`, `by_department/`) ne sont **pas** versionnés
(voir `.gitignore`) — ils se régénèrent via le pipeline.

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

python3 pipeline/build_france.py 2>&1 | tee -a france_build.log
python3 pipeline/merge_departments.py frontend/by_department/climbs_D*.geojson frontend/climbs_france.geojson

cd frontend
tippecanoe -o climbs_france.mbtiles -zg --extend-zooms-if-still-dropping --drop-densest-as-needed -l climbs -f climbs_france.geojson --force
pmtiles convert climbs_france.mbtiles climbs_france.pmtiles --force
```

Nécessite en plus `7zz` (extraction des archives RGE ALTI), `tippecanoe`
et `pmtiles` (CLI) sur le PATH.
