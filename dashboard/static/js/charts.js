// Hand-drawn SVG charts, measured to their container's real width (no
// scaling, so text stays its true size). Mark specs follow the dataviz
// method: bars <= 24px with a 4px rounded data end and a square base, a 2px
// surface gap between touching marks, 2px lines, >= 8px dots with a surface
// ring, hairline solid gridlines, selective direct labels, and a tooltip on
// every mark, reachable by keyboard as well as by pointer.

import {
  axisMoney, dateLabel, initials, money, monthLabel, number, outflow, percent, plural,
} from "./format.js";
import { rateBinLabels } from "./model.js";
import { applyStyle, bindTip, h, rove } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "style") applyStyle(el, v);
    else el.setAttribute(k, v);
  }
  if (parent) parent.append(el);
  return el;
}
function label(parent, x, y, text, attrs = {}) {
  const el = s("text", { x, y, ...attrs }, parent);
  el.textContent = text;
  return el;
}
function root(container, width, height, ariaLabel) {
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
                         role: "group", "aria-label": ariaLabel });
  container.replaceChildren(svg);
  return svg;
}
const textWidth = (str, size = 12) => String(str).length * size * 0.56;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function niceStep(range, count) {
  const raw = range / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}
function ticks(min, max, count = 5) {
  if (min === max) max = min + 1;
  const step = niceStep(max - min, count);
  const out = [];
  for (let v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + step / 2; v += step) {
    out.push(Math.round(v * 1e6) / 1e6);
  }
  return out;
}

/** A vertical bar from its baseline to its data end: rounded end, square base. */
function vbar(x, base, end, w, r = 4) {
  const hgt = Math.abs(end - base);
  if (hgt < 0.5) return "";
  r = Math.min(r, hgt, w / 2);
  return end < base
    ? `M${x},${base}V${end + r}A${r},${r} 0 0 1 ${x + r},${end}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${end + r}V${base}Z`
    : `M${x},${base}V${end - r}A${r},${r} 0 0 0 ${x + r},${end}H${x + w - r}A${r},${r} 0 0 0 ${x + w},${end - r}V${base}Z`;
}
/** A horizontal bar from its baseline (left) to its data end. */
function hbar(base, end, y, hgt, r = 4) {
  const w = Math.abs(end - base);
  if (w < 0.5) return "";
  r = Math.min(r, w, hgt / 2);
  return end > base
    ? `M${base},${y}H${end - r}A${r},${r} 0 0 1 ${end},${y + r}V${y + hgt - r}A${r},${r} 0 0 1 ${end - r},${y + hgt}H${base}Z`
    : `M${base},${y}H${end + r}A${r},${r} 0 0 0 ${end},${y + r}V${y + hgt - r}A${r},${r} 0 0 0 ${end + r},${y + hgt}H${base}Z`;
}
/** A rectangle with its own radius per corner: [top-left, top-right, bottom-right, bottom-left]. */
function rrect(x, y, w, hgt, radii) {
  const m = Math.min(w / 2, hgt / 2);
  const [tl, tr, br, bl] = radii.map((r) => Math.max(0, Math.min(r, m)));
  return `M${x + tl},${y}H${x + w - tr}${tr ? `A${tr},${tr} 0 0 1 ${x + w},${y + tr}` : ""}` +
         `V${y + hgt - br}${br ? `A${br},${br} 0 0 1 ${x + w - br},${y + hgt}` : ""}` +
         `H${x + bl}${bl ? `A${bl},${bl} 0 0 1 ${x},${y + hgt - bl}` : ""}` +
         `V${y + tl}${tl ? `A${tl},${tl} 0 0 1 ${x + tl},${y}` : ""}Z`;
}

function empty(container, text) {
  container.replaceChildren(h("p", { class: "empty", text }));
}

