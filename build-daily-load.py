#!/usr/bin/env python3
"""Build data/daily-load.json: NYC (NYISO Zone J) daily electricity use for the trailing ~12 months.

Source : NYISO monthly `pal` archives (5-minute integrated real-time load by zone) -> Zone J "N.Y.C.".
         Per day we compute:
           peak   = max of that day's 5-minute N.Y.C. readings (MW)  -- the day's high-water mark
           mean   = average of the readings (MW)                     -- the day's typical load
           energy = mean * 24 / 1000 (GWh)                           -- total electricity consumed that day
         mean*24 is used for energy (not a raw sum) because archived days carry a few extra readings
         (~293 vs 288), so an average-times-hours integral is count-robust.
Window : the most recent ~13 monthly archives, then trimmed to the last 365 available days.

Re-run : `python3 build-daily-load.py`. Zips are cached in .cache/ (gitignored) and shared with
         build-heat-demand.py. Every number in data/daily-load.json is computed here; nothing is hand-entered.
"""
import urllib.request, io, zipfile, csv, json, ssl, os, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache"); os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-grid-pulse/daily-load"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")

# ---- month list: current month back 13 months (covers a full rolling year + the partial current month) ----
today = datetime.date.today()
months = []
y, m = today.year, today.month
for _ in range(14):
    months.append((y, m))
    m -= 1
    if m == 0:
        m = 12; y -= 1
months.reverse()

# ---- NYC (Zone J) daily peak / mean / energy from monthly pal archives ----
# Current-month archive is usually stale by a few days; recent days beyond it are appended from
# the per-day pal endpoint (YYYYMMDDpal.csv), which retains only the most recent days.
load_daily = {}

def ingest_csv(text):
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or row[2] != "N.Y.C.":
            continue
        try:
            mw = float(row[4])
        except ValueError:
            continue
        mm, dd, yyyy = row[0][:10].split("/")
        load_daily.setdefault(f"{yyyy}-{mm}-{dd}", []).append(mw)

for (yy, mm) in months:
    tag = f"{yy}{mm:02d}01"
    zpath = os.path.join(CACHE, f"pal_{tag}.zip")
    is_current = (yy == today.year and mm == today.month)
    try:
        # always re-pull the current (and previous, if early in month) archive so it isn't stale
        stale_ok = os.path.exists(zpath) and os.path.getsize(zpath) > 1000 and not is_current
        if not stale_ok:
            open(zpath, "wb").write(fetch(f"http://mis.nyiso.com/public/csv/pal/{tag}pal_csv.zip", binary=True))
        zf = zipfile.ZipFile(zpath)
    except Exception as e:
        print("  skip", tag, e); continue
    for name in zf.namelist():
        if name.endswith(".csv"):
            ingest_csv(zf.read(name).decode("utf-8", "replace"))
    print(f"  {tag}: cumulative {len(load_daily)} days")

# ---- fill the last few days from the per-day endpoint (archive lags real time) ----
for back in range(0, 8):
    d = today - datetime.timedelta(days=back)
    key = d.strftime("%Y-%m-%d")
    if key in load_daily and len(load_daily[key]) > 200:
        continue  # already well-covered by the monthly archive
    tag = d.strftime("%Y%m%d")
    try:
        ingest_csv(fetch(f"http://mis.nyiso.com/public/csv/pal/{tag}pal.csv"))
    except Exception:
        pass

# ---- reduce to per-day stats ----
rows = []
for key in sorted(load_daily):
    vals = load_daily[key]
    if len(vals) < 200:   # drop partial days (a full day has ~288 5-min readings)
        continue
    mean = sum(vals) / len(vals)
    rows.append({
        "date": key,
        "peak": round(max(vals), 1),
        "mean": round(mean, 1),
        "energy": round(mean * 24 / 1000, 2),   # GWh consumed that day
    })

if not rows:
    raise SystemExit("no complete days -- check source availability")

# ---- trim to the last 365 available days ----
rows = rows[-365:]

# ---- summary stats over the window ----
peaks = [r["peak"] for r in rows]
energies = [r["energy"] for r in rows]
maxpeak = max(rows, key=lambda r: r["peak"])
minpeak = min(rows, key=lambda r: r["peak"])
maxen = max(rows, key=lambda r: r["energy"])
minen = min(rows, key=lambda r: r["energy"])

out = {
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source": "NYISO Zone J (N.Y.C.) 5-minute integrated real-time load, monthly pal archives",
    "start": rows[0]["date"],
    "end": rows[-1]["date"],
    "nDays": len(rows),
    "stats": {
        "peakMax": maxpeak["peak"], "peakMaxDate": maxpeak["date"],
        "peakMin": minpeak["peak"], "peakMinDate": minpeak["date"],
        "energyMax": maxen["energy"], "energyMaxDate": maxen["date"],
        "energyMin": minen["energy"], "energyMinDate": minen["date"],
        "energyTotalTWh": round(sum(energies) / 1000, 2),
        "peakAvg": round(sum(peaks) / len(peaks)),
        "energyAvg": round(sum(energies) / len(energies), 1),
    },
    # compact: one row per day
    "days": [{"d": r["date"], "peak": r["peak"], "mean": r["mean"], "energy": r["energy"]} for r in rows],
}
path = os.path.join(ROOT, "data", "daily-load.json")
json.dump(out, open(path, "w"), separators=(",", ":"))
print(f"\nWrote data/daily-load.json  ({len(rows)} days, {rows[0]['date']} -> {rows[-1]['date']})")
s = out["stats"]
print(f"  peak: max {s['peakMax']} MW on {s['peakMaxDate']}, min {s['peakMin']} MW on {s['peakMinDate']}")
print(f"  energy: {s['energyTotalTWh']} TWh over the year; max {s['energyMax']} GWh on {s['energyMaxDate']}")
