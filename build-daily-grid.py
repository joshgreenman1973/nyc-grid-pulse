#!/usr/bin/env python3
"""Build data/history/daily-grid.json: one row per Eastern calendar day for the trailing 365 days
ending yesterday -- NYC price, statewide fuel mix and carbon, load, net imports and the Champlain
Hudson Power Express (CHPE) line.

Sources (NYISO public CSVs; monthly zips at https://mis.nyiso.com/public/csv/<folder>/YYYYMM01<file>_csv.zip,
per-day files at https://mis.nyiso.com/public/csv/<folder>/YYYYMMDD<file>.csv for the last ~8 days):
  realtime/realtime_zone          5-minute real-time LBMP by zone -> N.Y.C.
                                  rt    = time-weighted daily average ($/MWh). Rows are interval-ending
                                          (00:05 .. next day 00:00) and include irregular extra dispatch
                                          intervals (e.g. 09:32:47), so each price is weighted by the
                                          minutes since the previous row.
                                  rtMax = highest interval price of the day.
  damlbmp/damlbmp_zone            hourly day-ahead LBMP -> N.Y.C.; da = mean of the day's hours.
  rtfuelmix/rtfuelmix             statewide generation by fuel category (MW, ~5-minute).
                                  gen[cat] = mean MW x hours in the day (MWh). cleanPct = Nuclear + Hydro +
                                  Wind + Other Renewables as a share of the total. co2 = lbs CO2/MWh using
                                  the same factors as fetch-data.mjs.
  pal/pal                         5-minute real-time actual load by zone. loadMW = mean of the 11-zone sum;
                                  nycMW = mean of N.Y.C.; loadMWh = loadMW x hours in the day.
  palIntegrated/palIntegrated     hourly integrated load by zone. Used ONLY for days whose 5-minute pal file
                                  NYISO no longer serves (the November 2026 pal zip held only Nov. 19-24);
                                  those dates are listed in "loadFromIntegrated".
  ExternalLimitsFlows/ExternalLimitsFlows
                                  5-minute interface flows; the row "SCH - HQ_CHPE" is the scheduled flow on
                                  CHPE. chpeMW = mean, chpeMax = max. No row that day -> null (not 0).
  netImpMW = statewide load minus in-state generation, averaged over 5-minute timestamps present in both
             pal and rtfuelmix (approximate: it also absorbs transmission losses and pumped storage).

Day length: energy uses mean MW x the actual hours in the Eastern day (23 on the March DST day, 25 on the
November one), not a raw sum, because archived days carry a few extra off-grid readings (~293 vs 288).
Days with fewer than 200 readings in a feed are treated as incomplete and not computed from that feed.

Memory: NYISO's public archive is not a reliable record (current-month zips can be sparse; per-day files
404 after ~8 days). Every value this script has ever computed is carried forward from the previous
data/history/daily-grid.json, per feed group; a value is only replaced by a fresh complete computation.
Dates with no data at all are listed in "missing"; dates lacking one group are listed in "gaps".

Re-run: `python3 build-daily-grid.py`. Zips are cached in .cache/ (gitignored); pal zips are shared with
build-daily-load.py. Every number is computed here; nothing is hand-entered.
"""
import urllib.request, io, zipfile, csv, json, os, datetime, time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache"); os.makedirs(CACHE, exist_ok=True)
OUT = os.path.join(ROOT, "data", "history", "daily-grid.json")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
NY = ZoneInfo("America/New_York")
BASE = "https://mis.nyiso.com/public/csv"
T0 = time.time()

CO2_LBS_PER_MWH = {"Natural Gas": 898, "Dual Fuel": 898, "Other Fossil Fuels": 1672}   # others 0 (as fetch-data.mjs)
FUEL_CATS = ["Dual Fuel", "Natural Gas", "Nuclear", "Other Fossil Fuels", "Other Renewables", "Wind", "Hydro"]
CLEAN = {"Nuclear", "Hydro", "Wind", "Other Renewables"}
ZONES = {"CAPITL", "CENTRL", "DUNWOD", "GENESE", "HUD VL", "LONGIL", "MHK VL", "MILLWD", "N.Y.C.", "NORTH", "WEST"}
MIN_READINGS = 200
CHPE_ROW = "SCH - HQ_CHPE"

