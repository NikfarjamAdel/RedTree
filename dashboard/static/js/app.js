// The page: loads the figures, keeps the listing and period, and renders
// every section against the same slice so the numbers always agree.

import {
  rateLegend, renderBridge, renderColumns, renderCosts, renderPnl, renderSeasons, renderTable, renderTape,
} from "./charts.js";
import {
  dateLabel, decimal1, money, monthLabel, number, percent, plural, rangeLabel,
} from "./format.js";
import { buildModel, buildView, isComplete, periodPresets } from "./model.js";
import { h, hideTip, periodMenu, setupViewToggles, statusIcon, themeSwitch } from "./ui.js";

const $ = (id) => document.getElementById(id);
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const state = { model: null, listingId: null, period: null, view: null, entered: false, target: "local" };

// ------------------------------------------------------------- loading
async function load({ refresh = false } = {}) {
  const main = $("main");
  const reload = $("reload-button");
  if (refresh) { main.classList.add("is-refreshing"); reload.classList.add("is-spinning"); }
  try {
    let res;
    try {
      res = await fetch("/api/dashboard", { cache: "no-store" });
    } catch {
      throw Object.assign(new Error("The dashboard server isn’t running."), { info: { error: "ServerDown" } });
    }
    const body = await res.json().catch(() => ({ error: "BadResponse", detail: `HTTP ${res.status}` }));
    if (!res.ok || body.error) throw Object.assign(new Error(body.detail || "Request failed"), { info: body });
    state.model = buildModel(body);
    if (!state.model.listings.length) throw Object.assign(new Error("No listings"), { info: { error: "NoListings" } });
    showError(null);
    chooseSelection();
    render();
  } catch (err) {
    showError(err);
  } finally {
    main.classList.remove("is-refreshing");
    reload.classList.remove("is-spinning");
  }
}

/** Keep what's selected when it still exists; otherwise the URL; otherwise defaults. */
function chooseSelection() {
  const { model } = state;
  const hash = new URLSearchParams(location.hash.slice(1));
  const wanted = state.listingId ?? Number(hash.get("listing"));
  state.listingId = model.data.has(wanted) ? wanted : model.listings[0].id;

  const d = model.data.get(state.listingId);
  const months = [...d.months.keys()].sort();
  const presets = periodPresets(d);
  const valid = (p) => p && months.includes(p.from) && months.includes(p.to) && p.from <= p.to;
  const fromHash = { id: hash.get("preset") || "custom", from: hash.get("from"), to: hash.get("to") };

  if (state.period?.id && state.period.id !== "custom") {
    state.period = presets.find((p) => p.id === state.period.id) || null;
  }
  if (!valid(state.period)) state.period = valid(fromHash) ? fromHash : null;
  if (!valid(state.period)) state.period = presets[0] || { id: "all", from: months[0], to: months.at(-1) };
}

function writeHash() {
  const p = new URLSearchParams({ listing: state.listingId, from: state.period.from, to: state.period.to,
                                  preset: state.period.id });
  history.replaceState(null, "", `#${p}`);
}

// --------------------------------------------------------------- render
function render() {
  hideTip();
  const { model } = state;
  const d = model.data.get(state.listingId);
  const v = buildView(model, state.listingId, state.period.from, state.period.to);
  state.view = v;

  renderTopbar(v, d);
  renderHero(v);
  renderLegends(v);
  renderKpis(v);
  renderHealth(v);
  renderTables(v);
  renderMeasured();
  $("guests-sub").textContent = v.patterns.stays
    ? `${plural(v.patterns.stays, "stay")} began in the period${v.cancelled.length ? `; ${plural(v.cancelled.length, "booking")} cancelled` : ", none cancelled"}.`
    : "No stays began in the period.";
  $("footer-text").textContent =
    `Read from the RedTree database at ${timeLabel(model.generatedAt)}. Money comes from the report views, so it matches the monthly report exactly.`;
  writeHash();
}

