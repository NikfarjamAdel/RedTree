// Turns the API payload into what the page shows for one listing and one
// period. Money is never recalculated here: monthly figures are the
// report's (vw_monthly_report); this only sums them over the period and
// regroups bookings, nights and cost categories.

import { humanize, money, parseISO } from "./format.js";

const pad = (n) => String(n).padStart(2, "0");
export const monthKey = (iso) => iso.slice(0, 7);

export function addMonths(key, n) {
  let [y, m] = key.split("-").map(Number);
  m += n;
  y += Math.floor((m - 1) / 12);
  m = (((m - 1) % 12) + 12) % 12 + 1;
  return `${y}-${pad(m)}`;
}

export function monthsBetween(from, to) {
  const out = [];
  for (let k = from; k <= to; k = addMonths(k, 1)) out.push(k);
  return out;
}

export function daysInMonth(key) {
  const [y, m] = key.split("-").map(Number);
  return new Date(y, m, 0).getDate();
}

const DAY_MS = 86400000;
export const daysBetween = (a, b) => Math.round((parseISO(b) - parseISO(a)) / DAY_MS);
const maxKey = (a, b) => (a > b ? a : b);
const minKey = (a, b) => (a < b ? a : b);
const sum = (rows, f) => rows.reduce((s, r) => s + (Number(f(r)) || 0), 0);

export const isComplete = (m) => m.revenueComplete && !m.interestMissing;

// --------------------------------------------------------------- build
export function buildModel(p) {
  const categories = new Map(p.categories.map((c) => [c.category_code, c]));
  const today = p.freshness.today;
  const latestExport = p.freshness.latest_export;

  const listings = p.listings.map((l) => ({
    id: l.listing_id,
    title: l.listing_title || `Listing ${l.listing_id}`,
    status: l.listing_status,
    listedFrom: l.listed_from,
    cleaningCost: l.cleaning_cost_per_turnover,
    loanShare: l.loan_interest_share,
    propertyId: l.property_id,
    property: l.property_name,
    address: l.address,
  }));

  const data = new Map();
  for (const listing of listings) {
    data.set(listing.id, {
      listing, months: new Map(), bookings: new Map(), nightIndex: new Map(),
      shares: [], bills: [], rules: [], blocked: [],
    });
  }

  const monthRow = (d, key) => {
    if (!d.months.has(key)) {
      const monthEnd = `${key}-${pad(daysInMonth(key))}`;
      d.months.set(key, {
        key, nights: 0, available: 0, revenue: 0, fee: 0, expenses: 0, cleaning: 0,
        operating: 0, interest: 0, kept: 0, unverified: 0, inReport: false,
        interestMissing: false,
        revenueComplete: Boolean(latestExport && latestExport > monthEnd),
      });
    }
    return d.months.get(key);
  };

  for (const r of p.months) {
    const d = data.get(r.listing_id);
    if (!d) continue;
    Object.assign(monthRow(d, monthKey(r.month_start)), {
      nights: r.nights_sold, revenue: r.revenue, expenses: r.expenses, cleaning: r.cleaning,
      operating: r.operating_profit, interest: r.loan_interest, kept: r.profit_after_interest,
      interestMissing: Boolean(r.interest_missing), revenueComplete: Boolean(r.revenue_complete),
      unverified: r.unverified_expenses, inReport: true,
    });
  }
  for (const r of p.fees) {
    const d = data.get(r.listing_id);
    if (d && d.months.has(monthKey(r.month_start))) d.months.get(monthKey(r.month_start)).fee = r.platform_fee;
  }
  for (const r of p.available) {
    const d = data.get(r.listing_id);
    if (!d) continue;
    const key = monthKey(r.month_start);
    if (d.months.has(key) || r.available_nights > 0) monthRow(d, key).available = r.available_nights;
  }

  for (const r of p.bookings) {
    const d = data.get(r.listing_id);
    if (!d) continue;
    d.bookings.set(r.booking_id, {
      id: r.booking_id, code: r.confirmation_code, guest: r.guest, bookedOn: r.booked_on,
      checkIn: r.check_in, checkOut: r.check_out, nightlyRate: r.nightly_rate,
      guests: r.number_of_guests, source: r.source,
      gross: r.gross_amount, fee: r.platform_fee, net: r.net_amount, payoutDate: r.payout_date,
      cleaningCost: r.cleaning_cost, cancelled: Boolean(r.is_cancelled), nights: [],
    });
  }
  for (const r of p.nights) {
    const d = data.get(r.listing_id);
    const b = d && d.bookings.get(r.booking_id);
    if (!b) continue;
    b.nights.push({ date: r.night_date, rate: r.rate_night });
    d.nightIndex.set(r.night_date, { bookingId: r.booking_id, rate: r.rate_night });
  }
  for (const d of data.values()) {
    for (const b of d.bookings.values()) {
      b.nights.sort((a, c) => (a.date < c.date ? -1 : 1));
      b.nightCount = b.nights.length;
      b.avgRate = b.nightCount ? sum(b.nights, (n) => n.rate) / b.nightCount : b.nightlyRate;
      b.firstNight = b.nights[0]?.date;
      b.lastNight = b.nights[b.nightCount - 1]?.date;
      b.leadDays = b.bookedOn ? Math.max(0, daysBetween(b.bookedOn, b.checkIn)) : null;
    }
    const rates = [];
    for (const b of d.bookings.values()) for (const n of b.nights) rates.push(n.rate);
    d.rateBins = rateBins(rates);
  }

  for (const r of p.expense_shares) {
    data.get(r.listing_id)?.shares.push({ key: monthKey(r.month_start), category: r.category,
      type: r.expense_type, amount: r.amount, unverified: r.unverified });
  }
  for (const r of p.expense_bills) {
    data.get(r.listing_id)?.bills.push({ key: monthKey(r.month_start), category: r.category,
      scope: r.scope, amount: r.amount });
  }
  for (const r of p.apportionment) data.get(r.listing_id)?.rules.push(r);
  for (const r of p.blocked) data.get(r.listing_id)?.blocked.push(r);

  return { listings, data, categories, today, freshness: p.freshness, checks: p.checks,
           generatedAt: p.generated_at };
}