# feed key -> (folder, file suffix)
FEEDS = {
    "realtime": ("realtime", "realtime_zone"),
    "damlbmp": ("damlbmp", "damlbmp_zone"),
    "rtfuelmix": ("rtfuelmix", "rtfuelmix"),
    "pal": ("pal", "pal"),
    "palIntegrated": ("palIntegrated", "palIntegrated"),
    "ExternalLimitsFlows": ("ExternalLimitsFlows", "ExternalLimitsFlows"),
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-grid-pulse/daily-grid"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


today = datetime.datetime.now(NY).date()
end = today - datetime.timedelta(days=1)            # last complete Eastern day
start = end - datetime.timedelta(days=364)
months = []
y, m = start.year, start.month
while (y, m) <= (today.year, today.month):
    months.append((y, m))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_after(y, m):
    return datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)


# ---- raw text per feed per day: {feed: {"YYYY-MM-DD": csv text}} -- keep whichever copy has more rows ----
raw = {f: {} for f in FEEDS}


def keep(feed, day, text):
    if day not in raw[feed] or text.count("\n") > raw[feed][day].count("\n"):
        raw[feed][day] = text


for feed, (folder, suffix) in FEEDS.items():
    for (yy, mm) in months:
        tag = f"{yy}{mm:02d}01"
        zpath = os.path.join(CACHE, f"{feed}_{tag}.zip")
        try:
            # a cached zip is only trusted if it was downloaded at least a day after its month ended;
            # otherwise (current month, or a zip saved while the month was still in progress) re-pull it
            fresh_enough = False
            if os.path.exists(zpath) and os.path.getsize(zpath) > 1000:
                saved = datetime.datetime.fromtimestamp(os.path.getmtime(zpath), NY).date()
                fresh_enough = saved > month_after(yy, mm)
            if not fresh_enough:
                data = fetch(f"{BASE}/{folder}/{tag}{suffix}_csv.zip")
                with open(zpath, "wb") as fh:
                    fh.write(data)
            zf = zipfile.ZipFile(zpath)
        except Exception as e:
            print(f"  skip {feed} {tag}: {e}")
            continue
        for name in zf.namelist():
            if name.endswith(".csv") and name[:8].isdigit():
                d = f"{name[:4]}-{name[4:6]}-{name[6:8]}"
                keep(feed, d, zf.read(name).decode("utf-8", "replace"))
    # the monthly archive lags; fill recent days from the per-day endpoint (kept ~8 days)
    for back in range(1, 9):
        d = today - datetime.timedelta(days=back)
        k = d.isoformat()
        if k in raw[feed] and raw[feed][k].count("\n") > 250:
            continue
        try:
            keep(feed, k, fetch(f"{BASE}/{folder}/{d.strftime('%Y%m%d')}{suffix}.csv").decode("utf-8", "replace"))
        except Exception:
            pass
    have = [k for k in raw[feed] if start.isoformat() <= k <= end.isoformat()]
    print(f"  {feed}: {len(have)} day files in window")


# ---- time helpers ----
def hours_in(day):
    a = datetime.datetime(day.year, day.month, day.day, tzinfo=NY)
    n = day + datetime.timedelta(days=1)
    b = datetime.datetime(n.year, n.month, n.day, tzinfo=NY)
    return (b.timestamp() - a.timestamp()) / 3600


def parse_naive(s):
    s = s.strip()
    fmt = "%m/%d/%Y %H:%M:%S" if len(s) > 16 else "%m/%d/%Y %H:%M"
    return datetime.datetime.strptime(s, fmt)


def epoch_tz(s, tzabbr):
    """Timestamp with an explicit EST/EDT column."""
    off = -4 if tzabbr.strip() == "EDT" else -5
    return parse_naive(s).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=off))).timestamp()


def epochs_no_tz(stamps):
    """Timestamps without a zone column, in file order. A step backwards marks the repeated
    (fall-back) hour, which is then read as standard time."""
    out, prev, folded = [], None, False
    for s in stamps:
        n = parse_naive(s)
        if prev is not None and n < prev:
            folded = True
        out.append(n.replace(tzinfo=NY, fold=1 if folded else 0).timestamp())
        prev = n
    return out


def rows(text):
    return list(csv.reader(io.StringIO(text)))[1:]


def mean(v):
    return sum(v) / len(v) if v else None