/** The charts that are drawn to their container's width. */
function renderMeasured() {
  const v = state.view;
  if (!v) return;
  renderBridge($("chart-bridge"), v, { entering: !state.entered && !reducedMotion });
  state.entered = true;
  renderPnl($("chart-months"), v);
  renderTape($("chart-tape"), v);
  renderCosts($("chart-costs"), v.costs);
  renderColumns($("chart-stay"), v.patterns.length, { title: "Length of stay", xNote: "nights" });
  renderColumns($("chart-lead"), v.patterns.lead, { title: "Booked ahead", xNote: "days before arrival" });
  renderSeasons($("chart-season"), v.patterns.seasons);
  $("stay-sub").textContent = v.patterns.avgStay ? `${decimal1(v.patterns.avgStay)} nights on average` : "";
  $("lead-sub").textContent = v.patterns.medianLead != null
    ? `Half of stays were booked ${plural(Math.round(v.patterns.medianLead), "day")} or less ahead` : "";
  $("season-sub").textContent = "Average nightly rate, Sun–Thu against Fri & Sat";
}

function renderTopbar(v, d) {
  const place = $("place");
  place.replaceChildren(
    h("span", { class: "place__property", text: v.listing.property }),
    h("span", { class: "place__sep", "aria-hidden": "true", text: "/" }),
    h("span", { class: "place__listing", text: v.listing.title }),
  );
  $("period-label").textContent = rangeLabel(v.from, v.to);
  document.title = `${v.listing.title} · RedTree dashboard`;

  const picker = $("listing-picker");
  picker.hidden = state.model.listings.length < 2;
  if (!picker.hidden) {
    const select = $("listing-select");
    select.replaceChildren(...state.model.listings.map((l) =>
      h("option", { value: l.id, selected: l.id === state.listingId, text: `${l.property} · ${l.title}` })));
  }

  menu.render(periodPresets(d), state.period, [...d.months.keys()].sort(),
              { range: rangeLabel, month: (k) => monthLabel(k) });
}

function renderHero(v) {
  const T = v.totals;
  const L = v.listing;
  $("hero-eyebrow").textContent = `Profit after interest · ${rangeLabel(v.from, v.to)}`;
  const title = $("hero-title");
  const strong = (text) => h("strong", { text });
  let lede = "";
  title.replaceChildren();
  if (!v.months.length) {
    title.append("No figures for this period.");
    lede = "Choose another period from the menu above.";
  } else if (T.revenue <= 0) {
    title.append(`${L.title} has no payouts in this period yet.`);
    lede = `Costs so far: ${money(T.expenses + T.cleaning + T.interest)}.`;
  } else if (T.kept >= 0) {
    title.append(`${L.title} kept `, strong(money(T.kept)), " of ", strong(money(T.revenue)), " in payouts.");
    lede = `That’s ${Math.round((T.kept / T.revenue) * 100)}¢ in every payout dollar, after expenses, cleaning` +
      (L.loanShare ? ` and its ${percent(L.loanShare)} share of the loan interest.` : ".");
  } else {
    title.append(`${L.title} fell `, strong(money(-T.kept)), " short after costs and loan interest.");
    lede = `Payouts of ${money(T.revenue)} didn’t cover expenses, cleaning` +
      (L.loanShare ? " and its share of the loan interest." : ".");
  }
  $("hero-lede").textContent = lede;

  const flags = $("hero-flags");
  flags.replaceChildren();
  if (v.incomplete.length) {
    const n = v.incomplete.length;
    flags.append(h("a", { class: "chip", href: "#health" }, statusIcon("warning"),
      n === 1 ? `${monthLabel(v.incomplete[0].key, { long: true })} is still incomplete`
              : `${n} months in this period are still incomplete`));
  }
  if (T.unverified > 0.5) {
    flags.append(h("a", { class: "chip", href: "#health" }, statusIcon("warning"),
      `${money(T.unverified)} of costs not yet checked by a person`));
  }
}

function legend(container, items) {
  container.replaceChildren(...items.map((it) => it.title
    ? h("span", { class: "legend__title", text: it.title })
    : h("span", { class: "legend__item" }, h("span", { class: `key key--${it.shape || "rect"} ${it.key || ""}` }), it.text)));
}

