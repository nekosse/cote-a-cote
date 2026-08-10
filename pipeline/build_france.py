#!/usr/bin/env python3
"""Pipeline complete, departement par departement, pour toute la France
metropolitaine (hors Corse pour l'instant - catalogue RGE ALTI 5M sous des
codes non numeriques 02A/02B a gerer a part).

Pour chaque departement, dans l'ordre :
  1. Telecharge le MNT RGE ALTI 5m (.7z) depuis le catalogue IGN
  2. L'extrait et fusionne les dalles .asc en un seul GeoTIFF
  3. Supprime IMMEDIATEMENT le .7z et les dalles brutes (le poste ne peut
     pas garder 96 x ~2.5 Go d'intermediaires en meme temps - 66 Go
     disponibles au lancement)
  4. Recupere les troncons OSM sur l'EMPRISE DU MNT (pas par nom via
     Nominatim - cf le bug Savoie/Haute-Savoie ou "Savoie, France" resout
     vers le mauvais departement : l'emprise du MNT est la seule source
     fiable)
  5. Calcule les cotes (reutilise slope_map.py sans le modifier) et ecrit
     un petit GeoJSON par departement (climbs_D0XX.geojson)
  6. Supprime le GeoTIFF et le GeoPackage OSM du departement (seul le
     GeoJSON des cotes, tres petit, est conserve durablement)

Reprise : un fichier d'etat (france_progress.json) marque chaque
departement "done" des que son GeoJSON est ecrit - relancer le script
saute les departements deja faits.

Un department qui echoue (erreur reseau, MNT indisponible...) est logge
et saute plutot que de bloquer tout le run - cf france_errors.log."""
import json
import os
import subprocess
import sys
import time
import traceback

import requests

BASE = "/Users/leo/dev/tours-slope-map"
CATALOG_PATH = f"{BASE}/pipeline/rge_alti_catalog.json"
PROGRESS_PATH = f"{BASE}/france_progress.json"
ERROR_LOG = f"{BASE}/france_errors.log"
CLIMBS_DIR = f"{BASE}/frontend/by_department"
WORK_DIR = f"{BASE}/_work_france"
DELAY_BETWEEN_DEPTS = 5.0  # courtoisie envers l'API Overpass publique, partagee

os.makedirs(CLIMBS_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)

DEPT_NAMES = {
    "001": "Ain", "002": "Aisne", "003": "Allier", "004": "Alpes-de-Haute-Provence",
    "005": "Hautes-Alpes", "006": "Alpes-Maritimes", "007": "Ardèche", "008": "Ardennes",
    "009": "Ariège", "010": "Aube", "011": "Aude", "012": "Aveyron", "013": "Bouches-du-Rhône",
    "014": "Calvados", "015": "Cantal", "016": "Charente", "017": "Charente-Maritime",
    "018": "Cher", "019": "Corrèze", "021": "Côte-d'Or", "022": "Côtes-d'Armor",
    "023": "Creuse", "024": "Dordogne", "025": "Doubs", "026": "Drôme", "027": "Eure",
    "028": "Eure-et-Loir", "029": "Finistère", "030": "Gard", "031": "Haute-Garonne",
    "032": "Gers", "033": "Gironde", "034": "Hérault", "035": "Ille-et-Vilaine",
    "036": "Indre", "037": "Indre-et-Loire", "038": "Isère", "039": "Jura", "040": "Landes",
    "041": "Loir-et-Cher", "042": "Loire", "043": "Haute-Loire", "044": "Loire-Atlantique",
    "045": "Loiret", "046": "Lot", "047": "Lot-et-Garonne", "048": "Lozère",
    "049": "Maine-et-Loire", "050": "Manche", "051": "Marne", "052": "Haute-Marne",
    "053": "Mayenne", "054": "Meurthe-et-Moselle", "055": "Meuse", "056": "Morbihan",
    "057": "Moselle", "058": "Nièvre", "059": "Nord", "060": "Oise", "061": "Orne",
    "062": "Pas-de-Calais", "063": "Puy-de-Dôme", "064": "Pyrénées-Atlantiques",
    "065": "Hautes-Pyrénées", "066": "Pyrénées-Orientales", "067": "Bas-Rhin",
    "068": "Haut-Rhin", "069": "Rhône", "070": "Haute-Saône", "071": "Saône-et-Loire",
    "072": "Sarthe", "073": "Savoie", "074": "Haute-Savoie", "075": "Paris",
    "076": "Seine-Maritime", "077": "Seine-et-Marne", "078": "Yvelines", "079": "Deux-Sèvres",
    "080": "Somme", "081": "Tarn", "082": "Tarn-et-Garonne", "083": "Var", "084": "Vaucluse",
    "085": "Vendée", "086": "Vienne", "087": "Haute-Vienne", "088": "Vosges", "089": "Yonne",
    "090": "Territoire de Belfort", "091": "Essonne", "092": "Hauts-de-Seine",
    "093": "Seine-Saint-Denis", "094": "Val-de-Marne", "095": "Val-d'Oise",
}