// ================================================================ bridge
/** From what guests paid to what was left after loan interest. */
export function bridgeSteps(v) {
  const T = v.totals;
  const steps = [
    { id: "gross", label: "Guests paid", value: T.gross, kind: "total", cls: "m-neutral", key: "k-neutral",
      note: "What guests paid for their stays, before the platform’s fee." },
    { id: "fee", label: "Platform fee", value: -T.fee, kind: "delta", cls: "m-fee", key: "k-fee",
      note: "Kept by Airbnb. Never revenue and never an expense: it doesn’t reach the bank." },
    { id: "payouts", label: "Payouts", value: T.revenue, kind: "total", cls: "m-payout", key: "k-payout",
      note: "What reached the bank, counted in the month each night was stayed." },
    { id: "expenses", label: "Expenses", value: -T.expenses, kind: "delta", cls: "m-expense", key: "k-expense",
      note: "This listing’s share of every bill, with shared bills split and big purchases spread." },
    { id: "cleaning", label: "Cleaning", value: -T.cleaning, kind: "delta", cls: "m-cleaning", key: "k-cleaning",
      note: "A cost for every turnover, including in-house cleaning." },
    { id: "operating", label: "Operating profit", value: T.operating, kind: "total", cls: "m-neutral",
      key: "k-neutral", note: "Payouts less expenses and cleaning." },
  ];
  if (T.interest || v.listing.loanShare) {
    steps.push(
      { id: "interest", label: "Loan interest", value: -T.interest, kind: "delta", cls: "m-interest",
        key: "k-interest", note: v.listing.loanShare
          ? `This listing’s ${percent(v.listing.loanShare)} share of the property’s loan interest, accrued by day.`
          : "Loan interest, accrued by day." },
      { id: "kept", label: "Profit after interest", value: T.kept, kind: "total", cls: "m-kept",
        key: "k-kept", strong: true, note: "Operating profit less loan interest: what the listing made." },
    );
  } else {
    Object.assign(steps[steps.length - 1], { cls: "m-kept", key: "k-kept", strong: true });
  }
  let run = 0;
  for (const st of steps) {
    if (st.kind === "total") { st.from = 0; st.to = st.value; }
    else { st.from = run; st.to = run + st.value; }
    run = st.to;
  }
  return steps;
}

export function renderBridge(container, v, { entering = false } = {}) {
  if (!v.months.length) return empty(container, "No figures in this period.");
  const steps = bridgeSteps(v);
  const W = Math.max(280, container.clientWidth);
  const lo = Math.min(0, ...steps.flatMap((st) => [st.from, st.to]));
  const hi = Math.max(1, ...steps.flatMap((st) => [st.from, st.to]));
  const tipFor = (st) => () => ({
    title: st.label,
    rows: [
      { key: st.key, shape: "rect", value: st.kind === "delta" ? outflow(st.value) : money(st.value),
        label: st.kind === "delta" && v.totals.revenue > 0
          ? `${percent(Math.abs(st.value) / v.totals.revenue)} of payouts`
          : st.id === "kept" && v.totals.revenue > 0 ? `${percent(st.value / v.totals.revenue)} of payouts` : "" },
    ],
    note: st.note,
  });

  return W < 600 ? bridgeRows(container, steps, W, lo, hi, tipFor, entering)
                 : bridgeColumns(container, steps, W, lo, hi, tipFor, entering);
}