/** Rate thresholds at the quartiles, rounded to clean numbers, so the
 *  legend reads "$200–250" and each bin holds a fair share of nights.
 *  Taken over all of a listing's nights, so a stay's shade never changes
 *  with the period. */
function rateBins(rates) {
  if (!rates.length) return [];
  const s = [...rates].sort((a, b) => a - b);
  const q = (p) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))];
  for (const step of [50, 25, 10]) {
    const t = [q(0.25), q(0.5), q(0.75)].map((v) => Math.round(v / step) * step);
    if (new Set(t).size === 3 && t[0] > s[0]) return t;
  }
  return [...new Set([q(0.25), q(0.5), q(0.75)].map(Math.round))];
}

export function rateBin(bins, rate) {
  let i = 0;
  while (i < bins.length && rate >= bins[i]) i++;
  return i + 1; // 1..bins.length+1
}

export function rateBinLabels(bins) {
  if (!bins.length) return [];
  const labels = [`Under $${bins[0]}`];
  for (let i = 1; i < bins.length; i++) labels.push(`$${bins[i - 1]}–${bins[i]}`);
  labels.push(`$${bins[bins.length - 1]} and up`);
  return labels;
}

// ------------------------------------------------------------- periods
export function periodPresets(d) {
  const keys = [...d.months.keys()].sort();
  if (!keys.length) return [];
  const first = keys[0];
  const last = keys[keys.length - 1];
  const complete = keys.filter((k) => isComplete(d.months.get(k)));
  const out = [];

  if (complete.length) {
    const end = complete[complete.length - 1];
    out.push({ id: "last12", group: "recent", label: "Last 12 complete months",
               from: maxKey(addMonths(end, -11), first), to: end });
  }
  // Australian financial years run 1 July to 30 June
  const fyOf = (k) => { const [y, m] = k.split("-").map(Number); return m >= 7 ? y : y - 1; };
  for (let y = fyOf(last); y >= fyOf(first); y--) {
    out.push({ id: `fy${y}`, group: "fy", label: `Financial year ${y}–${String(y + 1).slice(2)}`,
               from: maxKey(`${y}-07`, first), to: minKey(`${y + 1}-06`, last) });
  }
  for (let y = Number(last.slice(0, 4)); y >= Number(first.slice(0, 4)); y--) {
    out.push({ id: `cy${y}`, group: "cy", label: `Calendar year ${y}`,
               from: maxKey(`${y}-01`, first), to: minKey(`${y}-12`, last) });
  }
  out.push({ id: "all", group: "all", label: "All months", from: first, to: last });
  return out;
}

