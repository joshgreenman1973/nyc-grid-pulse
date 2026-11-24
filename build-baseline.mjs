#!/usr/bin/env node
// Builds data/baseline.json — the historical context for the New York City load curve.
//
// Two products, both from NYISO's archived "pal" (actual load) files, which are
// byte-identical in format to the live feed:
//
//   typicalDay : per-5-minute-slot p10 / median / p90 of New York City (Zone J) load
//                across the last 28 days that are the SAME KIND of day as today
//                (today excluded): weekdays if today is a working weekday, else
//                weekends and holidays, which run structurally lower. Read from
//                data/history/nyc-load-curves.json, which build-daily-load.py keeps
//                (NYISO's own current-month archive is too sparse to rely on: in
//                November 2026 it held only six days, leaving a two-day band).
//
//   priorYears : the New York City load curve on the same WEEKDAY one, two and
//                three years back (52, 104, 156 weeks), so a Tuesday is compared
//                with a Tuesday and a holiday never stands in for a working day.
//                Year-to-year differences are driven mostly by weather, not by any
//                trend — labeled as such in the dashboard.
//
// Archived months come as ZIPs (one CSV per day). We shell out to `unzip`
// (present on macOS and the GitHub Actions ubuntu runner).

import { writeFileSync, mkdirSync, rmSync, readFileSync, readdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TMP = `${__dirname}/.tmp-baseline`;
const PRIOR_YEARS = 3;

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;

function parseCsv(text) {
  return text.trim().split(/\r?\n/).map((line) => {
    const f = [];
    let cur = "", q = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (q) { if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"') q = true;
      else if (c === ",") { f.push(cur); cur = ""; }
      else cur += c;
    }
    f.push(cur);
    return f;
  });
}

// returns Map "HH:MM" -> NYC load (MW) for one archived pal CSV.
// Each reading is bucketed to its 5-minute slot (floor) and averaged within the
// day, so days with a few extra/duplicate readings align to a clean 288-slot grid.
function nycBySlot(csvText) {
  const buckets = new Map(); // slot -> {sum, n}
  for (const r of parseCsv(csvText).slice(1)) {
    if (r.length < 5 || r[2] !== "N.Y.C.") continue;
    const [hh, mm] = r[0].trim().split(" ")[1].split(":");
    const mw = parseFloat(r[4]);
    if (!Number.isFinite(mw)) continue;
    const slot = `${hh}:${pad(Math.floor(+mm / 5) * 5)}`;
    if (!buckets.has(slot)) buckets.set(slot, { sum: 0, n: 0 });
    const b = buckets.get(slot);
    b.sum += mw; b.n++;
  }
  const m = new Map();
  for (const [slot, b] of buckets) m.set(slot, b.sum / b.n);
  return m;
}

async function downloadMonthZip(yyyymm01, destDir) {
  const url = `https://mis.nyiso.com/public/csv/pal/${yyyymm01}pal_csv.zip`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} for ${url}`);
  const buf = Buffer.from(await res.arrayBuffer());
  mkdirSync(destDir, { recursive: true });
  const zipPath = `${destDir}/m.zip`;
  writeFileSync(zipPath, buf);
  execSync(`unzip -o -q "${zipPath}" -d "${destDir}"`);
  return destDir;
}

function percentile(sorted, p) {
  if (!sorted.length) return null;
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

// Eastern calendar date (NYISO's dates are Eastern; the Actions runner is UTC).
function easternToday() {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const g = (t) => +parts.find((p) => p.type === t).value;
  return new Date(Date.UTC(g("year"), g("month") - 1, g("day")));
}
// All dates below are UTC-midnight Date objects standing for Eastern calendar days.
const iso = (d) => d.toISOString().slice(0, 10);
const addDays = (d, n) => new Date(d.getTime() + n * 86400000);
const nthWeekday = (y, m, dow, n) => { // n-th (1-based) weekday of a month; n = -1 for the last
  if (n > 0) { const first = new Date(Date.UTC(y, m, 1)); return addDays(first, ((dow - first.getUTCDay() + 7) % 7) + 7 * (n - 1)); }
  const last = new Date(Date.UTC(y, m + 1, 0)); return addDays(last, -((last.getUTCDay() - dow + 7) % 7));
};
const observed = (d) => (d.getUTCDay() === 6 ? addDays(d, -1) : d.getUTCDay() === 0 ? addDays(d, 1) : d);
// Holidays that visibly lower city load: the NERC holidays plus the day after Thanksgiving.
function holidays(y) {
  const thanks = nthWeekday(y, 10, 4, 4);
  return new Set([
    observed(new Date(Date.UTC(y, 0, 1))), nthWeekday(y, 4, 1, -1), observed(new Date(Date.UTC(y, 6, 4))),
    nthWeekday(y, 8, 1, 1), thanks, addDays(thanks, 1), observed(new Date(Date.UTC(y, 11, 25))),
  ].map(iso));
}
const isHoliday = (d) => holidays(d.getUTCFullYear()).has(iso(d));
const isWorkday = (d) => d.getUTCDay() !== 0 && d.getUTCDay() !== 6 && !isHoliday(d);
const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const slotLabel = (i) => `${pad(Math.floor(i / 12))}:${pad((i % 12) * 5)}`;

async function main() {
  rmSync(TMP, { recursive: true, force: true });
  const today = easternToday();
  const todayWork = isWorkday(today);
  const dayType = todayWork ? "weekday" : "weekend or holiday";

  // ---- typical-day band: last 28 days of the same kind as today ----
  const curvesPath = `${__dirname}/data/history/nyc-load-curves.json`;
  const curves = JSON.parse(readFileSync(curvesPath, "utf8")).days; // run build-daily-load.py first
  const windowStart = addDays(today, -28);
  const used = Object.keys(curves)
    .filter((k) => k >= iso(windowStart) && k < iso(today))
    .filter((k) => isWorkday(new Date(k + "T00:00:00Z")) === todayWork)
    .sort();
  const typicalDay = [];
  for (let i = 0; i < 288; i++) {
    const s = used.map((k) => curves[k][i]).filter(Number.isFinite).sort((a, b) => a - b);
    if (!s.length) continue;
    typicalDay.push({ slot: slotLabel(i), p10: +percentile(s, 0.1).toFixed(0), median: +percentile(s, 0.5).toFixed(0), p90: +percentile(s, 0.9).toFixed(0), n: s.length });
  }

  // ---- same weekday 52, 104 and 156 weeks back ----
  const priorYears = [];
  for (let i = 1; i <= PRIOR_YEARS; i++) {
    let d = addDays(today, -364 * i);
    // keep a holiday from standing in for a working day (and vice versa)
    if (isWorkday(d) !== todayWork) d = addDays(d, -7);
    const y = d.getUTCFullYear(), mo = d.getUTCMonth() + 1, da = d.getUTCDate();
    const dir = `${TMP}/y${y}${pad(mo)}`;
    try {
      await downloadMonthZip(`${y}${pad(mo)}01`, dir);
      const slots = nycBySlot(readFileSync(`${dir}/${y}${pad(mo)}${pad(da)}pal.csv`, "utf8"));
      const curve = [...slots.entries()].map(([slot, mw]) => ({ slot, nyc: +mw.toFixed(0) })).sort((a, b) => a.slot.localeCompare(b.slot));
      const peak = curve.reduce((mx, c) => (c.nyc > mx.nyc ? c : mx), { nyc: 0, slot: null });
      priorYears.push({ year: y, date: iso(d), weekday: DOW[d.getUTCDay()], holiday: isHoliday(d), peakMW: peak.nyc, peakSlot: peak.slot, curve });
    } catch (e) {
      console.warn(`  skip ${iso(d)}: ${e.message}`);
    }
  }

  rmSync(TMP, { recursive: true, force: true });

  const out = {
    generatedAt: new Date().toISOString(),
    forDate: iso(today),
    typicalDay: {
      dayType, daysUsed: used.length, windowStart: used[0] ?? null, windowEnd: used[used.length - 1] ?? null,
      todayHoliday: isHoliday(today), slots: typicalDay,
    },
    priorYears,
  };
  mkdirSync(`${__dirname}/data`, { recursive: true });
  writeFileSync(`${__dirname}/data/baseline.json`, JSON.stringify(out));
  console.log(
    `Wrote data/baseline.json for ${out.forDate}\n` +
      `  typical-day band: ${used.length} ${dayType} days (${out.typicalDay.windowStart} to ${out.typicalDay.windowEnd}), ${typicalDay.length} slots\n` +
      `  prior years: ${priorYears.map((p) => `${p.date} (${p.weekday}) peak ${p.peakMW} MW`).join(" · ")}`
  );
}

main().catch((e) => { console.error(e); process.exit(1); });