function bridgeColumns(container, steps, W, lo, hi, tipFor, entering) {
  const H = clamp(Math.round(W * 0.26), 250, 320);
  const m = { top: 30, bottom: 46 };
  const plotH = H - m.top - m.bottom;
  const y = (val) => m.top + ((hi - val) / (hi - lo)) * plotH;
  const band = W / steps.length;
  const barW = 24;
  const svg = root(container, W, H, "Profit bridge");
  if (entering) svg.classList.add("is-entering");

  s("line", { x1: 0, x2: W, y1: y(0), y2: y(0), class: "baseline" }, svg);

  const groups = [];
  steps.forEach((st, i) => {
    const cx = band * (i + 0.5);
    const x = cx - barW / 2;
    const g = s("g", { class: "bridge-step", style: `--i:${i}`, role: "img",
      "aria-label": `${st.label}: ${st.kind === "delta" ? outflow(st.value) : money(st.value)}` }, svg);
    s("rect", { x: band * i + 2, y: 4, width: band - 4, height: H - 8, rx: 10, class: "hit" }, g);

    if (st.kind === "total") {
      s("path", { d: vbar(x, y(0), y(st.to), barW), class: `bridge-bar ${st.cls} ${st.to >= 0 ? "from-bottom" : "from-top"}` }, g);
    } else {
      const top = y(Math.max(st.from, st.to));
      const bottom = y(Math.min(st.from, st.to));
      if (bottom - top >= 0.5) {
        s("path", { d: rrect(x, top, barW, bottom - top, [3, 3, 3, 3]), class: `bridge-bar ${st.cls} from-top` }, g);
      }
    }
    // the running total carries on to the next step
    if (i < steps.length - 1) {
      s("line", { x1: x + barW, x2: band * (i + 1.5) - barW / 2, y1: y(st.to), y2: y(st.to),
                  class: "connector", style: `--i:${i}` }, svg);
    }
    // value: above a total, below a deduction
    const text = st.kind === "delta" ? outflow(st.value) : money(st.value);
    const above = st.kind === "total" ? st.to >= 0 : false;
    const vy = above ? y(Math.max(st.from, st.to)) - 10 : y(Math.min(st.from, st.to)) + 19;
    label(g, cx, vy, text, { class: `bridge-value ${st.kind === "delta" ? "bridge-value--delta" : ""}`,
                             "text-anchor": "middle", style: `--i:${i}` });
    // step name, on two lines when it won't fit the band
    const words = st.label.split(" ");
    const lines = textWidth(st.label, 12.5) > band - 10 && words.length > 1
      ? [words.slice(0, Math.ceil(words.length / 2)).join(" "), words.slice(Math.ceil(words.length / 2)).join(" ")]
      : [st.label];
    lines.forEach((line, j) => label(g, cx, H - m.bottom + 22 + j * 15, line,
      { class: `bridge-label ${st.strong ? "bridge-label--strong" : ""}`, "text-anchor": "middle" }));
    bindTip(g, tipFor(st));
    groups.push(g);
  });
  rove(groups, "Profit bridge. Use the arrow keys to move between steps.");
}

function bridgeRows(container, steps, W, lo, hi, tipFor, entering) {
  const rowH = 40;
  const nameW = 112;
  const valueW = 76;
  const plotX = nameW;
  const plotW = W - nameW - valueW - 8;
  const H = steps.length * rowH + 6;
  const x = (val) => plotX + ((val - lo) / (hi - lo)) * plotW;
  const svg = root(container, W, H, "Profit bridge");
  if (entering) svg.classList.add("is-entering");
  s("line", { x1: x(0), x2: x(0), y1: 0, y2: H, class: "baseline" }, svg);

  const groups = [];
  steps.forEach((st, i) => {
    const cy = i * rowH + rowH / 2;
    const barH = 16;
    const g = s("g", { class: "bridge-step", style: `--i:${i}`, role: "img",
      "aria-label": `${st.label}: ${st.kind === "delta" ? outflow(st.value) : money(st.value)}` }, svg);
    s("rect", { x: 0, y: i * rowH + 2, width: W, height: rowH - 4, rx: 8, class: "hit" }, g);
    if (st.kind === "total") {
      s("path", { d: hbar(x(0), x(st.to), cy - barH / 2, barH), class: `bridge-bar ${st.cls} from-left` }, g);
    } else {
      const left = x(Math.min(st.from, st.to));
      const right = x(Math.max(st.from, st.to));
      if (right - left >= 0.5) {
        s("path", { d: rrect(left, cy - barH / 2, right - left, barH, [3, 3, 3, 3]), class: `bridge-bar ${st.cls} from-left` }, g);
      }
    }
    if (i < steps.length - 1) {
      s("line", { x1: x(st.to), x2: x(st.to), y1: cy + barH / 2, y2: cy + rowH - barH / 2,
                  class: "connector", style: `--i:${i}` }, svg);
    }
    label(g, 0, cy + 4, st.label, { class: `bridge-label ${st.strong ? "bridge-label--strong" : ""}` });
    label(g, W, cy + 5, st.kind === "delta" ? outflow(st.value) : money(st.value),
      { class: `bridge-value ${st.kind === "delta" ? "bridge-value--delta" : ""}`, "text-anchor": "end",
        style: `--i:${i}` });
    bindTip(g, tipFor(st));
    groups.push(g);
  });
  rove(groups, "Profit bridge. Use the arrow keys to move between steps.");
}