function renderLegends(v) {
  legend($("legend-months"), [
    { key: "k-payout", text: "Payouts" },
    { key: "k-expense", text: "Expenses" },
    { key: "k-cleaning", text: "Cleaning" },
    { key: "k-interest", text: "Loan interest" },
    { key: "k-kept", shape: "dot", text: "Profit after interest" },
    ...(v.incomplete.length ? [{ key: "k-ink2", shape: "faded", text: "Incomplete month" }] : []),
  ]);
  legend($("legend-nights"), [
    { title: "Nightly rate" },
    ...rateLegend(v.rateBins),
    { shape: "empty", text: "Not booked" },
  ]);
  legend($("legend-costs"), [
    { key: "k-expense", text: "Expenses" },
    { key: "k-cleaning", text: "Cleaning" },
    { key: "k-interest", text: "Loan interest" },
    { key: "k-track-expense", shape: "track", text: "Full bill, before splitting" },
  ]);
  legend($("legend-season"), [
    { key: "k-weekday", text: "Sun–Thu" },
    { key: "k-weekend", text: "Fri & Sat" },
  ]);
}

function renderKpis(v) {
  const T = v.totals;
  const P = v.patterns;
  const tiles = [
    { label: "Occupancy", value: T.available ? percent(T.nights / T.available) : "–",
      note: `${number(T.nights)} of ${number(T.available)} nights sold` },
    { label: "Payout per night sold", value: T.nights ? money(T.revenue / T.nights) : "–",
      note: "what a booked night brought in" },
    { label: "Payout per available night", value: T.available ? money(T.revenue / T.available) : "–",
      note: "spread over every night for sale" },
    { label: "Average stay", value: P.avgStay ? `${decimal1(P.avgStay)} nights` : "–",
      note: `${plural(P.stays, "stay")} began in the period` },
  ];
  $("kpis").replaceChildren(...tiles.map((t) => h("div", { class: "kpi" },
    h("div", { class: "kpi__label", text: t.label }),
    h("div", { class: "kpi__value", text: t.value }),
    h("div", { class: "kpi__note", text: t.note }))));
}

function renderHealth(v) {
  const H = v.health;
  const items = [];
  const item = (kind, text, meta) => items.push(h("li", { class: "health__item" }, statusIcon(kind),
    h("span", { class: "health__text", text }), meta ? h("span", { class: "health__meta" }, meta) : ""));
  const code = (text) => h("code", { text });

  if (H.payoutsThrough) {
    item(H.payoutsThrough >= v.to ? "good" : "warning",
      `Payouts complete through ${monthLabel(H.payoutsThrough, { long: true })}`,
      `Latest earnings export dated ${dateLabel(H.latestExport, { year: true, weekday: false })}.`);
  } else {
    item("warning", "No earnings exports loaded yet", "Upload an Airbnb earnings export to the exports container.");
  }
  if (v.listing.loanShare) {
    item(H.interestThrough && H.interestThrough >= v.to ? "good" : "warning",
      H.interestThrough ? `Loan interest entered through ${monthLabel(H.interestThrough, { long: true })}`
                        : "No loan interest entered yet",
      "A month is complete once its charge and the next month’s are entered.");
  }
  if (H.incomplete.length) {
    const names = H.incomplete.map((m) => monthLabel(m.key, { year: false }));
    item("warning", `${plural(H.incomplete.length, "month")} in this period still incomplete`,
      names.length > 6 ? `${names.slice(0, 6).join(", ")} and ${names.length - 6} more.` : `${names.join(", ")}.`);
  } else {
    item("good", "Every month in this period is complete");
  }
  if (H.unverified > 0.5) {
    item("warning", `${money(H.unverified)} of costs not yet checked by a person`,
      ["Audit them with ", code("python python/review_receipts.py local --audit"), "."]);
  } else {
    item("good", "Every cost in this period has been checked by a person");
  }
  if (H.receiptsToReview) {
    item("warning", `${plural(H.receiptsToReview, "receipt")} waiting for review`,
      ["Decide them with ", code("python python/review_receipts.py local"), "."]);
  } else {
    item("good", "No receipts waiting for review");
  }
  $("health-list").replaceChildren(...items);

  $("checks-list").replaceChildren(...H.checks.map((c) => h("li", {
    class: `check${c.failing_rows ? " is-failing" : ""}` },
    statusIcon(c.failing_rows ? "critical" : "good"),
    h("span", { text: c.title }),
    h("span", { class: "check__state", text: c.failing_rows ? plural(c.failing_rows, "problem") : "Passing" }))));
}

