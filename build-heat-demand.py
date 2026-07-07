#!/usr/bin/env python3
"""Build data/heat-demand.json: NYC (NYISO Zone J) summer peak demand matched to NYC temperature.

Load  : NYISO monthly `pal` archives (5-min integrated real-time load by zone) -> Zone J "N.Y.C.".
        Daily peak = max of that day's 5-min N.Y.C. readings; daily mean = average.
Temp  : Open-Meteo historical archive (free, keyless), Central Park (40.78, -73.97):
        daily maximum air temperature and daily maximum apparent ("feels-like") temperature, degF.
Window: June-August (core NYC cooling season), 2019 through the latest full summer.

Re-run: `python3 build-heat-demand.py`. Zips are cached in .cache/ (gitignored) to avoid re-downloading.
Every number on the "How heat drives demand" card is computed here; nothing is hand-entered.
"""
import urllib.request, io, zipfile, csv, json, ssl, os, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache"); os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
YEARS = list(range(2019, 2026))     # 2019..2025 (extend as new summers complete)
MONTHS = [6, 7, 8]
LAT, LON = 40.78, -73.97
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-grid-pulse/heat-demand"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")

# ---- 1. NYC (Zone J) daily peak & mean load from monthly pal archives ----
load_daily = {}
for y in YEARS:
    for m in MONTHS:
        tag = f"{y}{m:02d}01"
        zpath = os.path.join(CACHE, f"pal_{tag}.zip")
        try:
            if not os.path.exists(zpath) or os.path.getsize(zpath) < 1000:
                open(zpath, "wb").write(fetch(f"http://mis.nyiso.com/public/csv/pal/{tag}pal_csv.zip", binary=True))
            zf = zipfile.ZipFile(zpath)
        except Exception as e:
            print("  skip", tag, e); continue
        byday = {}
        for name in zf.namelist():
            if not name.endswith(".csv"): continue
            for row in csv.reader(io.StringIO(zf.read(name).decode("utf-8", "replace"))):
                if len(row) < 5 or row[2] != "N.Y.C.": continue
                try: mw = float(row[4])
                except ValueError: continue
                mm, dd, yyyy = row[0][:10].split("/")
                byday.setdefault(f"{yyyy}-{mm}-{dd}", []).append(mw)
        for key, vals in byday.items():
            if vals:
                load_daily[key] = {"peak": round(max(vals), 1), "mean": round(sum(vals) / len(vals), 1)}
    print(f"  load {y}: cumulative {len(load_daily)} days")

# ---- 2. NYC daily max air temp + apparent ("feels-like") max ----
temp_daily = {}
for y in YEARS:
    url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={LAT}&longitude={LON}"
           f"&start_date={y}-06-01&end_date={y}-08-31"
           f"&daily=temperature_2m_max,apparent_temperature_max&temperature_unit=fahrenheit"
           f"&timezone=America%2FNew_York")
    j = json.loads(fetch(url))
    for t, tmax, app in zip(j["daily"]["time"], j["daily"]["temperature_2m_max"], j["daily"]["apparent_temperature_max"]):
        if tmax is not None:
            temp_daily[t] = {"tmax": round(tmax, 1), "feels": round(app, 1) if app is not None else None}

# ---- 3. Join on date ----
rows = []
for d in sorted(set(load_daily) & set(temp_daily)):
    L, T = load_daily[d], temp_daily[d]
    rows.append({"date": d, "tmax": T["tmax"], "feels": T["feels"], "peak": L["peak"]})
if not rows:
    raise SystemExit("no joined days -- check source availability")

# ---- 4. Stats ----
def pearson(xs, ys):
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** .5; sy = sum((y - my) ** 2 for y in ys) ** .5
    return cov / (sx * sy) if sx and sy else 0.0

tmaxs = [r["tmax"] for r in rows]; peaks = [r["peak"] for r in rows]
feelsrows = [r for r in rows if r["feels"] is not None]
n = len(rows); mx = sum(tmaxs) / n; my = sum(peaks) / n
slope = sum((x - mx) * (y - my) for x, y in zip(tmaxs, peaks)) / sum((x - mx) ** 2 for x in tmaxs)

bands = []
for lo, hi in [(60, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 95), (95, 115)]:
    sel = [r["peak"] for r in rows if lo <= r["tmax"] < hi]
    if sel:
        bands.append({"lo": lo, "hi": hi, "n": len(sel),
                      "avg": round(sum(sel) / len(sel)), "max": round(max(sel))})

srt = sorted(rows, key=lambda r: r["tmax"])
q = n // 4
mild = [r["peak"] for r in rows if r["tmax"] < 75]
line = []
for center, half in [(64, 4), (68, 2), (72, 2), (76, 2), (80, 2), (84, 2), (88, 2), (92, 2), (96, 2), (100, 3)]:
    sel = [r["peak"] for r in rows if center - half <= r["tmax"] < center + half]
    if len(sel) >= 3:
        line.append([center, round(sum(sel) / len(sel))])

top_hot = sorted(rows, key=lambda r: -r["tmax"])[:8]
top_peak = sorted(rows, key=lambda r: -r["peak"])[:8]

out = {
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "range": f"June-August {YEARS[0]}-{YEARS[-1]}",
    "nDays": n,
    "loadSource": "NYISO Zone J (N.Y.C.) 5-minute integrated real-time load, monthly pal archives",
    "tempSource": "Open-Meteo historical archive, Central Park (40.78, -73.97)",
    "stats": {
        "rTemp": round(pearson(tmaxs, peaks), 3),
        "rFeels": round(pearson([r["feels"] for r in feelsrows], [r["peak"] for r in feelsrows]), 3),
        "slopeMwPerF": round(slope),
        "mildAvg": round(sum(mild) / len(mild)) if mild else None,
        "hotQuartileAvg": round(sum(r["peak"] for r in srt[-q:]) / q),
        "maxPeak": round(max(peaks)),
        "maxPeakDate": max(rows, key=lambda r: r["peak"])["date"],
    },
    "bands": bands,
    "line": line,
    "hottest": [{"date": r["date"], "tmax": r["tmax"], "feels": r["feels"], "peak": round(r["peak"])} for r in top_hot],
    "peakDays": [{"date": r["date"], "peak": round(r["peak"]), "tmax": r["tmax"], "feels": r["feels"]} for r in top_peak],
    "points": [[r["tmax"], round(r["peak"]), r["feels"], r["date"]] for r in rows],
}
path = os.path.join(ROOT, "data", "heat-demand.json")
json.dump(out, open(path, "w"), separators=(",", ":"))
print(f"\nWrote data/heat-demand.json  ({n} days, {YEARS[0]}-{YEARS[-1]})")
print(f"  r(temp)={out['stats']['rTemp']}  r(feels)={out['stats']['rFeels']}  slope={out['stats']['slopeMwPerF']} MW/F")
print(f"  mild<75F avg={out['stats']['mildAvg']}  hot-quartile avg={out['stats']['hotQuartileAvg']}  max={out['stats']['maxPeak']} on {out['stats']['maxPeakDate']}")