// ========================================================= month by month
export function renderPnl(container, v) {
  const months = v.months;
  if (!months.length) return empty(container, "No months in this period. Choose another period.");
  const W = Math.max(300, container.clientWidth);
  const compact = W < 640;
  const H = compact ? 270 : 330;
  const m = { top: 22, right: 4, bottom: 44, left: compact ? 44 : 56 };
  const plotW = W - m.left - m.right;
  const plotH = H - m.top - m.bottom;
  const out = (x) => x.expenses + x.cleaning + x.interest;

  const hiRaw = Math.max(0, ...months.map((x) => Math.max(x.revenue, x.kept)));
  const loRaw = Math.min(0, ...months.map((x) => Math.min(-out(x), x.kept)));
  const tk = ticks(loRaw, hiRaw, compact ? 4 : 6);
  const lo = tk[0];
  const hi = tk[tk.length - 1];
  const fmt = axisMoney(Math.max(Math.abs(lo), Math.abs(hi)));
  const y = (val) => m.top + ((hi - val) / (hi - lo)) * plotH;

  const svg = root(container, W, H, "Payouts, costs and profit after interest by month");
  for (const t of tk) {
    if (t !== 0) s("line", { x1: m.left, x2: W - m.right, y1: y(t), y2: y(t), class: "gridline" }, svg);
    label(svg, m.left - 10, y(t) + 4, fmt(t), { class: "axis-text", "text-anchor": "end" });
  }

  const band = plotW / months.length;
  const barW = Math.min(24, band * 0.58);
  const every = band >= 34 ? 1 : band >= 18 ? 2 : 3;
  const groups = [];
  const points = [];
  const best = months.filter((x) => x.revenueComplete && !x.interestMissing)
    .reduce((a, b) => (!a || b.kept > a.kept ? b : a), null);

  months.forEach((mo, i) => {
    const cx = m.left + band * (i + 0.5);
    const x = cx - barW / 2;
    const complete = mo.revenueComplete && !mo.interestMissing;
    const g = s("g", { class: `pnl-month${complete ? "" : " is-incomplete"}`, role: "img",
      "aria-label": `${monthLabel(mo.key, { long: true })}: payouts ${money(mo.revenue)}, costs ${money(out(mo))}, profit after interest ${money(mo.kept)}${complete ? "" : ", incomplete"}` }, svg);
    s("rect", { x: m.left + band * i, y: m.top - 8, width: band, height: plotH + 16, rx: 6, class: "hit" }, g);

    // money in, above the line (1px of the 2px surface gap each side of zero)
    if (mo.revenue > 0) s("path", { d: vbar(x, y(0) - 1, y(mo.revenue), barW), class: "bar m-payout" }, g);
    // money out, stacked below: expenses, cleaning, interest
    const parts = [["m-expense", mo.expenses], ["m-cleaning", mo.cleaning], ["m-interest", mo.interest]]
      .filter(([, val]) => val > 0);
    let acc = 0;
    parts.forEach(([cls, val], j) => {
      const top = y(-acc) + 1;
      const bottom = y(-(acc + val)) - (j < parts.length - 1 ? 1 : 0);
      acc += val;
      if (bottom - top < 0.5) return;
      const d = j === parts.length - 1 ? vbar(x, top, bottom, barW) : `M${x},${top}H${x + barW}V${bottom}H${x}Z`;
      s("path", { d, class: `bar ${cls}` }, g);
    });

    points.push({ x: cx, y: y(mo.kept), complete, mo });
    if (i % every === 0) {
      label(g, cx, H - m.bottom + 18, monthLabel(mo.key, { year: false }),
        { class: "axis-text", "text-anchor": "middle" });
      if (i === 0 || mo.key.endsWith("-01")) {
        label(g, cx, H - m.bottom + 33, mo.key.slice(0, 4), { class: "axis-text axis-text--strong", "text-anchor": "middle" });
      }
    }
    bindTip(g, () => ({
      title: monthLabel(mo.key, { long: true }),
      sub: complete ? `${mo.nights} of ${mo.available} nights sold`
        : !mo.revenueComplete ? "Incomplete: waiting for the next earnings export"
        : "Incomplete: loan interest not yet entered",
      rows: [
        { key: "k-payout", shape: "rect", value: money(mo.revenue), label: "Payouts" },
        { key: "k-expense", shape: "rect", value: outflow(mo.expenses), label: "Expenses" },
        { key: "k-cleaning", shape: "rect", value: outflow(mo.cleaning), label: "Cleaning" },
        { key: "k-interest", shape: "rect", value: outflow(mo.interest), label: "Loan interest" },
        { key: "k-kept", shape: "line", value: money(mo.kept), label: "Profit after interest", total: true },
      ],
    }));
    groups.push(g);
  });

  s("line", { x1: m.left, x2: W - m.right, y1: y(0), y2: y(0), class: "baseline" }, svg);
  const line = s("path", { d: points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(""),
                           class: "profit-line" }, svg);
  line.setAttribute("aria-hidden", "true");
  for (const p of points) {
    s("circle", { cx: p.x, cy: p.y, r: 4.5, class: `profit-dot${p.complete ? "" : " is-incomplete"}`,
                  "pointer-events": "none" }, svg);
  }
  // one direct label: the best complete month
  if (best && best.kept > 0) {
    const p = points.find((pt) => pt.mo === best);
    const right = p.x + 12 + textWidth(money(best.kept), 12.5) < W - m.right;
    label(svg, right ? p.x + 10 : p.x - 10, p.y - 9, money(best.kept),
      { class: "value-label value-label--halo", "text-anchor": right ? "start" : "end", "pointer-events": "none" });
  }
  rove(groups, "Months. Use the arrow keys to move between months.");
}

