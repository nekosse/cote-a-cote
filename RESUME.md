# Etat du pipeline France entiere - pause du 2026-08-10 02:15

## Ou on en est

- **80/102 departements traites** (metropole + DOM-TOM inclus dans le
  catalogue RGE ALTI), **286 897 cotes** cumulees dans
  `pmtiles_poc/by_department/*.geojson`
- **9 en erreur** (timeout Overpass ou crash mosaique), a reessayer :
  `001 038 054 057 059 062 067 077 089`
  (Ain, Isere, Meurthe-et-Moselle, Moselle, Nord, Pas-de-Calais, Bas-Rhin,
  Seine-et-Marne, Yonne)
- **13 jamais tentes** : `091 092 093 094 095` (Essonne, Hauts-de-Seine,
  Seine-Saint-Denis, Val-de-Marne, Val-d'Oise) + `971 972 973 974 975 976
  977 978` (DOM-TOM : Guadeloupe, Martinique, Guyane, Reunion,
  Saint-Pierre-et-Miquelon, Mayotte, Saint-Barthelemy, Saint-Martin - ils
  sont dans le catalogue RGE ALTI, jamais evoque explicitement avec
  l'utilisateur si on les veut, a confirmer avant de les lancer)
- Corse (02A/02B) : **pas dans le catalogue RGE ALTI 5m** telecharge, a
  traiter a part si voulu (chercher un autre produit/format).

## Pour reprendre

```bash
cd /Users/leo/dev/tours-slope-map && source venv/bin/activate
python3 build_france.py 2>&1 | tee -a france_build.log
```

Le script lit `france_progress.json` et saute tout ce qui a
`status: done` - il ne refait que les 9 erreurs + les 13 jamais tentes
(~22 departements restants, a ce rythme ~2-3h).

Si on veut exclure les DOM-TOM pour l'instant, retirer les cles `971` a
`978` de `rge_alti_catalog.json` avant de relancer (ou les ignorer dans
`build_france.py`, `remaining = [d for d in dept_codes if not
d.startswith('9') and ...]`).

## Pour finaliser une fois tout fait (ou pour tester avec ce qu'on a deja)

```bash
# fusionner tous les geojson par departement en un seul fichier
python3 merge_departments.py pmtiles_poc/by_department/climbs_D*.geojson pmtiles_poc/climbs_france.geojson

# tuiles vectorielles + PMTiles
cd pmtiles_poc
tippecanoe -o climbs_france.mbtiles -zg --extend-zooms-if-still-dropping -l climbs -f climbs_france.geojson --force
pmtiles convert climbs_france.mbtiles climbs_france.pmtiles --force

# mettre a jour interactive_departement.html : pointer vers climbs_france.pmtiles,
# mettre a jour N_CLIMBS (compter les features du geojson fusionne)

# copier + deployer
cp interactive_departement.html ../firebase_deploy/public/index.html
cp climbs_france.pmtiles ../firebase_deploy/public/
rm -f ../firebase_deploy/public/climbs_all.pmtiles  # ancien fichier
cd ../firebase_deploy && firebase deploy --only hosting:app
```

## A savoir / pieges deja rencontres

- **Nominatim resout "Savoie, France" vers la Haute-Savoie** ("Upper
  Savoy") - c'est pour ca que `fetch_osm_bbox.py` utilise l'emprise du
  MNT deja telecharge plutot qu'un geocodage par nom. Ne PAS revenir a
  `ox.geocode_to_gdf(nom_departement)`.
- **Cache osmnx desactive** (`ox.settings.use_cache = False` dans
  `fetch_osm_bbox.py`) - sinon `cache/` grossit sans limite (6+ Go vus
  apres 25 departements).
- **Disque limite** (66 Go libres au debut de la journee, a verifier avant
  de relancer avec `df -h`) : chaque departement nettoie ses fichiers
  intermediaires (.7z, .asc, .tif, .gpkg) juste apres usage - ne garder
  que les petits `climbs_D0XX.geojson` (quelques Mo chacun).
- **Le fichier `france_progress.json` n'est PAS relu par le process en
  cours** : ne pas l'editer a la main pendant qu'un run tourne, ca se
  fait ecraser au prochain `save_progress()`. Editer seulement quand rien
  ne tourne (comme maintenant).
- Timeouts Overpass (~1200s) ponctuels sur des gros departements (Isere,
  Nord, Pas-de-Calais...) - pas une panne generale d'Overpass (verifie
  via `curl https://overpass-api.de/api/status`), juste des requetes
  lentes sur des zones denses. Le retry naturel (relancer le script) a
  suffi jusqu'ici.