// --------------------------------------------------------------- tables
function statusCell(m) {
  const kind = isComplete(m) ? "good" : !m.revenueComplete ? "pending" : "warning";
  const text = isComplete(m) ? "Complete" : !m.revenueComplete ? "Waiting for export" : "Interest missing";
  return h("span", { class: "status" }, statusIcon(kind), text);
}

function statementTable(container, v, { withStatus = true } = {}) {
  const T = v.totals;
  const cols = [
    { label: "Month", value: (m) => monthLabel(m.key), cls: "text" },
    { label: "Nights sold", value: (m) => number(m.nights) },
    { label: "Occupancy", value: (m) => (m.available ? percent(m.nights / m.available) : "–") },
    { label: "Payouts", value: (m) => money(m.revenue) },
    { label: "Expenses", value: (m) => money(m.expenses) },
    { label: "Cleaning", value: (m) => money(m.cleaning) },
    { label: "Operating profit", value: (m) => money(m.operating) },
    { label: "Loan interest", value: (m) => money(m.interest) },
    { label: "Profit after interest", value: (m) => money(m.kept), cls: "is-strong" },
  ];
  if (withStatus) cols.push({ label: "Status", value: statusCell, cls: "text" });
  const foot = ["Total", number(T.nights), T.available ? percent(T.nights / T.available) : "–",
    money(T.revenue), money(T.expenses), money(T.cleaning), money(T.operating), money(T.interest), money(T.kept)];
  if (withStatus) foot.push(v.incomplete.length ? `${v.incomplete.length} incomplete` : "All complete");
  renderTable(container, {
    caption: `${v.listing.title}, ${rangeLabel(v.from, v.to)}. Costs are shown as positive amounts.`,
    columns: cols, rows: v.months, foot,
    rowClass: (m) => (isComplete(m) ? "" : "is-incomplete"),
  });
}

function renderTables(v) {
  statementTable($("table-statement"), v);
  statementTable($("table-months"), v, { withStatus: false });

  renderTable($("table-stays"), {
    caption: `${plural(v.stays.length, "stay")} that began in the period.`,
    columns: [
      { label: "Check-in", value: (b) => dateLabel(b.checkIn, { year: true }), cls: "text" },
      { label: "Guest", value: (b) => b.guest, cls: "text" },
      { label: "Nights", value: (b) => number(b.nightCount) },
      { label: "Nightly rate", value: (b) => money(b.avgRate) },
      { label: "Payout", value: (b) => (b.net != null ? money(b.net) : "–") },
      { label: "Booked ahead", value: (b) => (b.leadDays != null ? plural(b.leadDays, "day") : "–") },
      { label: "Code", value: (b) => h("span", { class: "mono", text: b.code }), cls: "text" },
    ],
    rows: v.stays,
  });

  renderTable($("table-costs"), {
    caption: "Shares are this listing’s; full bills are before splitting.",
    columns: [
      { label: "Cost", value: (c) => c.label, cls: "text" },
      { label: "This listing’s share", value: (c) => money(c.share), cls: "is-strong" },
      { label: "Full bill", value: (c) => (c.shared ? money(c.bill) : "–") },
      { label: "Share", value: (c) => (c.shared ? percent(c.rate) : "100%") },
      { label: "Split by", value: (c) => c.basis || (c.shared ? "–" : "Not shared"), cls: "text" },
    ],
    rows: v.costs,
    foot: ["Total", money(v.costs.reduce((a, c) => a + c.share, 0)), "", "", ""],
  });

  const P = v.patterns;
  const holder = $("table-guests");
  const a = h("div");
  const b = h("div");
  const c = h("div");
  holder.replaceChildren(a, b, c);
  renderTable(a, { caption: "Length of stay",
    columns: [{ label: "Nights", value: (r) => r.label, cls: "text" }, { label: "Stays", value: (r) => number(r.count) }],
    rows: P.length });
  renderTable(b, { caption: "Booked ahead",
    columns: [{ label: "Days before arrival", value: (r) => r.label, cls: "text" }, { label: "Stays", value: (r) => number(r.count) }],
    rows: P.lead });
  renderTable(c, { caption: "Average nightly rate by season",
    columns: [
      { label: "Season", value: (r) => `${r.label} (${r.span})`, cls: "text" },
      { label: "Sun–Thu", value: (r) => (r.weekdayRate ? money(r.weekdayRate) : "–") },
      { label: "Nights", value: (r) => number(r.weekday.n) },
      { label: "Fri & Sat", value: (r) => (r.weekendRate ? money(r.weekendRate) : "–") },
      { label: "Nights", value: (r) => number(r.weekend.n) },
    ],
    rows: P.seasons });
}

