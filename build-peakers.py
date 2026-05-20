#!/usr/bin/env python3
"""Build data/peakers.json — New York City's peaking power plants, located.

Source: U.S. EIA Form 860M ("Preliminary Monthly Electric Generator Inventory"),
the authoritative federal inventory of every operating generator, with location,
prime mover and capacity. Keyless public download.

"Peaker" is defined here, verifiably, as an in-city generator whose PRIME MOVER is:
  GT = simple-cycle combustion (gas) turbine
  IC = internal-combustion (reciprocating) engine
These are the unit types built to run only during peak demand — the same fleet
NYC environmental-justice studies track. Combined-cycle (CA/CT/CS) and steam (ST)
units are excluded because they are not peaking units.

Run occasionally (the fleet changes ~annually); commit the resulting JSON.
"""

import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

import openpyxl

NYC_COUNTIES = {"New York": "Manhattan", "Kings": "Brooklyn", "Queens": "Queens",
                "Bronx": "Bronx", "Richmond": "Staten Island"}
PEAKER_PRIME_MOVERS = {"GT", "IC"}
# Grid-serving sectors only. Excludes the many Commercial/Industrial backup and
# CHP engines (hospitals, universities) that EIA also inventories but which are
# not grid peaking plants.
GRID_SECTORS = {"Electric Utility", "IPP Non-CHP"}
ENERGY_SOURCE_LABELS = {"NG": "natural gas", "DFO": "distillate fuel oil",
                        "KER": "kerosene", "RFO": "residual fuel oil",
                        "FO2": "fuel oil", "JF": "jet fuel"}
PAGE = "https://www.eia.gov/electricity/data/eia860m/"


def candidate_860m_urls():
    """Return current (non-archive) 860M generator file URLs, newest first.
    EIA lists future months that don't exist yet, so callers must try in order."""
    html = urllib.request.urlopen(PAGE).read().decode("utf-8", "ignore")
    links = re.findall(r'href="(/electricity/data/eia860m/xls/[a-z]+_generator\d{4}\.xlsx)"', html, re.I)
    months = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)}
    def key(u):
        m = re.search(r'/([a-z]+)_generator(\d{4})\.xlsx', u, re.I)
        return (int(m.group(2)), months.get(m.group(1).lower(), 0))
    return ["https://www.eia.gov" + u for u in sorted(set(links), key=key, reverse=True)]


def load_latest_860m():
    """Try candidate files newest-first; return (workbook, filename, url) for the first real xlsx."""
    for url in candidate_860m_urls():
        try:
            data = urllib.request.urlopen(url).read()
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            return wb, url.split("/")[-1], url
        except (zipfile.BadZipFile, Exception):
            continue
    raise RuntimeError("No valid EIA-860M file found")


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    wb, fname, url = load_latest_860m()
    print(f"Source: {fname}")
    ws = wb["Operating"]

    rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[2]]
    idx = {name: i for i, name in enumerate(header)}

    def col(r, name):
        return r[idx[name]] if name in idx and idx[name] < len(r) else None

    plants = {}
    for r in rows[3:]:
        if not r or str(col(r, "Plant State")).strip() != "NY":
            continue
        county = (col(r, "County") or "").strip()
        if county not in NYC_COUNTIES:
            continue
        pm = (col(r, "Prime Mover Code") or "").strip()
        if pm not in PEAKER_PRIME_MOVERS:
            continue
        if str(col(r, "Sector") or "").strip() not in GRID_SECTORS:
            continue
        pid = col(r, "Plant ID")
        cap = fnum(col(r, "Net Summer Capacity (MW)")) or 0.0
        nameplate = fnum(col(r, "Nameplate Capacity (MW)")) or 0.0
        src = (col(r, "Energy Source Code") or "").strip()
        oyear = col(r, "Operating Year")
        lat, lon = fnum(col(r, "Latitude")), fnum(col(r, "Longitude"))
        p = plants.setdefault(pid, {
            "plant": (col(r, "Plant Name") or "").strip(),
            "borough": NYC_COUNTIES[county], "county": county,
            "lat": lat, "lon": lon, "units": 0,
            "netSummerMW": 0.0, "nameplateMW": 0.0,
            "primeMovers": set(), "fuels": set(), "minYear": None,
        })
        p["units"] += 1
        p["netSummerMW"] += cap
        p["nameplateMW"] += nameplate
        p["primeMovers"].add(pm)
        if src:
            p["fuels"].add(ENERGY_SOURCE_LABELS.get(src, src))
        if lat and not p["lat"]:
            p["lat"], p["lon"] = lat, lon
        yr = fnum(oyear)
        if yr and (p["minYear"] is None or yr < p["minYear"]):
            p["minYear"] = int(yr)

    out_plants = []
    for p in plants.values():
        p["primeMovers"] = sorted(p["primeMovers"])
        p["fuels"] = sorted(p["fuels"])
        p["netSummerMW"] = round(p["netSummerMW"], 1)
        p["nameplateMW"] = round(p["nameplateMW"], 1)
        out_plants.append(p)
    out_plants.sort(key=lambda p: -p["netSummerMW"])

    out = {
        "source": f"U.S. EIA Form 860M ({fname})",
        "sourceUrl": url,
        "definition": "Grid-serving generators in the five New York City counties whose prime mover is GT (simple-cycle combustion turbine) or IC (internal-combustion engine), limited to the Electric Utility and merchant IPP sectors. Excludes commercial/industrial backup and cogeneration engines (e.g. hospitals, universities) that are not grid peaking plants.",
        "primeMoverCodes": {"GT": "simple-cycle combustion turbine", "IC": "internal-combustion engine"},
        "totalPlants": len(out_plants),
        "totalNetSummerMW": round(sum(p["netSummerMW"] for p in out_plants), 1),
        "plants": out_plants,
    }
    out_path = Path(__file__).parent / "data" / "peakers.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out))
    print(f"Wrote {out_path}")
    print(f"  {out['totalPlants']} NYC peaker plants, {out['totalNetSummerMW']} MW net summer total")
    for p in out_plants[:12]:
        print(f"    {p['plant'][:34]:34} {p['borough']:13} {p['netSummerMW']:7.1f} MW  {'/'.join(p['primeMovers'])}  {','.join(p['fuels'])}")


if __name__ == "__main__":
    sys.exit(main())
