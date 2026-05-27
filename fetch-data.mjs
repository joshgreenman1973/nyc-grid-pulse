#!/usr/bin/env node
// Fetches NYISO real-time public CSV feeds and bundles them into data/latest.json
// so the static dashboard can read same-origin JSON (the NYISO feeds send no CORS header).
//
// Feeds (all 5-minute resolution, refreshed through the day):
//   rtfuelmix   - statewide generation by fuel category (MW)
//   pal         - actual load by NYISO zone (MW)   [NYC = "N.Y.C." = Zone J]
//   realtime_zone - real-time LBMP wholesale price by zone ($/MWh)
//
// No API key required. Run on a schedule to keep the snapshot fresh.

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// --- emission factors: pounds of CO2 per MWh of generation (operational combustion only) ---
// Sources: U.S. EIA average operating heat rates / emission factors.
// Lifecycle/upstream emissions are excluded. Dual Fuel is assumed gas-fired.
const CO2_LBS_PER_MWH = {
  "Natural Gas": 898,
  "Dual Fuel": 898,
  "Other Fossil Fuels": 1672, // oil/coal proxy
  Nuclear: 0,
  Hydro: 0,
  Wind: 0,
  "Other Renewables": 0,
};

const CLEAN_CATEGORIES = new Set(["Nuclear", "Hydro", "Wind", "Other Renewables"]);

// NYISO publishes its CSV files by Eastern (America/New_York) date, while the
// GitHub Actions runner's clock is UTC. Deriving the date from UTC asks NYISO
// for "tomorrow's" file between 8pm and midnight Eastern -> 404. Always build
// the date string in Eastern time.
function easternYmd(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t).value;
  return `${get("year")}${get("month")}${get("day")}`;
}

async function getCsv(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.text();
}

// minimal CSV parser handling quoted fields
function parseCsv(text) {
  const rows = [];
  for (const line of text.trim().split(/\r?\n/)) {
    const fields = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"') {
          if (line[i + 1] === '"') { cur += '"'; i++; } else inQ = false;
        } else cur += c;
      } else if (c === '"') inQ = true;
      else if (c === ",") { fields.push(cur); cur = ""; }
      else cur += c;
    }
    fields.push(cur);
    rows.push(fields);
  }
  return rows;
}

// "05/20/2026 13:25:00" -> ISO-ish key + display
function parseTs(s) {
  const [date, time] = s.trim().split(" ");
  const [mm, dd, yyyy] = date.split("/");
  return `${yyyy}-${mm}-${dd}T${time}`;
}

function fetchFeeds(D) {
  return Promise.all([
    getCsv(`https://mis.nyiso.com/public/csv/rtfuelmix/${D}rtfuelmix.csv`),
    getCsv(`https://mis.nyiso.com/public/csv/pal/${D}pal.csv`),
    getCsv(`https://mis.nyiso.com/public/csv/realtime/${D}realtime_zone.csv`),
    // isolf  = NYISO's own zonal load forecast (MW, hourly). Same definition as actual load.
    getCsv(`https://mis.nyiso.com/public/csv/isolf/${D}isolf.csv`),
    // damlbmp = day-ahead market LBMP by zone ($/MWh, hourly). Same definition as real-time LBMP.
    getCsv(`https://mis.nyiso.com/public/csv/damlbmp/${D}damlbmp_zone.csv`),
  ]);
}

