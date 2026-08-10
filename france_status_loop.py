#!/usr/bin/env python3
"""Emet une ligne de statut toutes les 45s pour le suivi live du pipeline
France entiere (utilise par le Monitor - une ligne stdout = une notif)."""
import json
import os
import re
import time

BASE = "/Users/leo/dev/tours-slope-map"
PROGRESS_PATH = f"{BASE}/france_progress.json"
LOG_PATH = f"{BASE}/france_build.log"
TOTAL = 102
START = time.time()

while True:
    time.sleep(900)
    try:
        progress = json.load(open(PROGRESS_PATH)) if os.path.exists(PROGRESS_PATH) else {}
    except (json.JSONDecodeError, OSError):
        progress = {}
    done = sum(1 for v in progress.values() if v.get("status") == "done")
    err = sum(1 for v in progress.values() if v.get("status") == "error")
    n_climbs = sum(v.get("n_climbs", 0) for v in progress.values() if v.get("status") == "done")

    current_dept, last_line = "?", ""
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, errors="ignore") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                last_line = line
                break
        for line in reversed(lines):
            m = re.match(r"=== (D\d+ .+) ===", line)
            if m:
                current_dept = m.group(1)
                break

    elapsed_min = (time.time() - START) / 60
    rate = done / elapsed_min if elapsed_min > 0 and done > 0 else 0
    eta = f"{(TOTAL - done - err) / rate:.0f}min" if rate > 0 else "?"
    print(
        f"[{time.strftime('%H:%M:%S')}] {done}/{TOTAL} ok, {err} erreurs, "
        f"{n_climbs} cotes cumulees | en cours: {current_dept} | \"{last_line[:70]}\" | "
        f"ecoule {elapsed_min:.0f}min, ETA ~{eta}",
        flush=True,
    )
