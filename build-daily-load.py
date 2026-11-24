#!/usr/bin/env python3
"""Build data/daily-load.json: NYC (NYISO Zone J) daily electricity use for the trailing ~12 months.

Source : NYISO monthly `pal` archives (5-minute integrated real-time load by zone) -> Zone J "N.Y.C.".
         Per day we compute:
           peak   = max of that day's 5-minute N.Y.C. readings (MW)  -- the day's high-water mark
           mean   = average of the readings (MW)                     -- the day's typical load
           energy = mean * 24 / 1000 (GWh)                           -- total electricity consumed that day
         mean*24 is used for energy (not a raw sum) because archived days carry a few extra readings
         (~293 vs 288), so an average-times-hours integral is count-robust.
Window : the 365 calendar days ending with the last complete day.
Memory : NYISO's public archive is not a reliable record. The current-month zip can be sparse (in
         November 2026 it held only Nov. 19-24) and per-day files vanish after about eight days. So
         every day this script has ever computed is carried forward from the previous
         data/daily-load.json; a day is only ever replaced by a fresh complete computation, never
         dropped because NYISO stopped serving it. Days still missing are listed in "missing".
Curves : also writes data/history/nyc-load-curves.json, the 5-minute Zone J curve (288 slots) for
         each of the last 70 complete days, carried forward the same way. build-baseline.mjs reads it.

Re-run : `python3 build-daily-load.py`. Zips are cached in .cache/ (gitignored) and shared with
         build-heat-demand.py. Every number in data/daily-load.json is computed here; nothing is hand-entered.
"""
import urllib.request, io, zipfile, csv, json, ssl, os, datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache"); os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-grid-pulse/daily-load"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")

# ---- month list: current month back 13 months (covers a full rolling year + the partial current month) ----
# NYISO dates are Eastern; the Actions runner's clock is UTC.
today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
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
slots_daily = {}   # date -> {slot index 0..287: [readings]}

def ingest_csv(text):
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or row[2] != "N.Y.C.":
            continue
        try:
            mw = float(row[4])
        except ValueError:
            continue
        mm, dd, yyyy = row[0][:10].split("/")
        key = f"{yyyy}-{mm}-{dd}"
        load_daily.setdefault(key, []).append(mw)
        try:
            hh, mi = int(row[0][11:13]), int(row[0][14:16])
            slot = (hh * 60 + mi) // 5
            if 0 <= slot < 288:
                slots_daily.setdefault(key, {}).setdefault(slot, []).append(mw)
        except ValueError:
            pass

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
fresh = {}
for key in sorted(load_daily):
    vals = load_daily[key]
    if len(vals) < 200 or key >= today.isoformat():   # drop partial days (a full day has ~288 readings)
        continue
    mean = sum(vals) / len(vals)
    fresh[key] = {
        "date": key,
        "peak": round(max(vals), 1),
        "mean": round(mean, 1),
        "energy": round(mean * 24 / 1000, 2),   # GWh consumed that day
    }

# ---- carry forward every day already captured (NYISO's archive drops days) ----
path = os.path.join(ROOT, "data", "daily-load.json")
kept = 0
if os.path.exists(path):
    try:
        for r in json.load(open(path)).get("days", []):
            if r["d"] not in fresh and r["d"] < today.isoformat():
                fresh[r["d"]] = {"date": r["d"], "peak": r["peak"], "mean": r["mean"], "energy": r["energy"]}
                kept += 1
    except Exception as e:
        print("  could not read previous daily-load.json:", e)
print(f"  carried forward {kept} days NYISO no longer serves")

if not fresh:
    raise SystemExit("no complete days -- check source availability")

# ---- window: the 365 calendar days ending with the last complete day ----
end = datetime.date.fromisoformat(max(fresh))
start = end - datetime.timedelta(days=364)
rows, missing = [], []
d = start
while d <= end:
    k = d.isoformat()
    if k in fresh:
        rows.append(fresh[k])
    else:
        missing.append(k)
    d += datetime.timedelta(days=1)
if missing:
    print(f"  WARNING: {len(missing)} days missing from the window: {missing[:10]}{'...' if len(missing) > 10 else ''}")

# ---- 5-minute curves for the last 70 complete days (read by build-baseline.mjs) ----
curves_path = os.path.join(ROOT, "data", "history", "nyc-load-curves.json")
os.makedirs(os.path.dirname(curves_path), exist_ok=True)
curves = {}
if os.path.exists(curves_path):
    try:
        curves = json.load(open(curves_path)).get("days", {})
    except Exception as e:
        print("  could not read previous curves:", e)
for key, slots in slots_daily.items():
    if key >= today.isoformat() or len(slots) < 280:   # complete days only
        continue
    curve, last = [], None
    for i in range(288):
        v = slots.get(i)
        last = round(sum(v) / len(v), 1) if v else last   # fill a rare missing slot with the prior one
        curve.append(last)
    if curve[0] is None:
        continue
    curves[key] = curve
cutoff = (end - datetime.timedelta(days=69)).isoformat()
curves = {k: v for k, v in sorted(curves.items()) if k >= cutoff}
json.dump({
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source": "NYISO Zone J (N.Y.C.) 5-minute integrated real-time load (pal); readings averaged into 288 five-minute slots per day",
    "days": curves,
}, open(curves_path, "w"), separators=(",", ":"))
print(f"  wrote {len(curves)} daily curves to data/history/nyc-load-curves.json")

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
    "missing": missing,
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
json.dump(out, open(path, "w"), separators=(",", ":"))
print(f"\nWrote data/daily-load.json  ({len(rows)} days, {rows[0]['date']} -> {rows[-1]['date']})")
s = out["stats"]
print(f"  peak: max {s['peakMax']} MW on {s['peakMaxDate']}, min {s['peakMin']} MW on {s['peakMinDate']}")
print(f"  energy: {s['energyTotalTWh']} TWh over the year; max {s['energyMax']} GWh on {s['energyMaxDate']}")