// ---------------------------------------------------------------- view
export function buildView(model, listingId, from, to) {
  const d = model.data.get(listingId);
  const keys = monthsBetween(from, to).filter((k) => d.months.has(k));
  const months = keys.map((k) => d.months.get(k));
  const t = {
    revenue: sum(months, (m) => m.revenue),
    fee: sum(months, (m) => m.fee),
    expenses: sum(months, (m) => m.expenses),
    cleaning: sum(months, (m) => m.cleaning),
    operating: sum(months, (m) => m.operating),
    interest: sum(months, (m) => m.interest),
    kept: sum(months, (m) => m.kept),
    nights: sum(months, (m) => m.nights),
    available: sum(months, (m) => m.available),
    unverified: sum(months, (m) => m.unverified),
  };
  t.gross = t.revenue + t.fee;

  const fromDate = `${from}-01`;
  const toDate = `${to}-${pad(daysInMonth(to))}`;
  const inRange = (iso) => iso >= fromDate && iso <= toDate;
  const stays = [...d.bookings.values()]
    .filter((b) => !b.cancelled && b.nightCount > 0 && inRange(b.checkIn))
    .sort((a, b) => (a.checkIn < b.checkIn ? -1 : 1));
  const cancelled = [...d.bookings.values()].filter((b) => b.cancelled && inRange(b.checkIn));

  return {
    listing: d.listing, from, to, keys, months, totals: t, stays, cancelled,
    incomplete: months.filter((m) => !isComplete(m)),
    rateBins: d.rateBins,
    tape: buildTape(model, d, keys),
    costs: buildCosts(model, d, keys, t, toDate),
    patterns: buildPatterns(d, stays, inRange),
    health: buildHealth(model, months, t),
  };
}

function isForSale(d, iso) {
  if (iso < d.listing.listedFrom) return false;
  return !d.blocked.some((b) => iso >= b.blocked_start_date && iso <= b.blocked_end_date);
}

function buildTape(model, d, keys) {
  return keys.map((key) => {
    const m = d.months.get(key);
    const [y, mo] = key.split("-").map(Number);
    const days = daysInMonth(key);
    const cells = [];
    const segments = [];
    let current = null;
    for (let day = 1; day <= days; day++) {
      const iso = `${key}-${pad(day)}`;
      const night = d.nightIndex.get(iso);
      const dow = new Date(y, mo - 1, day).getDay();
      cells.push({ day, iso, booked: Boolean(night), forSale: isForSale(d, iso),
                   future: iso > model.today, weekend: dow === 5 || dow === 6 });
      if (night && current && current.bookingId === night.bookingId && current.end === day - 1) {
        current.end = day;
      } else {
        if (current) segments.push(current);
        current = night ? { bookingId: night.bookingId, start: day, end: day } : null;
      }
    }
    if (current) segments.push(current);
    for (const s of segments) {
      const b = d.bookings.get(s.bookingId);
      s.booking = b;
      s.continuesFrom = s.start === 1 && b.firstNight < `${key}-01`;
      s.continuesTo = s.end === days && b.lastNight > `${key}-${pad(days)}`;
      s.bin = rateBin(d.rateBins, b.avgRate);
    }
    const sold = cells.filter((c) => c.booked).length;
    return {
      key, days, cells, segments, sold,
      available: m.available,
      occupancy: m.available ? sold / m.available : null,
      perNight: sold ? m.revenue / sold : null,
      complete: isComplete(m),
    };
  });
}

/** The rule in force on a date for a listing and category. */
function ruleOn(d, category, iso) {
  return d.rules.find((r) => r.category === category && r.valid_from <= iso &&
                             (!r.valid_to || r.valid_to > iso));
}