// =========================================================== night by night
export function renderTape(container, v) {
  const rows = v.tape;
  if (!rows.length) return empty(container, "No months in this period.");
  const W = Math.max(300, container.clientWidth);
  const compact = W < 720;
  const labelW = compact ? 34 : 70;
  const statsW = compact ? 40 : 228;
  const rowH = compact ? 24 : 30;
  const barH = compact ? 14 : 18;
  const headH = 24;
  const gridX = labelW + 6;
  const gridW = W - gridX - statsW - (compact ? 6 : 18);
  const col = gridW / 31;
  const H = headH + rows.length * rowH + 2;
  const svg = root(container, W, H, "Nights sold, by month and day");

  const dayTicks = col >= 22 ? Array.from({ length: 31 }, (_, i) => i + 1)
    : compact ? [1, 8, 15, 22, 29] : [1, 5, 10, 15, 20, 25, 31];
  for (const d of dayTicks) {
    label(svg, gridX + col * (d - 0.5), 13, String(d), { class: "tape-head", "text-anchor": "middle" });
  }
  const sx = W - statsW + 14;
  if (!compact) {
    label(svg, sx, 13, "Sold", { class: "tape-head" });
    label(svg, sx + 136, 13, "Occupancy", { class: "tape-head", "text-anchor": "end" });
    label(svg, W, 13, "Per night", { class: "tape-head", "text-anchor": "end" });
  } else {
    label(svg, W, 13, "Occ.", { class: "tape-head", "text-anchor": "end" });
  }

  const stays = [];
  rows.forEach((r, i) => {
    const top = headH + i * rowH;
    const cy = top + rowH / 2;
    const row = s("g", { class: "tape-row" }, svg);
    s("rect", { x: 0, y: top + 1, width: W, height: rowH - 2, rx: 6, class: "tape-row__bg" }, row);

    const showYear = i === 0 || r.key.endsWith("-01");
    label(row, 0, cy + 4, monthLabel(r.key, { year: false }), { class: "tape-month" });
    if (showYear && !compact) label(row, 30, cy + 4, r.key.slice(0, 4), { class: "tape-year" });

    for (const c of r.cells) {
      if (!c.forSale || c.booked) continue;
      const x = gridX + col * (c.day - 1) + 1;
      const cls = `cell${c.future ? " is-future" : c.weekend ? " is-weekend" : ""}`;
      s("rect", { x, y: cy - barH / 2, width: Math.max(1, col - 2), height: barH,
                  rx: Math.min(3, (col - 2) / 2), class: cls }, row);
    }

    for (const seg of r.segments) {
      const b = seg.booking;
      const left = gridX + col * (seg.start - 1) + (seg.continuesFrom ? 0 : 1);
      const right = gridX + col * seg.end - (seg.continuesTo ? 0 : 1);
      const w = right - left;
      const g = s("g", { class: "stay", role: "img",
        "aria-label": `${b.guest}, ${dateLabel(b.checkIn)} to ${dateLabel(b.checkOut)}, ${plural(b.nightCount, "night")}, ${money(b.avgRate)} a night` }, row);
      s("rect", { x: left, y: top, width: w, height: rowH, class: "hit" }, g);
      s("path", { d: rrect(left, cy - barH / 2, w, barH,
        [seg.continuesFrom ? 0 : 4, seg.continuesTo ? 0 : 4, seg.continuesTo ? 0 : 4, seg.continuesFrom ? 0 : 4]),
        class: `stay__bar rate-${seg.bin}` }, g);
      const ini = initials(b.guest);
      if (!compact && w >= 28 && ini) {
        label(g, left + w / 2, cy + 3.8, ini, { class: `stay__initials rate-ink-${seg.bin}`, "text-anchor": "middle" });
      }
      bindTip(g, () => stayTip(b));
      stays.push(g);
    }

    // the month's figures
    if (!compact) {
      label(row, sx, cy + 4, `${r.sold}/${r.available}`, { class: "tape-stat" });
      const meterX = sx + 46;
      const meterW = 50;
      s("rect", { x: meterX, y: cy - 2, width: meterW, height: 4, rx: 2, class: "meter-track" }, row);
      if (r.occupancy) {
        s("rect", { x: meterX, y: cy - 2, width: Math.max(2, meterW * Math.min(1, r.occupancy)), height: 4,
                    rx: 2, class: "meter-fill" }, row);
      }
      label(row, sx + 136, cy + 4, percent(r.occupancy), { class: "tape-stat tape-stat--strong", "text-anchor": "end" });
      label(row, W, cy + 4, r.perNight ? money(r.perNight) : "–", { class: "tape-stat", "text-anchor": "end" });
    } else {
      label(row, W, cy + 4, percent(r.occupancy), { class: "tape-stat tape-stat--strong", "text-anchor": "end" });
    }
  });
  rove(stays, "Stays. Use the arrow keys to move between stays.");
}