async function main() {
  // Right after Eastern midnight NYISO may not have posted the new day's files
  // yet, so fall back to the prior Eastern day until they appear.
  let D = easternYmd();
  let feeds;
  try {
    feeds = await fetchFeeds(D);
  } catch (e) {
    if (/^404\b/.test(e.message)) {
      D = easternYmd(new Date(Date.now() - 86_400_000));
      console.warn(`Today's NYISO files not posted yet; falling back to ${D}`);
      feeds = await fetchFeeds(D);
    } else {
      throw e;
    }
  }
  const [fuelTxt, loadTxt, priceTxt, forecastTxt, daPriceTxt] = feeds;

  // ---- fuel mix: group by timestamp ----
  const fuelRows = parseCsv(fuelTxt).slice(1);
  const fuelByTs = new Map();
  for (const r of fuelRows) {
    if (r.length < 4 || !r[0]) continue;
    const t = parseTs(r[0]);
    const cat = r[2];
    const mw = parseFloat(r[3]);
    if (!Number.isFinite(mw)) continue;
    if (!fuelByTs.has(t)) fuelByTs.set(t, {});
    fuelByTs.get(t)[cat] = mw;
  }
  const fuelMix = [...fuelByTs.entries()].map(([t, cats]) => {
    const total = Object.values(cats).reduce((a, b) => a + b, 0);
    let clean = 0, co2 = 0;
    for (const [cat, mw] of Object.entries(cats)) {
      if (CLEAN_CATEGORIES.has(cat)) clean += mw;
      co2 += mw * (CO2_LBS_PER_MWH[cat] ?? 0);
    }
    return {
      t,
      cats,
      total: +total.toFixed(1),
      cleanMW: +clean.toFixed(1),
      cleanPct: total ? +((clean / total) * 100).toFixed(1) : 0,
      co2RateLbsPerMWh: total ? +(co2 / total).toFixed(0) : 0,
    };
  });

  // ---- load: group by timestamp, keep zones ----
  const loadRows = parseCsv(loadTxt).slice(1);
  const loadByTs = new Map();
  for (const r of loadRows) {
    if (r.length < 5 || !r[0]) continue;
    const t = parseTs(r[0]);
    const zone = r[2];
    const mw = parseFloat(r[4]);
    if (!Number.isFinite(mw)) continue;
    if (!loadByTs.has(t)) loadByTs.set(t, {});
    loadByTs.get(t)[zone] = +mw.toFixed(1);
  }
  const load = [...loadByTs.entries()].map(([t, zones]) => {
    const total = Object.values(zones).reduce((a, b) => a + b, 0);
    return { t, zones, total: +total.toFixed(1), nyc: zones["N.Y.C."] ?? null };
  });

  // ---- price (LBMP): group by timestamp, keep zones ----
  const priceRows = parseCsv(priceTxt).slice(1);
  const priceByTs = new Map();
  for (const r of priceRows) {
    if (r.length < 4 || !r[0]) continue;
    const t = parseTs(r[0]);
    const zone = r[1];
    const lbmp = parseFloat(r[3]);
    const congestion = parseFloat(r[5]);
    if (!Number.isFinite(lbmp)) continue;
    if (!priceByTs.has(t)) priceByTs.set(t, {});
    priceByTs.get(t)[zone] = { lbmp, congestion: Number.isFinite(congestion) ? congestion : 0 };
  }
  const price = [...priceByTs.entries()].map(([t, zones]) => ({ t, zones }));

  // ---- NYC load forecast (isolf, hourly). Column "N.Y.C." ----
  const fcRows = parseCsv(forecastTxt);
  const fcHeader = fcRows[0].map((h) => h.trim());
  const nycCol = fcHeader.findIndex((h) => h === "N.Y.C.");
  const forecast = [];
  const forecastByHour = new Map(); // "YYYY-MM-DDTHH" -> MW
  for (const r of fcRows.slice(1)) {
    if (!r[0] || nycCol < 0) continue;
    const t = parseTs(r[0]); // "...THH:00"
    const mw = parseFloat(r[nycCol]);
    if (!Number.isFinite(mw)) continue;
    forecast.push({ t, nyc: mw });
    forecastByHour.set(t.slice(0, 13), mw);
  }

  // ---- NYC day-ahead LBMP (damlbmp, hourly) ----
  const daRows = parseCsv(daPriceTxt).slice(1);
  const dayAhead = [];
  const dayAheadByHour = new Map(); // "YYYY-MM-DDTHH" -> $/MWh
  for (const r of daRows) {
    if (!r[0] || r[1] !== "N.Y.C.") continue;
    const t = parseTs(r[0]);
    const lbmp = parseFloat(r[3]);
    if (!Number.isFinite(lbmp)) continue;
    dayAhead.push({ t, nyc: lbmp });
    dayAheadByHour.set(t.slice(0, 13), lbmp);
  }

  // ---- latest convenience snapshot ----
  const lastFuel = fuelMix[fuelMix.length - 1];
  const lastLoad = load[load.length - 1];
  const lastPrice = price[price.length - 1];
  const nycPrice = lastPrice?.zones?.["N.Y.C."]?.lbmp ?? null;

  // wholesale spend rate right now: NYC load (MW) * NYC price ($/MWh) = $/hour
  const nycSpendPerHour =
    lastLoad?.nyc != null && nycPrice != null ? Math.round(lastLoad.nyc * nycPrice) : null;

  // peak NYC load so far today
  let peakNyc = { t: null, mw: 0 };
  for (const l of load) if (l.nyc != null && l.nyc > peakNyc.mw) peakNyc = { t: l.t, mw: l.nyc };

  // forecast / day-ahead comparators for the current hour (matched by hour key)
  const curHourKey = lastLoad?.t?.slice(0, 13) ?? null;
  const forecastNow = curHourKey ? forecastByHour.get(curHourKey) ?? null : null;
  const dayAheadNow = curHourKey ? dayAheadByHour.get(curHourKey) ?? null : null;
  const loadVsForecastPct =
    forecastNow && lastLoad?.nyc != null ? +(((lastLoad.nyc - forecastNow) / forecastNow) * 100).toFixed(1) : null;
  const rtVsDaPct =
    dayAheadNow && nycPrice != null ? +(((nycPrice - dayAheadNow) / dayAheadNow) * 100).toFixed(1) : null;

  const out = {
    generatedAt: new Date().toISOString(),
    sourceDate: `${D.slice(0, 4)}-${D.slice(4, 6)}-${D.slice(6, 8)}`,
    timezone: "America/New_York",
    co2FactorsLbsPerMWh: CO2_LBS_PER_MWH,
    cleanCategories: [...CLEAN_CATEGORIES],
    latest: {
      t: lastLoad?.t ?? null,
      nycLoadMW: lastLoad?.nyc ?? null,
      stateLoadMW: lastLoad?.total ?? null,
      nycSharePct: lastLoad?.nyc && lastLoad?.total ? +((lastLoad.nyc / lastLoad.total) * 100).toFixed(1) : null,
      nycForecastMW: forecastNow,
      loadVsForecastPct,
      nycPriceLbmp: nycPrice,
      nycDayAheadLbmp: dayAheadNow,
      rtVsDaPct,
      nycSpendPerHour,
      cleanPct: lastFuel?.cleanPct ?? null,
      co2RateLbsPerMWh: lastFuel?.co2RateLbsPerMWh ?? null,
      fuelCats: lastFuel?.cats ?? null,
      genTotalMW: lastFuel?.total ?? null,
      peakNyc,
    },
    fuelMix,
    load,
    price,
    forecast,
    dayAhead,
  };

  const outPath = `${__dirname}/data/latest.json`;
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(out));
  console.log(
    `Wrote ${outPath}\n` +
      `  timestamp: ${out.latest.t}\n` +
      `  NYC load: ${out.latest.nycLoadMW} MW (${out.latest.nycSharePct}% of state ${out.latest.stateLoadMW} MW)\n` +
      `  vs forecast: ${out.latest.nycForecastMW} MW (${out.latest.loadVsForecastPct > 0 ? "+" : ""}${out.latest.loadVsForecastPct}%)\n` +
      `  NYC price: $${out.latest.nycPriceLbmp}/MWh real-time vs $${out.latest.nycDayAheadLbmp} day-ahead (${out.latest.rtVsDaPct > 0 ? "+" : ""}${out.latest.rtVsDaPct}%)\n` +
      `  -> ~$${out.latest.nycSpendPerHour?.toLocaleString()}/hr wholesale\n` +
      `  clean gen: ${out.latest.cleanPct}%  |  CO2 rate: ${out.latest.co2RateLbsPerMWh} lbs/MWh\n` +
      `  points: ${fuelMix.length} fuel / ${load.length} load / ${price.length} price`
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