function buildCosts(model, d, keys, t, toDate) {
  const keySet = new Set(keys);
  const byCat = new Map();
  const get = (c) => {
    if (!byCat.has(c)) byCat.set(c, { category: c, share: 0, bill: 0, unverified: 0,
                                      types: new Set(), scopes: new Set() });
    return byCat.get(c);
  };
  for (const s of d.shares) {
    if (!keySet.has(s.key)) continue;
    const c = get(s.category);
    c.share += s.amount; c.unverified += s.unverified; c.types.add(s.type);
  }
  for (const b of d.bills) {
    if (!keySet.has(b.key)) continue;
    const c = get(b.category);
    c.bill += b.amount; c.scopes.add(b.scope);
  }

  const items = [];
  for (const c of byCat.values()) {
    if (Math.abs(c.share) < 0.005) continue;
    const info = model.categories.get(c.category) || {};
    const shared = c.scopes.has("property");
    const rule = shared ? ruleOn(d, c.category, toDate) : null;
    const notes = [];
    if (c.types.has("capital")) {
      notes.push(`Purchases are spread over ${info.capital_life_months || 60} months, so each month carries a part.`);
    } else if (info.spread_months > 1) {
      notes.push(`Each bill is spread over the ${info.spread_months} months it covers.`);
    }
    items.push({
      id: c.category, family: "expense", label: humanize(c.category),
      description: info.description, share: c.share, bill: Math.max(c.bill, c.share),
      shared, rate: shared && c.bill ? c.share / c.bill : 1,
      basis: rule?.basis?.replace(/^Example:\s*/i, ""), notes, unverified: c.unverified,
      capital: c.types.has("capital"),
    });
  }
  if (t.cleaning > 0) {
    const cost = d.listing.cleaningCost;
    const turnovers = cost ? Math.round(t.cleaning / cost) : null;
    items.push({
      id: "cleaning", family: "cleaning", label: "Cleaning", share: t.cleaning, bill: t.cleaning,
      shared: false, rate: 1, notes: [turnovers
        ? `${turnovers} turnovers at ${money(cost)} each: in-house cleaning, given a cost so profit isn’t flattered.`
        : "Cleaning costs recorded against stays."],
      description: "Cleaning after each stay",
    });
  }
  if (t.interest > 0) {
    const share = d.listing.loanShare;
    items.push({
      id: "interest", family: "interest", label: "Loan interest", share: t.interest,
      bill: share ? t.interest / share : t.interest, shared: Boolean(share), rate: share || 1,
      basis: share ? "the listing’s share of the property’s loans" : null,
      notes: ["Accrued day by day from each month’s interest charge."],
      description: "Interest on the property’s loans",
    });
  }
  items.sort((a, b) => b.share - a.share);
  return items;
}

const LENGTH_BUCKETS = ["1", "2", "3", "4", "5", "6", "7+"];
const LEAD_BUCKETS = [
  { label: "0–3", max: 3 }, { label: "4–7", max: 7 }, { label: "8–14", max: 14 },
  { label: "15–30", max: 30 }, { label: "31–90", max: 90 }, { label: "90+", max: Infinity },
];
// Southern hemisphere
const SEASONS = [
  { id: "summer", label: "Summer", months: [12, 1, 2], span: "Dec–Feb" },
  { id: "autumn", label: "Autumn", months: [3, 4, 5], span: "Mar–May" },
  { id: "winter", label: "Winter", months: [6, 7, 8], span: "Jun–Aug" },
  { id: "spring", label: "Spring", months: [9, 10, 11], span: "Sep–Nov" },
];

function median(values) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function buildPatterns(d, stays, inRange) {
  const length = LENGTH_BUCKETS.map((label) => ({ label, count: 0 }));
  for (const b of stays) length[Math.min(b.nightCount, 7) - 1].count++;

  const leads = stays.map((b) => b.leadDays).filter((v) => v != null);
  const lead = LEAD_BUCKETS.map((bk) => ({ label: bk.label, count: 0 }));
  for (const v of leads) lead[LEAD_BUCKETS.findIndex((bk) => v <= bk.max)].count++;

  const seasons = SEASONS.map((s) => ({ ...s, weekday: { n: 0, total: 0 }, weekend: { n: 0, total: 0 } }));
  for (const b of d.bookings.values()) {
    for (const n of b.nights) {
      if (!inRange(n.date)) continue;
      const dt = parseISO(n.date);
      const s = seasons.find((x) => x.months.includes(dt.getMonth() + 1));
      const slot = dt.getDay() === 5 || dt.getDay() === 6 ? s.weekend : s.weekday;
      slot.n++; slot.total += n.rate;
    }
  }
  for (const s of seasons) {
    s.weekdayRate = s.weekday.n ? s.weekday.total / s.weekday.n : null;
    s.weekendRate = s.weekend.n ? s.weekend.total / s.weekend.n : null;
  }

  return {
    length, lead, seasons,
    stays: stays.length,
    avgStay: stays.length ? sum(stays, (b) => b.nightCount) / stays.length : null,
    medianLead: median(leads),
  };
}

function buildHealth(model, months, t) {
  const f = model.freshness;
  const lastDay = (iso) => { const dt = parseISO(iso); dt.setDate(dt.getDate() - 1); return dt; };
  const keyOf = (dt) => `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}`;
  return {
    // A month's payouts are complete once an export dated after it is loaded
    payoutsThrough: f.latest_export ? keyOf(lastDay(f.latest_export)) : null,
    latestExport: f.latest_export,
    lastLoaded: f.last_loaded,
    // Interest for a month needs that month's charge and the next one's
    interestThrough: f.latest_interest ? addMonths(monthKey(f.latest_interest), -1) : null,
    receiptsToReview: f.receipts_to_review,
    unverified: t.unverified,
    incomplete: months.filter((m) => !isComplete(m)),
    checks: model.checks,
  };
}