# ---- per-feed daily computations ----
def calc_realtime(day, text):
    pts = [(r[0], float(r[3])) for r in rows(text) if len(r) > 3 and r[1] == "N.Y.C."]
    if len(pts) < MIN_READINGS:
        return None
    ep = epochs_no_tz([p[0] for p in pts])
    seq = sorted(zip(ep, [p[1] for p in pts]))
    d0 = datetime.datetime(day.year, day.month, day.day, tzinfo=NY).timestamp()
    d1 = d0 + hours_in(day) * 3600
    seq = [s for s in seq if d0 < s[0] <= d1]                 # interval-ending: (00:00, 24:00]
    # drop a trailing run of 15-minute advisory rows if one ever appears in a completed day
    tail = 0
    while len(seq) - tail > 2 and seq[-1 - tail][0] - seq[-2 - tail][0] == 900:
        tail += 1
    if tail >= 2:
        print(f"    {day}: dropped {tail} trailing 15-minute advisory rows")
        seq = seq[:-tail]
    if len(seq) < MIN_READINGS:
        return None
    tot, wsum, prev = 0.0, 0.0, d0
    for t, p in seq:
        w = t - prev
        tot += w * p; wsum += w; prev = t
    if wsum < 0.95 * (d1 - d0):
        print(f"    {day}: realtime covers only {wsum/3600:.1f} h")
        return None
    return {"rt": round(tot / wsum, 2), "rtMax": round(max(p for _, p in seq), 2)}


def calc_da(day, text):
    v = [float(r[3]) for r in rows(text) if len(r) > 3 and r[1] == "N.Y.C."]
    if len(v) < round(hours_in(day)):
        return None
    return {"da": round(mean(v), 2)}


fuel_ts = {}   # day -> {epoch: total gen MW}   (for net imports)
load_ts = {}   # epoch -> statewide load MW      (for net imports)


def calc_fuel(day, text):
    by_t = {}
    for r in rows(text):
        if len(r) < 4:
            continue
        try:
            mw = float(r[3])
        except ValueError:
            continue
        by_t.setdefault(epoch_tz(r[0], r[1]), {})[r[2]] = mw
    by_t = {t: c for t, c in by_t.items() if len(c) == len(FUEL_CATS)}
    if len(by_t) < MIN_READINGS:
        return None
    fuel_ts[day.isoformat()] = {t: sum(c.values()) for t, c in by_t.items()}
    h = hours_in(day)
    gen = {cat: mean([c[cat] for c in by_t.values()]) * h for cat in FUEL_CATS}
    total = sum(gen.values())
    clean = sum(v for k, v in gen.items() if k in CLEAN)
    co2 = sum(v * CO2_LBS_PER_MWH.get(k, 0) for k, v in gen.items())
    return {"cleanPct": round(100 * clean / total, 1), "co2": round(co2 / total),
            "gen": {k: round(v) for k, v in gen.items()}}


def zone_totals(text, value_col):
    by_t = {}
    for r in rows(text):
        if len(r) <= value_col or r[2] not in ZONES:
            continue
        try:
            mw = float(r[value_col])
        except ValueError:
            continue
        by_t.setdefault(epoch_tz(r[0], r[1]), {})[r[2]] = mw
    return {t: z for t, z in by_t.items() if len(z) == len(ZONES)}


def calc_pal(day, text):
    by_t = zone_totals(text, 4)
    if len(by_t) < MIN_READINGS:
        return None
    for t, z in by_t.items():
        load_ts[t] = sum(z.values())
    lm = mean([sum(z.values()) for z in by_t.values()])
    return {"loadMW": round(lm), "nycMW": round(mean([z["N.Y.C."] for z in by_t.values()])),
            "loadMWh": round(lm * hours_in(day))}


def calc_pal_integrated(day, text):
    by_t = zone_totals(text, 4)
    if len(by_t) < round(hours_in(day)):
        return None
    lm = mean([sum(z.values()) for z in by_t.values()])
    return {"loadMW": round(lm), "nycMW": round(mean([z["N.Y.C."] for z in by_t.values()])),
            "loadMWh": round(lm * hours_in(day))}


def calc_chpe(day, text):
    rs = rows(text)
    stamps = {r[0] for r in rs if len(r) > 3}
    if len(stamps) < MIN_READINGS:
        return None                                   # file itself incomplete -> unknown, not null
    v = [float(r[3]) for r in rs if len(r) > 3 and r[1] == CHPE_ROW]
    if not v:
        return {"chpeMW": None, "chpeMax": None}      # line not in the feed that day
    if len(v) < MIN_READINGS:
        print(f"    {day}: only {len(v)} CHPE rows; leaving CHPE null for this day")
        return {"chpeMW": None, "chpeMax": None}
    return {"chpeMW": round(mean(v)), "chpeMax": round(max(v))}


GROUPS = {   # group -> fields carried forward together
    "rt": ["rt", "rtMax"], "da": ["da"], "fuel": ["cleanPct", "co2", "gen"],
    "load": ["loadMW", "nycMW", "loadMWh"], "netImp": ["netImpMW"], "chpe": ["chpeMW", "chpeMax"],
}