function stayTip(b) {
  const booked = b.checkOut ? Math.round((new Date(b.checkOut) - new Date(b.checkIn)) / 86400000) : b.nightCount;
  return {
    title: b.guest,
    sub: `${dateLabel(b.checkIn, { year: true })} → ${dateLabel(b.checkOut)}`,
    rows: [
      { value: plural(b.nightCount, "night"), label: booked !== b.nightCount ? `stayed (booked ${booked})` : "stayed" },
      { value: money(b.avgRate), label: "nightly rate" },
      { value: b.net != null ? money(b.net) : "–", label: "payout" },
      { value: b.leadDays != null ? plural(b.leadDays, "day") : "–", label: "booked ahead" },
    ],
    code: b.code,
  };
}

export function rateLegend(bins) {
  return rateBinLabels(bins).map((text, i) => ({ key: `k-rate-${i + 1}`, text }));
}

// ======================================================== where it goes
export function renderCosts(list, items) {
  if (!items.length) {
    list.replaceChildren(h("li", { class: "empty", text: "No costs in this period." }));
    return;
  }
  // The pale track shows a shared bill's full size. Loan interest has no
  // track: the property's whole interest would set the scale and shrink
  // every other row to a sliver; its text still gives the full amount.
  const track = (it) => it.shared && it.family === "expense";
  const max = Math.max(...items.map((it) => it.share), ...items.filter(track).map((it) => it.bill));
  list.replaceChildren(...items.map((it) => {
    const meta = it.shared
      ? `${percent(it.rate)} of ${money(it.bill)}`
      : it.capital ? "Spread over its useful life" : it.family === "cleaning" ? "Every turnover" : "This listing only";
    const li = h("li", { class: `cost f-${it.family}`, tabindex: "0",
                         "aria-label": `${it.label}: ${money(it.share)}${it.shared ? `, ${meta}` : ""}` },
      h("div", { class: "cost__name" }, it.label, h("span", { class: "cost__meta", text: meta })),
      h("div", { class: "cost__bar", "aria-hidden": "true" },
        track(it) ? h("span", { class: "cost__track", style: `width:${(it.bill / max) * 100}%` }) : "",
        h("span", { class: "cost__fill", style: `width:${Math.max(0.6, (it.share / max) * 100)}%` })),
      h("div", { class: "cost__value", text: money(it.share) }),
    );
    bindTip(li, () => ({
      title: it.label,
      sub: it.description,
      rows: [
        { key: `k-${it.family}`, shape: "rect", value: money(it.share), label: "this listing’s share" },
        ...(it.shared ? [{ key: `k-track-${it.family}`, shape: "rect",
                           value: money(it.bill), label: "full bill" }] : []),
      ],
      note: [it.shared && it.basis ? `Split ${percent(it.rate)} by ${it.basis}.` : "", ...(it.notes || [])]
        .filter(Boolean).join(" "),
    }));
    return li;
  }));
}