// ------------------------------------------------------------------ CSV
function downloadCsv() {
  const v = state.view;
  if (!v) return;
  const header = ["month", "nights_sold", "available_nights", "guests_paid", "platform_fee", "payouts",
    "expenses", "cleaning", "operating_profit", "loan_interest", "profit_after_interest", "complete"];
  const n2 = (x) => (Number(x) || 0).toFixed(2);
  const rows = v.months.map((m) => [m.key, m.nights, m.available, n2(m.revenue + m.fee), n2(m.fee), n2(m.revenue),
    n2(m.expenses), n2(m.cleaning), n2(m.operating), n2(m.interest), n2(m.kept), isComplete(m) ? "yes" : "no"]);
  const cell = (x) => (/[",\n]/.test(String(x)) ? `"${String(x).replace(/"/g, '""')}"` : String(x));
  const csv = [header, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const slug = v.listing.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "listing";
  const url = URL.createObjectURL(new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" }));
  const a = h("a", { href: url, download: `redtree-${slug}-${v.from}-to-${v.to}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------- error
function showError(err) {
  const box = $("hero-error");
  const sections = $("sections");
  if (!err) {
    box.hidden = true;
    sections.hidden = false;
    return;
  }
  const info = err.info || {};
  const serverDown = info.error === "ServerDown";
  const local = (info.target || "local") === "local";
  $("hero-title").textContent = "Can’t read the figures right now";
  $("hero-lede").textContent = "";
  $("hero-flags").replaceChildren();
  $("chart-bridge").replaceChildren();
  sections.hidden = true;

  const code = (text) => h("code", { text });
  let heading;
  let advice;
  if (serverDown) {
    heading = "The dashboard server has stopped";
    advice = ["Start it again with ", code("python dashboard/server.py"), ", then try again."];
  } else if (info.error === "NoListings") {
    heading = "There are no listings in the database yet";
    advice = ["Load your property with ", code("python python/setup_property.py"),
              ", or build the demo with ", code("python python/build_db.py --demo --yes"), "."];
  } else if (local) {
    heading = "The local database isn’t answering";
    advice = ["Open Docker Desktop and start SQL Server with ", code("docker start sqlserver"), ", then try again."];
  } else {
    heading = "Azure SQL isn’t answering";
    advice = ["A paused database can take a minute to wake up. Try again shortly."];
  }
  box.replaceChildren(
    h("h2", { text: heading }),
    h("p", {}, advice),
    info.detail ? h("details", {}, h("summary", { text: "Technical details" }),
                    h("p", { text: `${info.error}: ${info.detail}` })) : "",
    h("button", { class: "btn", type: "button", text: "Try again", onclick: () => load({ refresh: true }) }),
  );
  box.hidden = false;
}

function timeLabel(iso) {
  if (!iso) return "";
  const [date, time = ""] = iso.split("T");
  return `${time.slice(0, 5)} on ${dateLabel(date, { year: true, weekday: false })}`;
}

// ---------------------------------------------------------------- wiring
const menu = periodMenu({
  button: $("period-button"),
  menu: $("period-menu"),
  onSelect: (p) => { state.period = p; render(); },
});

$("listing-select").addEventListener("change", (e) => {
  state.listingId = Number(e.target.value);
  state.period = null;
  chooseSelection();
  render();
});
$("reload-button").addEventListener("click", () => load({ refresh: true }));
$("download-csv").addEventListener("click", downloadCsv);
setupViewToggles(() => renderMeasured());
themeSwitch($("theme-button"));

// Redraw the measured charts when their width changes (not on every pixel of height)
let lastWidth = 0;
let frame = 0;
new ResizeObserver((entries) => {
  const width = Math.round(entries[0].contentRect.width);
  if (width === lastWidth) return;
  lastWidth = width;
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(() => { hideTip(); renderMeasured(); });
}).observe($("sections"));

load();