fresh = {}            # day -> {group: {field: value}}
from_integrated = set()
window_days = [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]
# fuel/pal for the day after the window end are not needed; net-import matching uses fuel_ts + load_ts
for day in window_days:
    k = day.isoformat()
    g = {}
    for feed, fn, group in [("realtime", calc_realtime, "rt"), ("damlbmp", calc_da, "da"),
                            ("rtfuelmix", calc_fuel, "fuel"), ("pal", calc_pal, "load"),
                            ("ExternalLimitsFlows", calc_chpe, "chpe")]:
        if k in raw[feed]:
            try:
                res = fn(day, raw[feed][k])
            except Exception as e:
                print(f"    {k} {feed}: parse error {e}")
                res = None
            if res is not None:
                g[group] = res
    fresh[k] = g
# pal files are interval-beginning (00:00..23:55) and fuel files interval-ending (00:05..24:00), so the
# day after's pal file supplies the 24:00 point; load every pal file in hand before matching.
for k, text in raw["pal"].items():
    if k > end.isoformat() and k <= today.isoformat():
        try:
            calc_pal(datetime.date.fromisoformat(k), text)
        except Exception:
            pass
for k, g in fresh.items():
    if k in fuel_ts:
        diffs = [load_ts[t] - gmw for t, gmw in fuel_ts[k].items() if t in load_ts]
        if len(diffs) >= MIN_READINGS:
            g["netImp"] = {"netImpMW": round(mean(diffs))}

# ---- cross-check the hourly-integrated fallback against 5-minute pal where both exist ----
chk = []
for k in sorted(fresh):
    if k in raw["palIntegrated"] and "load" in fresh[k] and k not in from_integrated:
        alt = calc_pal_integrated(datetime.date.fromisoformat(k), raw["palIntegrated"][k])
        if alt:
            chk.append(100 * (alt["loadMW"] - fresh[k]["load"]["loadMW"]) / fresh[k]["load"]["loadMW"])
if chk:
    print(f"  palIntegrated vs pal daily loadMW on {len(chk)} overlap days: mean diff {mean(chk):+.2f}%, "
          f"max |diff| {max(abs(c) for c in chk):.2f}%")

# ---- carry forward everything already captured ----
prev_rows = {}
prev_integrated = set()
if os.path.exists(OUT):
    try:
        prev = json.load(open(OUT))
        prev_rows = {r["d"]: r for r in prev.get("days", [])}
        prev_integrated = set(prev.get("loadFromIntegrated", []))
    except Exception as e:
        print("  could not read previous daily-grid.json:", e)
kept = 0
for k, g in fresh.items():
    old = prev_rows.get(k)
    if not old:
        continue
    for group, fields in GROUPS.items():
        if group not in g and all(f in old for f in fields) and any(old[f] is not None for f in fields):
            g[group] = {f: old[f] for f in fields}
            kept += 1
            if group == "load" and k in prev_integrated:
                from_integrated.add(k)
        elif group == "chpe" and group not in g and "chpeMW" in old:
            g[group] = {"chpeMW": old["chpeMW"], "chpeMax": old.get("chpeMax")}
print(f"  carried forward {kept} day-groups NYISO no longer serves")

# ---- last resort for load: hourly palIntegrated, only where neither fresh nor carried 5-minute load exists ----
for k, g in fresh.items():
    day = datetime.date.fromisoformat(k)
    if "load" not in g and k in raw["palIntegrated"]:
        res = calc_pal_integrated(day, raw["palIntegrated"][k])
        if res:
            g["load"] = res
            from_integrated.add(k)
    if "netImp" not in g and k in from_integrated and "fuel" in g:
        # no 5-minute load for this day: daily mean load minus daily mean generation
        gen_mw = sum(g["fuel"]["gen"].values()) / hours_in(day)
        g["netImp"] = {"netImpMW": round(g["load"]["loadMW"] - gen_mw)}

# ---- assemble rows ----
days_out, missing, gaps = [], [], {gr: [] for gr in GROUPS}
for day in window_days:
    k = day.isoformat()
    g = fresh.get(k, {})
    if not g:
        missing.append(k)
        continue
    row = {"d": k}
    for group, fields in GROUPS.items():
        if group in g:
            row.update(g[group])
        else:
            gaps[group].append(k)
            row.update({f: None for f in fields})
    days_out.append(row)
gaps = {gr: v for gr, v in gaps.items() if v}
if not days_out:
    raise SystemExit("no days computed -- check source availability")


# ---- monthly summaries ----
def rnd(v, n):
    return None if v is None else (round(v) if n == 0 else round(v, n))