def load_progress():
    if os.path.exists(PROGRESS_PATH):
        return json.load(open(PROGRESS_PATH))
    return {}


def save_progress(progress):
    json.dump(progress, open(PROGRESS_PATH, "w"), indent=1)


def log_error(dept, msg):
    with open(ERROR_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} D{dept} ({DEPT_NAMES.get(dept, '?')}): {msg}\n")
    print(f"ERREUR D{dept}: {msg}", flush=True)


def download(url, out_path, max_tries=3):
    for attempt in range(max_tries):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            return
        except Exception as e:
            if attempt == max_tries - 1:
                raise
            print(f"  telechargement echoue ({e}), retry {attempt + 1}/{max_tries}...", flush=True)
            time.sleep(10)


def process_department(dept, entry):
    t0 = time.time()
    print(f"\n=== D{dept} {DEPT_NAMES.get(dept, '?')} ===", flush=True)

    dept_work = f"{WORK_DIR}/{dept}"
    os.makedirs(dept_work, exist_ok=True)
    sevenz_path = f"{dept_work}/dem.7z"
    extract_dir = f"{dept_work}/extracted"
    dem_tif = f"{dept_work}/dem.tif"
    osm_gpkg = f"{dept_work}/osm.gpkg"
    out_geojson = f"{CLIMBS_DIR}/climbs_D{dept}.geojson"

    print(f"Telechargement MNT...", flush=True)
    download(entry["url"], sevenz_path)

    print(f"Extraction...", flush=True)
    subprocess.run(["7zz", "x", sevenz_path, f"-o{extract_dir}", "-y"],
                    check=True, capture_output=True)
    os.remove(sevenz_path)

    asc_files = subprocess.run(
        ["find", extract_dir, "-iname", "*.asc"], capture_output=True, text=True, check=True
    ).stdout.strip().split("\n")
    asc_files = [f for f in asc_files if f]
    if not asc_files:
        raise RuntimeError("aucune dalle .asc trouvee dans l'archive")
    print(f"{len(asc_files)} dalles", flush=True)

    tiles_glob = os.path.dirname(asc_files[0]) + "/*.asc"
    subprocess.run(
        [sys.executable, f"{BASE}/pipeline/build_dem_mosaic.py", tiles_glob, dem_tif],
        check=True, capture_output=True,
    )
    subprocess.run(["rm", "-rf", extract_dir], check=True)

    print("Recuperation OSM sur l'emprise du MNT...", flush=True)
    subprocess.run(
        [sys.executable, f"{BASE}/pipeline/fetch_osm_bbox.py", dem_tif, osm_gpkg],
        check=True, capture_output=True, timeout=1200,
    )

    print("Calcul des cotes...", flush=True)
    subprocess.run(
        [sys.executable, f"{BASE}/pipeline/build_department.py", osm_gpkg, dem_tif, out_geojson],
        check=True, capture_output=True, timeout=1800,
    )

    subprocess.run(["rm", "-rf", dept_work], check=True)

    n = len(json.load(open(out_geojson))["features"])
    print(f"D{dept} termine en {time.time() - t0:.0f}s : {n} cotes -> {out_geojson}", flush=True)
    return n


def main():
    catalog = json.load(open(CATALOG_PATH))
    progress = load_progress()

    dept_codes = sorted(catalog.keys())
    remaining = [d for d in dept_codes if progress.get(d, {}).get("status") != "done"]
    print(f"{len(dept_codes)} departements au catalogue, {len(remaining)} restants", flush=True)

    for i, dept in enumerate(remaining):
        print(f"\n[{i + 1}/{len(remaining)}] --------------------------------", flush=True)
        try:
            n = process_department(dept, catalog[dept])
            progress[dept] = {"status": "done", "n_climbs": n}
        except Exception as e:
            progress[dept] = {"status": "error", "error": str(e)}
            log_error(dept, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            subprocess.run(["rm", "-rf", f"{WORK_DIR}/{dept}"])
        save_progress(progress)
        time.sleep(DELAY_BETWEEN_DEPTS)

    done = [d for d in progress if progress[d].get("status") == "done"]
    errors = [d for d in progress if progress[d].get("status") == "error"]
    print(f"\n=== TERMINE : {len(done)} ok, {len(errors)} en erreur ===", flush=True)
    if errors:
        print("Departements en erreur:", errors, flush=True)


if __name__ == "__main__":
    main()
