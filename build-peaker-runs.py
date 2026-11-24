#!/usr/bin/env python3
"""Build data/peaker-runs.json: how much each New York City peaking plant actually ran.

data/peakers.json (build-peakers.py) says where the 14 grid-serving peaking plants are. This script
adds what they did, per plant, for 2023, 2024, 2025 and 2026 year-to-date:

  Source 1: U.S. EPA Clean Air Markets Program Data (CAMPD) API, https://api.epa.gov/easey/
            - facilities-mgmt/facilities/attributes   unit list, unit type, programs, owner, and the
                                                      EIA generator IDs each unit drives
            - streaming-services/emissions/apportioned/quarterly   per-unit, per-quarter operating
                                                      time, gross load, heat input, CO2 and NOx
            - streaming-services/emissions/apportioned/daily       per-unit daily rows for 2025,
                                                      used only to count days any unit ran
  Source 2: U.S. EIA Form 860M generator inventory (the same workbook build-peakers.py used, for
            plant and generator IDs; the newest published workbook, for status and retirements).

Plant IDs: EIA plant IDs are the same numbers as EPA ORIS codes, so each peakers.json plant is
           re-derived from its 860M file with build-peakers.py's exact filter (NYC county, prime
           mover GT/IC, sector Electric Utility or IPP Non-CHP), keyed by plant name.
Unit match: a CAMPD unit belongs to a plant when it sits at that ORIS code and one of the EIA
           generator IDs CAMPD lists for it ("associatedGeneratorsAndNameplateCapacity") is one of the
           plant's peaker generators. Fallbacks, each recorded in the plant's "match" field:
             - generator IDs at the ORIS code never line up (EPA and EIA spell them differently):
               take the facility's combustion-turbine units;
             - the ORIS code has no CAMPD units at all: take CAMPD units elsewhere whose listed
               owner/operator is the plant's 860M owner and whose generator ID matches.
           Boilers and steam units at the same site (East River, 59th Street, 74th Street, Astoria)
           are never counted.
Not covered: a plant with no matching CAMPD unit is still written out, with covered: false.

API key:   reads DATA_GOV_API_KEY; defaults to the public DEMO_KEY (about 10 requests an hour from
           EPA's gateway). A full run needs 3 requests. Every raw response is cached in
           .cache/peaker-runs/ (gitignored) and reused, so re-runs cost nothing. Delete a cache file
           (or pass --refresh) to fetch it again; 2026 quarterly data is refetched automatically
           when the cached copy is more than 7 days old so new quarters get picked up.

Peaker Rule notes: the per-plant "peakerRule" text is the only hand-entered content. Each note
           carries the primary-source URL (NYISO or NY DEC) and a short verbatim quote. No numbers
           in it are used in any calculation.

Re-run:    python3 build-peaker-runs.py [--refresh]
"""
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache", "peaker-runs")
os.makedirs(CACHE, exist_ok=True)
API_KEY = os.environ.get("DATA_GOV_API_KEY") or "DEMO_KEY"
REFRESH = "--refresh" in sys.argv
EASEY = "https://api.epa.gov/easey"
YEARS = [2023, 2024, 2025, 2026]
DAILY_YEAR = 2025
EIA_PAGE = "https://www.eia.gov/electricity/data/eia860m/"
UA = {"User-Agent": "nyc-grid-pulse/peaker-runs"}

