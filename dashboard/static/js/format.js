// Number, money and date formatting. Australian conventions: AUD, day-first.

const MINUS = "−";
const AUD0 = new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD", maximumFractionDigits: 0 });
const AUD2 = new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const NUM0 = new Intl.NumberFormat("en-AU", { maximumFractionDigits: 0 });
const NUM1 = new Intl.NumberFormat("en-AU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** $17,214 / −$111. A true minus sign, and no "−$0". */
export function money(value, { cents = false, signed = false } = {}) {
  const v = Number(value) || 0;
  const shown = cents ? Math.round(Math.abs(v) * 100) : Math.round(Math.abs(v));
  const body = (cents ? AUD2 : AUD0).format(Math.abs(v));
  if (shown === 0) return body;
  if (v < 0) return MINUS + body;
  return signed ? "+" + body : body;
}

/** A cost shown as money going out: −$960. */
export const outflow = (value) => money(-Math.abs(value));

/** Axis ticks: in full under 10,000, compact on an axis that reaches it. */
export function axisMoney(maxAbs) {
  const compact = maxAbs >= 10000;
  return (v) => {
    if (v === 0) return "$0";
    const sign = v < 0 ? MINUS : "";
    const a = Math.abs(v);
    if (!compact) return sign + "$" + NUM0.format(a);
    if (a >= 1e6) return sign + "$" + trim(a / 1e6) + "M";
    return sign + "$" + trim(a / 1e3) + "K";
  };
}
const trim = (n) => (Math.round(n * 10) / 10).toString();

export const number = (v) => NUM0.format(v);
export const decimal1 = (v) => NUM1.format(v);
export const percent = (ratio, digits = 0) =>
  ratio == null || !isFinite(ratio) ? "–" : (ratio * 100).toFixed(digits) + "%";

export function plural(n, one, many = one + "s") {
  return `${number(n)} ${n === 1 ? one : many}`;
}

// ---- dates: ISO strings in, local calendar dates out (no time zones) ----

export function parseISO(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d || 1);
}

/** "2025-04" -> "Apr 2025" (or "Apr" with year: false, "April 2025" long) */
export function monthLabel(key, { year = true, long = false } = {}) {
  const [y, m] = key.split("-").map(Number);
  const name = (long ? MONTHS_LONG : MONTHS)[m - 1];
  return year ? `${name} ${y}` : name;
}

export function rangeLabel(from, to) {
  if (from === to) return monthLabel(from);
  const [fy] = from.split("-");
  const [ty] = to.split("-");
  if (fy === ty) return `${monthLabel(from, { year: false })} – ${monthLabel(to)}`;
  return `${monthLabel(from)} – ${monthLabel(to)}`;
}

/** "2025-04-03" -> "Thu 3 Apr" (+ year when asked) */
export function dateLabel(iso, { year = false, weekday = true } = {}) {
  const d = parseISO(iso);
  const parts = [];
  if (weekday) parts.push(DAYS[d.getDay()]);
  parts.push(d.getDate(), MONTHS[d.getMonth()]);
  if (year) parts.push(d.getFullYear());
  return parts.join(" ");
}

export function initials(name) {
  return (name || "")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
}

/** council_rates -> Council rates */
export function humanize(code) {
  const s = String(code || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
