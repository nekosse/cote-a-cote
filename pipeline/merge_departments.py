#!/usr/bin/env python3
"""Fusionne plusieurs GeoJSON de cotes (un par departement) en un seul
FeatureCollection, avec des idx globalement uniques (0..N-1)."""
import json
import sys

inputs = sys.argv[1:-1]
out_path = sys.argv[-1]

features = []
for path in inputs:
    d = json.load(open(path, encoding="utf-8"))
    features.extend(d["features"])
    print(f"{path}: {len(d['features'])} cotes")

for i, f in enumerate(features):
    f["properties"]["idx"] = i

out = {"type": "FeatureCollection", "features": features}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f)
print(f"Ecrit {out_path}: {len(features)} cotes au total")