// ===================================================== small columns
export function renderColumns(container, bins, { title, unit = "stay", xNote = "" } = {}) {
  const total = bins.reduce((a, b) => a + b.count, 0);
  if (!total) return empty(container, "No stays began in this period.");
  const W = Math.max(200, container.clientWidth);
  const H = 176;
  const m = { top: 22, bottom: xNote ? 40 : 26 };
  const plotH = H - m.top - m.bottom;
  const max = Math.max(...bins.map((b) => b.count));
  const band = W / bins.length;
  const barW = Math.min(24, band * 0.6);
  const y = (val) => m.top + (1 - val / max) * plotH;
  const svg = root(container, W, H, title);
  const groups = [];
  bins.forEach((b, i) => {
    const cx = band * (i + 0.5);
    const g = s("g", { role: "img", "aria-label": `${b.label}: ${plural(b.count, unit)}` }, svg);
    s("rect", { x: band * i + 1, y: 0, width: band - 2, height: H - m.bottom + 4, rx: 6, class: "hit" }, g);
    if (b.count) {
      s("path", { d: vbar(cx - barW / 2, y(0), y(b.count), barW), class: "m-payout" }, g);
      label(g, cx, y(b.count) - 7, number(b.count), { class: "value-label", "text-anchor": "middle" });
    }
    label(g, cx, H - m.bottom + 17, b.label, { class: "axis-text", "text-anchor": "middle" });
    bindTip(g, () => ({ title: `${b.label} ${xNote || ""}`.trim(),
      rows: [{ key: "k-payout", shape: "rect", value: plural(b.count, unit), label: `${percent(b.count / total)} of stays` }] }));
    groups.push(g);
  });
  s("line", { x1: 0, x2: W, y1: y(0), y2: y(0), class: "baseline" }, svg);
  if (xNote) label(svg, W / 2, H - 4, xNote, { class: "axis-text", "text-anchor": "middle" });
  rove(groups, title);
}

