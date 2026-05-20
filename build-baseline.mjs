#!/usr/bin/env node
// Builds data/baseline.json — the historical context for the New York City load curve.
//
// Two products, both from NYISO's archived "pal" (actual load) files, which are
// byte-identical in format to the live feed:
//
//   typicalDay : per-5-minute-slot p10 / median / p90 of New York City (Zone J) load
//                across the WEEKDAYS of the current month so far (today excluded).
//                A "what does a normal day look like" band. Weekday-only because
//                weekend load is structurally lower and today is a weekday.
//
//   priorYears : the New York City load curve on the SAME calendar date in prior
//                years (default 3). Year-to-year differences are driven mostly by
//                weather, not by any trend — labeled as such in the dashboard.
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

async function main() {
  rmSync(TMP, { recursive: true, force: true });
  const now = new Date();
  const month01 = `${now.getFullYear()}${pad(now.getMonth() + 1)}01`;
  const todayStr = ymd(now);

  // ---- typical weekday band from the current month so far ----
  const monthDir = `${TMP}/cur`;
  await downloadMonthZip(month01, monthDir);
  const slotValues = new Map(); // "HH:MM" -> [MW,...]
  let weekdaysUsed = 0;
  for (const file of readdirSync(monthDir)) {
    const m = file.match(/^(\d{8})pal\.csv$/);
    if (!m) continue;
    const dateStr = m[1];
    if (dateStr >= todayStr) continue; // exclude today and any future
    const d = new Date(+dateStr.slice(0, 4), +dateStr.slice(4, 6) - 1, +dateStr.slice(6, 8));
    const dow = d.getDay();
    if (dow === 0 || dow === 6) continue; // weekdays only
    weekdaysUsed++;
    for (const [slot, mw] of nycBySlot(readFileSync(`${monthDir}/${file}`, "utf8"))) {
      if (!slotValues.has(slot)) slotValues.set(slot, []);
      slotValues.get(slot).push(mw);
    }
  }
  const typicalDay = [...slotValues.entries()]
    .map(([slot, arr]) => {
      const s = arr.slice().sort((a, b) => a - b);
      return { slot, p10: +percentile(s, 0.1).toFixed(0), median: +percentile(s, 0.5).toFixed(0), p90: +percentile(s, 0.9).toFixed(0), n: arr.length };
    })
    .sort((a, b) => a.slot.localeCompare(b.slot));

  // ---- prior-year same-date curves ----
  const priorYears = [];
  for (let i = 1; i <= PRIOR_YEARS; i++) {
    const y = now.getFullYear() - i;
    const ym01 = `${y}${pad(now.getMonth() + 1)}01`;
    const dir = `${TMP}/y${y}`;
    try {
      await downloadMonthZip(ym01, dir);
      const dateFile = `${dir}/${y}${pad(now.getMonth() + 1)}${pad(now.getDate())}pal.csv`;
      const slots = nycBySlot(readFileSync(dateFile, "utf8"));
      const curve = [...slots.entries()].map(([slot, mw]) => ({ slot, nyc: +mw.toFixed(0) })).sort((a, b) => a.slot.localeCompare(b.slot));
      const peak = curve.reduce((mx, c) => (c.nyc > mx.nyc ? c : mx), { nyc: 0, slot: null });
      priorYears.push({ year: y, date: `${y}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`, peakMW: peak.nyc, peakSlot: peak.slot, curve });
    } catch (e) {
      console.warn(`  skip ${y}: ${e.message}`);
    }
  }

  rmSync(TMP, { recursive: true, force: true });

  const out = {
    generatedAt: new Date().toISOString(),
    forDate: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    typicalDay: { weekdaysUsed, monthLabel: now.toLocaleString("en-US", { month: "long", year: "numeric" }), slots: typicalDay },
    priorYears,
  };
  mkdirSync(`${__dirname}/data`, { recursive: true });
  writeFileSync(`${__dirname}/data/baseline.json`, JSON.stringify(out));
  console.log(
    `Wrote data/baseline.json\n` +
      `  typical-day band: ${weekdaysUsed} weekdays of ${out.typicalDay.monthLabel}, ${typicalDay.length} slots\n` +
      `  prior years: ${priorYears.map((p) => `${p.year} peak ${p.peakMW} MW`).join(" · ")}`
  );
}

main().catch((e) => { console.error(e); process.exit(1); });