monthly = []
by_month = {}
for r in days_out:
    by_month.setdefault(r["d"][:7], []).append(r)
for mon, rs in sorted(by_month.items()):
    def avg(f):
        v = [r[f] for r in rs if r.get(f) is not None]
        return mean(v)
    gen_days = [r for r in rs if r.get("gen")]
    gen = {c: sum(r["gen"][c] for r in gen_days) for c in FUEL_CATS} if gen_days else None
    tot = sum(gen.values()) if gen else 0
    rtmax = [r["rtMax"] for r in rs if r.get("rtMax") is not None]
    cmax = [r["chpeMax"] for r in rs if r.get("chpeMax") is not None]
    monthly.append({
        "m": mon, "n": len(rs),
        "rt": rnd(avg("rt"), 2), "rtMax": max(rtmax) if rtmax else None, "da": rnd(avg("da"), 2),
        "cleanPct": round(100 * sum(gen[c] for c in CLEAN) / tot, 1) if tot else None,
        "co2": round(sum(gen[c] * CO2_LBS_PER_MWH.get(c, 0) for c in FUEL_CATS) / tot) if tot else None,
        "gen": gen, "genDays": len(gen_days),
        "loadMW": rnd(avg("loadMW"), 0), "nycMW": rnd(avg("nycMW"), 0),
        "netImpMW": rnd(avg("netImpMW"), 0),
        "chpeMW": rnd(avg("chpeMW"), 0), "chpeMax": max(cmax) if cmax else None,
        "chpeDays": sum(1 for r in rs if r.get("chpeMW") is not None),
    })

out = {
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "sources": {
        "monthlyZip": BASE + "/<folder>/YYYYMM01<file>_csv.zip",
        "dailyCsv": BASE + "/<folder>/YYYYMMDD<file>.csv",
        "feeds": {k: {"folder": f, "file": s} for k, (f, s) in FEEDS.items()},
        "chpeRow": CHPE_ROW,
    },
    "co2Factors": {c: CO2_LBS_PER_MWH.get(c, 0) for c in FUEL_CATS},
    "definitions": {
        "d": "Eastern calendar date.",
        "rt": "New York City (zone N.Y.C.) real-time price, time-weighted daily average, $/MWh.",
        "rtMax": "Highest New York City real-time interval price that day, $/MWh.",
        "da": "New York City day-ahead price, average of the day's hourly prices, $/MWh.",
        "cleanPct": "Share of statewide generation from nuclear, hydro, wind and other renewables, percent of MWh.",
        "co2": "Statewide carbon intensity of in-state generation, pounds of CO2 per MWh, from the co2Factors (combustion only).",
        "gen": "Statewide generation by NYISO fuel category, MWh (average MW times hours in the day).",
        "loadMW": "Statewide electricity load, daily average MW (sum of NYISO's 11 zones).",
        "nycMW": "New York City (zone N.Y.C.) load, daily average MW.",
        "loadMWh": "Statewide electricity load for the day, MWh (loadMW times hours in the day).",
        "netImpMW": "Approximate net imports: statewide load minus in-state generation, daily average MW; also absorbs losses and pumped storage.",
        "chpeMW": "Scheduled flow on the Champlain Hudson Power Express (NYISO row SCH - HQ_CHPE), daily average MW; null when the line is not in the feed.",
        "chpeMax": "Highest 5-minute scheduled CHPE flow that day, MW.",
        "monthly": "Per calendar month: mean of the daily values (n = days present); cleanPct and co2 are computed from summed monthly generation; rtMax and chpeMax are the month's highest.",
    },
    "start": start.isoformat(), "end": end.isoformat(), "nDays": len(days_out),
    "missing": missing, "gaps": gaps, "loadFromIntegrated": sorted(k for k in from_integrated
                                                                    if start.isoformat() <= k <= end.isoformat()),
    "monthly": monthly,
    "days": days_out,
}
with open(OUT, "w") as fh:
    json.dump(out, fh, separators=(",", ":"))
print(f"\nWrote data/history/daily-grid.json: {len(days_out)} days {start} -> {end}, "
      f"{os.path.getsize(OUT)/1024:.0f} KB, {time.time()-T0:.0f}s")
if missing:
    print(f"  missing {len(missing)}: {missing[:12]}{'...' if len(missing) > 12 else ''}")
for gr, v in gaps.items():
    print(f"  gap in {gr}: {len(v)} days {v[:8]}{'...' if len(v) > 8 else ''}")
if from_integrated:
    print(f"  load from hourly palIntegrated on {len(from_integrated)} days")