export function renderSeasons(container, seasons) {
  const any = seasons.some((x) => x.weekdayRate || x.weekendRate);
  if (!any) return empty(container, "No nights stayed in this period.");
  const W = Math.max(200, container.clientWidth);
  const H = 176;
  const m = { top: 10, bottom: 40, left: 40 };
  const plotH = H - m.top - m.bottom;
  const tk = ticks(0, Math.max(...seasons.flatMap((x) => [x.weekdayRate || 0, x.weekendRate || 0])), 3);
  const max = tk[tk.length - 1];
  const band = (W - m.left) / seasons.length;
  const barW = Math.min(20, (band - 14) / 2);
  const y = (val) => m.top + (1 - val / max) * plotH;
  const svg = root(container, W, H, "Average nightly rate by season, weekday and weekend nights");
  // Pairs of thin bars leave no room for a value on each cap: a light axis
  // carries the scale, the tooltip and the table carry the exact figures.
  const fmt = axisMoney(max);
  for (const t of tk) {
    if (t) s("line", { x1: m.left, x2: W, y1: y(t), y2: y(t), class: "gridline" }, svg);
    label(svg, m.left - 8, y(t) + 4, fmt(t), { class: "axis-text", "text-anchor": "end" });
  }
  const showValues = barW >= 34;
  const groups = [];
  seasons.forEach((x, i) => {
    const cx = m.left + band * (i + 0.5);
    const g = s("g", { role: "img",
      "aria-label": `${x.label}: Sunday to Thursday ${x.weekdayRate ? money(x.weekdayRate) : "no nights"}, Friday and Saturday ${x.weekendRate ? money(x.weekendRate) : "no nights"}` }, svg);
    s("rect", { x: m.left + band * i + 1, y: 0, width: band - 2, height: H - m.bottom + 4, rx: 6, class: "hit" }, g);
    [["weekdayRate", "m-weekday", -1], ["weekendRate", "m-weekend", 1]].forEach(([field, cls, side]) => {
      const val = x[field];
      if (!val) return;
      const bx = side < 0 ? cx - 1 - barW : cx + 1;   // 2px surface gap between the pair
      s("path", { d: vbar(bx, y(0), y(val), barW), class: cls }, g);
      if (showValues) label(g, bx + barW / 2, y(val) - 7, money(val), { class: "value-label value-label--muted", "text-anchor": "middle" });
    });
    label(g, cx, H - m.bottom + 17, x.label, { class: "axis-text axis-text--strong", "text-anchor": "middle" });
    label(g, cx, H - m.bottom + 31, x.span, { class: "axis-text", "text-anchor": "middle" });
    bindTip(g, () => ({
      title: `${x.label} (${x.span})`,
      rows: [
        { key: "k-weekday", shape: "rect", value: x.weekdayRate ? money(x.weekdayRate) : "–",
          label: `Sun–Thu nights (${number(x.weekday.n)})` },
        { key: "k-weekend", shape: "rect", value: x.weekendRate ? money(x.weekendRate) : "–",
          label: `Fri & Sat nights (${number(x.weekend.n)})` },
      ],
      note: x.weekday.n + x.weekend.n < 20 ? "Few nights: treat this average with care." : "",
    }));
    groups.push(g);
  });
  s("line", { x1: m.left, x2: W, y1: y(0), y2: y(0), class: "baseline" }, svg);
  rove(groups, "Seasons");
}

// ================================================================= tables
/** columns: [{ label, value(row) -> string|Node, cls, title }] */
export function renderTable(container, { caption, columns, rows, foot, rowClass }) {
  const table = h("table", { class: "data-table" },
    caption ? h("caption", { text: caption }) : "",
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col", class: c.cls || "", text: c.label })))),
    h("tbody", {}, rows.map((r) => h("tr", { class: rowClass ? rowClass(r) : "" },
      columns.map((c, i) => {
        const val = c.value(r);
        const cell = h(i === 0 ? "th" : "td", { class: c.cls || "", scope: i === 0 ? "row" : null });
        cell.append(val instanceof Node ? val : document.createTextNode(val ?? ""));
        return cell;
      })))),
    foot ? h("tfoot", {}, h("tr", {}, columns.map((c, i) => h("td", { class: c.cls || "", text: foot[i] ?? "" })))) : "",
  );
  container.replaceChildren(table);
}