# Same filter as build-peakers.py
NYC_COUNTIES = {"New York", "Kings", "Queens", "Bronx", "Richmond"}
PEAKER_PRIME_MOVERS = {"GT", "IC"}
GRID_SECTORS = {"Electric Utility", "IPP Non-CHP"}
CT_TYPES = {"Combustion turbine", "Combined cycle", "Simple cycle combustion turbine"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


# ---------------------------------------------------------------------------------------------
# HTTP with cache
# ---------------------------------------------------------------------------------------------
def http_get(url, binary=False, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                raise RuntimeError(
                    "CAMPD rate limit hit (HTTP 429). DEMO_KEY allows only a few requests an hour; "
                    "wait and re-run (cached responses are kept), or set DATA_GOV_API_KEY.") from e
            if e.code in (400, 401, 403, 404):
                raise
        except Exception as e:  # network hiccup
            last = e
        time.sleep(5 * (i + 1))
    raise last


def campd(path, params, max_age_days=None):
    """GET an EASEY endpoint; cache the parsed JSON keyed on the URL without the api key."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())  # CAMPD wants literal '|' separators
    key = hashlib.sha1(f"{path}?{qs}".encode()).hexdigest()[:16]
    fpath = os.path.join(CACHE, f"campd_{path.strip('/').replace('/', '_')}_{key}.json")
    if os.path.exists(fpath) and not REFRESH:
        age = (time.time() - os.path.getmtime(fpath)) / 86400
        if max_age_days is None or age <= max_age_days:
            with open(fpath) as f:
                return json.load(f)["data"]
    url = f"{EASEY}{path}?api_key={urllib.parse.quote(API_KEY)}&{qs}"
    print(f"  fetching {path} ({qs[:90]}...)")
    data = json.loads(http_get(url))
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected CAMPD response for {path}: {str(data)[:300]}")
    with open(fpath, "w") as f:
        json.dump({"url": f"{EASEY}{path}?{qs}", "fetchedAt": now_iso(), "data": data}, f)
    return data


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------
# EIA 860M
# ---------------------------------------------------------------------------------------------
def eia_file_urls():
    """All 860M generator workbooks EIA links (current and archive), newest first."""
    html = http_get(EIA_PAGE)
    links = set(re.findall(r'href="(/electricity/data/eia860m/(?:archive/)?xls/([a-z]+)_generator(\d{4})\.xlsx)"', html, re.I))
    def key(t):
        return (int(t[2]), MONTHS.index(t[1].lower()) + 1 if t[1].lower() in MONTHS else 0)
    return [("https://www.eia.gov" + t[0], f"{t[1].lower()}_generator{t[2]}.xlsx") for t in sorted(links, key=key, reverse=True)]


def load_860m(fname, url_candidates):
    path = os.path.join(CACHE, fname)
    if not os.path.exists(path):
        for url in url_candidates:
            try:
                data = http_get(url, binary=True)
            except Exception:
                continue
            if data[:2] == b"PK":
                with open(path, "wb") as f:
                    f.write(data)
                break
    if not os.path.exists(path):
        return None
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


def sheet_rows(wb, name):
    rows = list(wb[name].iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[2]]
    idx = {h: i for i, h in enumerate(header)}
    out = []
    for r in rows[3:]:
        if not r or r[idx["Plant ID"]] in (None, ""):
            continue
        out.append({h: (r[i].strip() if isinstance(r[i], str) else r[i]) for h, i in idx.items() if i < len(r)})
    return out


def is_nyc_peaker(r, check_sector=True):
    return (str(r.get("Plant State")) == "NY" and (r.get("County") or "") in NYC_COUNTIES
            and (r.get("Prime Mover Code") or "") in PEAKER_PRIME_MOVERS
            and (not check_sector or str(r.get("Sector") or "") in GRID_SECTORS))


def ym(y, m):
    try:
        y = int(y)
    except (TypeError, ValueError):
        return None
    try:
        return f"{y}-{int(m):02d}"
    except (TypeError, ValueError):
        return str(y)


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Sourced Peaker Rule / NYISO notes (hand-entered text; every note has a primary-source URL and quote)
# Keyed by EIA plant ID / ORIS code.
# ---------------------------------------------------------------------------------------------
STAR_2026Q1 = "https://www.nyiso.com/documents/20142/39103148/2026-Q1-STAR-Report-Final.pdf/0f53b499-360c-29a8-09f7-6fd34a05bcf9"
STAR_2025Q3 = "https://www.nyiso.com/documents/20142/39103148/2025-Q3-STAR-Report-Final.pdf"
NARROWS_PRR = "https://extapps.dec.ny.gov/data/dar/afs/permits/prr_261020008600011_r4.pdf"
GOWANUS_NARROWS = [
    {"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1 (April 15, 2026)", "url": STAR_2026Q1,
     "quote": "designating the Gowanus 2 & 3 and Narrows 1 & 2 generators as needed to address ongoing reliability needs until May 1, 2029"},
    {"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1 (April 15, 2026)", "url": STAR_2026Q1,
     "quote": "AlphaGen, the owner, has proposed to withdraw the generator deactivation notices for these generators."},
    {"source": "NYISO, Short-Term Assessment of Reliability: 2025 Quarter 3 (October 2025)", "url": STAR_2025Q3,
     "quote": "designation of the Gowanus 2 & 3 and Narrows 1 & 2 generators under the DEC Peaker Rule in 2023 has allowed their continued operation beyond May 1, 2025"},
]
PEAKER_RULE = {
    2494: {
        "summary": "Subject to the DEC Peaker Rule (6 NYCRR Subpart 227-3). NYISO lists Gowanus 1-1 through 1-8 and 4-1 through 4-8 (the oil-fired units) as retired Nov. 1, 2022; Gowanus 2 and 3 are designated by NYISO as needed for New York City reliability until May 1, 2029.",
        "sources": [
            {"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1, Figure 2 'Status Changes Due to DEC Peaker Rule'", "url": STAR_2026Q1,
             "quote": "Gowanus 1-1 through 1-8 ... 11/1/2022 (R) ... Gowanus 4-1 through 4-8 ... 11/1/2022 (R)"},
        ] + GOWANUS_NARROWS,
    },
    2499: {
        "summary": "Subject to the DEC Peaker Rule. All 16 Narrows turbines (Narrows 1 and 2) are designated by NYISO as needed for New York City reliability until May 1, 2029; DEC's permit review says the site stopped burning oil in the ozone season from May 1, 2023.",
        "sources": GOWANUS_NARROWS + [
            {"source": "NY DEC, Permit Review Report, Narrows Generating Station, Permit 2-6102-00086/00011, Renewal 4 (04/03/2026)", "url": NARROWS_PRR,
             "quote": "Since May 1, 2023, The facility stopped operation of its combustion turbines burning oil fuel during the ozone season."},
            {"source": "NY DEC, Permit Review Report, Narrows Generating Station (04/03/2026)", "url": NARROWS_PRR,
             "quote": "Beginning May 1, 2025, the facility is operating under the New York Independent System Operator (NYISO) reliability determination."},
        ],
    },
    2504: {
        "summary": "Listed in NYISO's table of status changes due to the DEC Peaker Rule (74 St. GT 1 & 2, status date May 1, 2023), with the note that the units are no longer dispatched by NYISO and serve local reliability only.",
        "sources": [{"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1, Figure 2", "url": STAR_2026Q1,
                     "quote": "74 St. GT 1 & 2 (10) ... 05/01/2023; note 10: Unit no longer subject to NYISO dispatch and is used for local reliability only."}],
    },
    2503: {
        "summary": "Listed in NYISO's table of status changes due to the DEC Peaker Rule (59 St. GT 1, status date May 1, 2025), with the note that the unit is no longer dispatched by NYISO and serves local reliability only.",
        "sources": [{"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1, Figure 2", "url": STAR_2026Q1,
                     "quote": "59 St. GT 1 (10) ... 05/01/2025; note 10: Unit no longer subject to NYISO dispatch and is used for local reliability only."}],
    },
    8906: {
        "summary": "Listed in NYISO's table of status changes due to the DEC Peaker Rule as retired May 1, 2025 (Astoria GT 01), although EIA's 860M still carries the generator as out of service rather than retired.",
        "sources": [{"source": "NYISO, Short-Term Assessment of Reliability: 2026 Quarter 1, Figure 2", "url": STAR_2026Q1,
                     "quote": "Astoria GT 01 ... 5/1/2025 (R)"}],
    },
}


# ---------------------------------------------------------------------------------------------
def main():
    generated = now_iso()
    peakers = json.load(open(os.path.join(ROOT, "data", "peakers.json")))
    ids_url = peakers["sourceUrl"]
    ids_fname = ids_url.rsplit("/", 1)[-1]
    eia_files = eia_file_urls()
    by_name = {f: u for u, f in eia_files}

    # ---- 1. plant + generator IDs from the same 860M workbook peakers.json was built from ----
    cands = [ids_url] + ([by_name[ids_fname]] if ids_fname in by_name else [])
    wb_ids = load_860m(ids_fname, cands)
    if wb_ids is None:
        raise RuntimeError(f"Could not download {ids_fname}")
    ids_rows = [r for r in sheet_rows(wb_ids, "Operating") if is_nyc_peaker(r)]
    gens_by_name = defaultdict(list)
    for r in ids_rows:
        gens_by_name[r["Plant Name"]].append(r)

    # ---- 2. newest 860M workbook for status / retirement ----
    wb_latest, latest_fname, latest_url = None, None, None
    for url, fname in eia_files:
        wb_latest = load_860m(fname, [url])
        if wb_latest is not None:
            latest_fname, latest_url = fname, url
            break
    latest_op = {(r["Plant ID"], str(r["Generator ID"])): r for r in sheet_rows(wb_latest, "Operating")}
    latest_ret = defaultdict(list)
    for r in sheet_rows(wb_latest, "Retired"):
        latest_ret[r["Plant ID"]].append(r)
    print(f"860M: IDs from {ids_fname}; status from {latest_fname}")

    plants = []
    for p in peakers["plants"]:
        rows = gens_by_name.get(p["plant"])
        if not rows:
            raise RuntimeError(f"{p['plant']} not found in {ids_fname}")
        pid = int(rows[0]["Plant ID"])
        plants.append({"meta": p, "oris": pid, "entity": rows[0]["Entity Name"],
                       "gens": [str(r["Generator ID"]) for r in rows], "rows": rows})
    oris_list = sorted({x["oris"] for x in plants})
    fac_param = "|".join(str(o) for o in oris_list)

    # ---- 3. CAMPD unit attributes ----
    attrs = campd("/facilities-mgmt/facilities/attributes",
                  {"year": "|".join(map(str, YEARS)), "facilityId": fac_param, "page": 1, "perPage": 500})
    units_by_fac = defaultdict(dict)  # fac -> unitId -> latest attribute row
    unit_years = defaultdict(set)
    for a in sorted(attrs, key=lambda a: a["year"]):
        units_by_fac[a["facilityId"]][a["unitId"]] = a
        unit_years[(a["facilityId"], a["unitId"])].add(a["year"])

    def unit_gens(a):
        s = a.get("associatedGeneratorsAndNameplateCapacity") or ""
        return {g.strip() for g in re.findall(r"([^,()]+?)\s*\(", s)}

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("llc", "").replace("l.p.", ""))

    for pl in plants:
        gens = set(pl["gens"])
        fac_units = units_by_fac.get(pl["oris"], {})
        chosen = [(pl["oris"], u, a) for u, a in fac_units.items()
                  if a.get("unitType") in CT_TYPES and unit_gens(a) & gens]
        method = "EPA unit lists one of the plant's EIA generator IDs, same ORIS code"
        if not chosen and fac_units:
            chosen = [(pl["oris"], u, a) for u, a in fac_units.items() if a.get("unitType") in CT_TYPES]
            if chosen:
                method = ("generator IDs differ between EPA and EIA, so all combustion-turbine units at the same "
                          "ORIS code are used (EIA: " + ", ".join(sorted(gens)) + "; EPA: " +
                          ", ".join(sorted(g for _, _, a in chosen for g in unit_gens(a))) + ")")
        if not chosen:
            ent = norm(pl["entity"])
            for fac, us in units_by_fac.items():
                for u, a in us.items():
                    owners = [norm(o.split("(")[0]) for o in (a.get("ownerOperator") or "").split("|")]
                    if ent and ent in owners and unit_gens(a) & gens and a.get("unitType") in CT_TYPES:
                        chosen.append((fac, u, a))
            if chosen:
                f0 = chosen[0][2]
                method = (f"no EPA units under ORIS {pl['oris']}; EPA reports this plant's generator as unit "
                          + ", ".join(u for _, u, _ in chosen) + f" at ORIS {chosen[0][0]} ({f0['facilityName']}), "
                          f"whose listed owner/operator is {pl['entity']} and whose generator ID matches")
        pl["campd"] = chosen
        pl["match"] = method if chosen else "no EPA CAMPD unit found for this plant"
        matched_gens = set().union(*[unit_gens(a) for _, _, a in chosen]) if chosen else set()
        pl["unmatchedGens"] = sorted(gens - matched_gens) if "differ" not in method else []

    all_facs = sorted({f for pl in plants for f, _, _ in pl["campd"]})
    fac_param2 = "|".join(str(f) for f in all_facs)

    # ---- 4. CAMPD quarterly apportioned emissions, all years in one request ----
    this_year = datetime.date.today().year
    q = campd("/streaming-services/emissions/apportioned/quarterly",
              {"year": "|".join(map(str, YEARS)), "quarter": "1|2|3|4", "facilityId": fac_param2},
              max_age_days=7 if this_year in YEARS else None)
    # ---- 5. CAMPD daily apportioned emissions for DAILY_YEAR (days with any operation) ----
    d = campd("/streaming-services/emissions/apportioned/daily",
              {"beginDate": f"{DAILY_YEAR}-01-01", "endDate": f"{DAILY_YEAR}-12-31", "facilityId": fac_param2})

    def num(v):
        return float(v) if v not in (None, "") else 0.0

    def total(rows, field, ndigits):
        """Sum a field; None when EPA reported no value for it although the units operated."""
        vals = [float(r[field]) for r in rows if r.get(field) not in (None, "")]
        if not vals:
            return 0 if sum(num(r["sumOpTime"]) for r in rows) == 0 else None
        return round(sum(vals), ndigits) if ndigits else round(sum(vals))

    qrows = defaultdict(list)  # (fac, unit) -> rows
    for r in q:
        qrows[(r["facilityId"], r["unitId"])].append(r)
    drows = defaultdict(list)
    for r in d:
        drows[(r["facilityId"], r["unitId"])].append(r)
    published = defaultdict(set)
    for r in q:
        published[int(r["year"])].add(int(r["quarter"]))
    latest_year = max(published) if published else None
    ytd_q = max(published[latest_year]) if latest_year else None

    out_plants = []
    tot = defaultdict(lambda: defaultdict(float))
    tot_sc = defaultdict(lambda: defaultdict(float))
    for pl in plants:
        p = pl["meta"]
        keys = [(f, u) for f, u, _ in pl["campd"]]
        years = {}
        unit_detail = []
        for f, u, a in pl["campd"]:
            per = {}
            for y in YEARS:
                rs = [r for r in qrows[(f, u)] if int(r["year"]) == y]
                if rs:
                    per[str(y)] = {"operatingHours": round(sum(num(r["sumOpTime"]) for r in rs), 2),
                                   "hoursWithAnyOperation": int(sum(num(r["countOpTime"]) for r in rs)),
                                   "grossLoadMWh": round(sum(num(r["grossLoad"]) for r in rs)),
                                   "quarters": sorted(int(r["quarter"]) for r in rs)}
            unit_detail.append({"epaFacilityId": f, "epaUnitId": u, "unitType": a.get("unitType"),
                                "eiaGenerators": sorted(unit_gens(a)), "programs": a.get("programCodeInfo"),
                                "primaryFuel": a.get("primaryFuelInfo"), "secondaryFuel": a.get("secondaryFuelInfo"),
                                "epaOperatingStatus": a.get("operatingStatus"),
                                "inEpaInventoryYears": sorted(unit_years[(f, u)]), "byYear": per})
        for y in YEARS:
            ur = {k: [r for r in qrows[k] if int(r["year"]) == y] for k in keys}
            ur = {k: v for k, v in ur.items() if v}
            if not ur:
                years[str(y)] = None
                continue
            per_unit_hours = {k: sum(num(r["sumOpTime"]) for r in v) for k, v in ur.items()}
            per_unit_count = {k: sum(num(r["countOpTime"]) for r in v) for k, v in ur.items()}
            allr = [r for v in ur.values() for r in v]
            rec = {
                "quartersReported": sorted({int(r["quarter"]) for r in allr}),
                "unitsReporting": len(ur),
                "unitsThatRan": sum(1 for h in per_unit_hours.values() if h > 0),
                "operatingHoursAllUnits": round(sum(per_unit_hours.values()), 2),
                "operatingHoursMaxUnit": round(max(per_unit_hours.values()), 2),
                "hoursWithAnyOperationMaxUnit": int(max(per_unit_count.values())),
                "grossLoadMWh": total(allr, "grossLoad", 0),
                "heatInputMMBtu": total(allr, "heatInput", 0),
                "co2Tons": total(allr, "co2Mass", 1),
                "noxTons": total(allr, "noxMass", 2),
            }
            if y == DAILY_YEAR:
                days = {r["date"] for k in keys for r in drows[k] if num(r.get("sumOpTime")) > 0}
                rec["daysAnyUnitOperated"] = len(days) if any(drows[k] for k in keys) else None
            years[str(y)] = rec
            combined = any(a.get("unitType") == "Combined cycle" for _, _, a in pl["campd"])
            for fld in ("grossLoadMWh", "heatInputMMBtu", "co2Tons", "noxTons", "operatingHoursAllUnits"):
                if rec[fld] is not None:
                    tot[str(y)][fld] += rec[fld]
                    if not combined:
                        tot_sc[str(y)][fld] += rec[fld]

        # ---- retirement / status from 860M ----
        ret = {"eia860mFile": latest_fname, "eia860mUrl": latest_url, "generators": [],
               "plannedRetirements": [], "retiredInLatestFile": [], "outOfService": []}
        for g in pl["gens"]:
            lr = latest_op.get((pl["oris"], g))
            if lr is None:
                st = "not in Operating sheet"
            else:
                st = lr.get("Status")
            pr = ym(lr.get("Planned Retirement Year"), lr.get("Planned Retirement Month")) if lr else None
            ret["generators"].append({"generatorId": g, "status": st,
                                      "netSummerMW": fnum(lr.get("Net Summer Capacity (MW)")) if lr else None,
                                      "plannedRetirement": pr})
            if pr:
                ret["plannedRetirements"].append({"generatorId": g, "plannedRetirement": pr})
            if st and str(st).startswith("(OS)"):
                ret["outOfService"].append(g)
        for r in latest_ret.get(pl["oris"], []):
            if (r.get("Prime Mover Code") or "") in PEAKER_PRIME_MOVERS:
                ret["retiredInLatestFile"].append({"generatorId": str(r["Generator ID"]),
                                                   "retired": ym(r.get("Retirement Year"), r.get("Retirement Month")),
                                                   "netSummerMW": fnum(r.get("Net Summer Capacity (MW)"))})
        moved = [g for g in pl["gens"] if (pl["oris"], g) not in latest_op]
        if moved:
            ret["notInLatestOperatingSheet"] = moved

        # ---- computed notes ----
        notes = []
        types = {a.get("unitType") for _, _, a in pl["campd"]}
        if "Combined cycle" in types:
            notes.append("EPA classifies these units as combined-cycle (sourceCategory: "
                         + ", ".join(sorted({a.get('sourceCategory') or '' for _, _, a in pl['campd']}))
                         + "), not simple-cycle peakers, though EIA records their prime mover as GT.")
        if pl["unmatchedGens"]:
            notes.append("EIA generators with no matching EPA unit: " + ", ".join(pl["unmatchedGens"]) + ".")
        dropped = [(u, sorted(unit_years[(f, u)])) for f, u, _ in pl["campd"]
                   if max(unit_years[(f, u)]) < max(YEARS)]
        if dropped:
            last = max(max(ys) for _, ys in dropped)
            notes.append(f"{len(dropped)} EPA units ({', '.join(u for u, _ in dropped)}) appear in EPA's unit inventory "
                         f"only through {last} and report no emissions after that.")
        reported = [set(years[str(y)]["quartersReported"]) for y in YEARS if years.get(str(y))]
        if reported and all(qs <= {2, 3} for qs in reported):
            progs = sorted({a.get("programCodeInfo") for _, _, a in pl["campd"]})
            notes.append("EPA has records only for quarters 2 and 3 in every year: the unit reports only for the "
                         "May-September ozone season (EPA programs: " + "; ".join(progs) + "), so any running "
                         "outside that window is not counted.")
        missing = sorted({k for y in YEARS for k in ("grossLoadMWh", "heatInputMMBtu", "co2Tons", "noxTons")
                          if years.get(str(y)) and years[str(y)][k] is None})
        if missing:
            notes.append("EPA reports no value for " + ", ".join(missing) + " in some years although the unit "
                         "operated; those fields are null and left out of the totals.")
        cov = bool(pl["campd"])
        if not cov:
            notes.append("Not found in EPA CAMPD: no unit at this ORIS code or owned by this operator reports to an EPA "
                         "emissions program (units below program size thresholds are not required to).")

        out_plants.append({
            "plant": p["plant"], "borough": p["borough"], "orisCode": pl["oris"], "eiaOwner": pl["entity"],
            "covered": cov, "match": pl["match"],
            "units": pl["gens"],
            "epaUnits": unit_detail,
            "years": years,
            "ytdThrough": (f"{max(YEARS)}-Q{max(years[str(max(YEARS))]['quartersReported'])}"
                           if years.get(str(max(YEARS))) else None),
            "retirement": ret,
            "peakerRule": PEAKER_RULE.get(pl["oris"]),
            "notes": notes,
        })

    def rnd(t):
        return {y: {k: round(v, 2 if k == "noxTons" else 1) for k, v in v2.items()} for y, v2 in t.items()}
    totals = rnd(tot)
    totals_sc = rnd(tot_sc)

    out = {
        "generatedAt": generated,
        "ytdThrough": f"{latest_year}-Q{ytd_q}" if latest_year else None,
        "quartersPublished": {str(y): sorted(v) for y, v in sorted(published.items())},
        "sources": {
            "campd": "U.S. EPA Clean Air Markets Program Data API, https://api.epa.gov/easey/ (documentation: https://www.epa.gov/power-sector/cam-api-portal)",
            "campdEndpoints": [
                f"{EASEY}/facilities-mgmt/facilities/attributes",
                f"{EASEY}/streaming-services/emissions/apportioned/quarterly",
                f"{EASEY}/streaming-services/emissions/apportioned/daily",
            ],
            "eia860mIds": {"file": ids_fname, "url": ids_url, "use": "plant (ORIS) and generator IDs, same workbook as data/peakers.json"},
            "eia860mStatus": {"file": latest_fname, "url": latest_url, "use": "generator status, planned and actual retirements"},
            "peakerRule": sorted({s["url"] for v in PEAKER_RULE.values() for s in v["sources"]}),
        },
        "definitions": {
            "orisCode": "EIA plant ID, which is the same number EPA uses as the facility's ORIS code.",
            "covered": "True when at least one EPA CAMPD unit was matched to the plant's peaker generators; false means EPA has no emissions reporting for it.",
            "match": "How the EPA units were tied to the plant's EIA generators.",
            "units": "EIA generator IDs at the plant that pass the peakers.json filter (prime mover GT or IC, grid-serving sector).",
            "epaUnits": "The EPA reporting units counted for the plant, with the EIA generators EPA ties to each and per-unit hours by year.",
            "quartersReported": "Calendar quarters for which EPA has emissions records for any of the plant's units in that year.",
            "unitsReporting": "Number of the plant's EPA units with at least one emissions record that year (a unit can report zero operation).",
            "unitsThatRan": "Number of those units with more than zero operating hours that year.",
            "operatingHoursAllUnits": "EPA operating time (sumOpTime) summed across units; double-counts hours when several units ran at once.",
            "operatingHoursMaxUnit": "Operating hours of the single unit that ran the most that year; the cleaner answer to how many hours the plant ran.",
            "hoursWithAnyOperationMaxUnit": "For the busiest unit, the count of clock hours in which it operated at all, even briefly (EPA countOpTime).",
            "grossLoadMWh": "Gross electrical output in megawatt-hours, summed across units (before the plant's own use).",
            "heatInputMMBtu": "Fuel heat input in million British thermal units, summed across units.",
            "co2Tons": "Carbon dioxide emitted, short tons, summed across units.",
            "noxTons": "Nitrogen oxides emitted, short tons, summed across units.",
            "daysAnyUnitOperated": f"Number of calendar days in {DAILY_YEAR} on which at least one of the plant's units had operating time (from EPA daily data).",
            "ytdThrough": "Latest calendar quarter of the current year that EPA has published for these plants.",
            "retirement": "Generator status from the newest EIA 860M workbook: planned retirement dates, generators on its Retired sheet, and generators marked out of service.",
            "peakerRule": "Short notes on the New York DEC Peaker Rule (6 NYCRR Subpart 227-3) and NYISO reliability designations, each with a primary-source URL and quote; omitted where no primary source was found.",
            "totals": "Sums across all covered plants per year (null values skipped).",
            "totalsExcludingCombinedCycle": "The same sums leaving out plants whose EPA units are combined-cycle (East River), i.e. the simple-cycle peakers only.",
        },
        "totals": totals,
        "totalsExcludingCombinedCycle": totals_sc,
        "plants": out_plants,
    }
    out_path = os.path.join(ROOT, "data", "peaker-runs.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {out_path}")
    print(f"  ytdThrough {out['ytdThrough']}; quarters published {out['quartersPublished']}")
    for p in out_plants:
        y25, y26 = p["years"].get("2025"), p["years"].get("2026")
        f = lambda r, k: (r or {}).get(k, "-")
        print(f"  {p['plant'][:32]:32} cov={str(p['covered'])[0]} 2025 maxH={f(y25,'operatingHoursMaxUnit')!s:>8} "
              f"MWh={f(y25,'grossLoadMWh')!s:>8} days={f(y25,'daysAnyUnitOperated')!s:>4} | 2026 maxH={f(y26,'operatingHoursMaxUnit')!s:>8} MWh={f(y26,'grossLoadMWh')!s:>8}")


if __name__ == "__main__":
    sys.exit(main())
