// Tooltip, keyboard navigation, chart/table toggles, the period menu and the
// theme switch. Text that comes from the database (guest names, categories)
// is always inserted with textContent, never as HTML.

const SVG_NS = "http://www.w3.org/2000/svg";

/** Inline styles through the CSSOM: the page's Content-Security-Policy
 *  (no 'unsafe-inline') blocks style attributes, but not this. */
export function applyStyle(el, css) {
  for (const decl of String(css).split(";")) {
    const i = decl.indexOf(":");
    if (i > 0) el.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim());
  }
}

/** h("div", { class: "x", text: "hi" }, child, "text") */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k === "class") el.className = v;
    else if (k === "style") applyStyle(el, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ---------------------------------------------------------------- icons
const ICONS = {
  good: "M3.5 8.5l3 3 6-7",
  warning: "M8 2.5l6 11H2zM8 6.5v3.2M8 11.8v.1",
  critical: "M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM8 5.2v3.6M8 10.8v.1",
  clock: "M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM8 5v3.2l2.2 1.4",
  check: "M3.5 8.5l3 3 6-7",
  sun: "M8 5.2a2.8 2.8 0 1 1 0 5.6 2.8 2.8 0 0 1 0-5.6zM8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1",
  moon: "M12.8 10.2A5.5 5.5 0 0 1 5.8 3.2a5.5 5.5 0 1 0 7 7z",
};

export function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon ${cls}`.trim());
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

/** A status mark: never colour alone, always with its label next to it. */
export function statusIcon(kind) {
  const names = { good: "good", warning: "warning", critical: "critical", pending: "clock" };
  const tone = kind === "pending" ? "muted" : kind;
  return icon(names[kind] || "clock", `status-icon status-icon--${tone}`);
}

// -------------------------------------------------------------- tooltip
const tip = document.getElementById("tooltip");
let tipOwner = null;

/** content: { title, sub, rows: [{ key, shape, value, label, total }], note, code } */
function fillTip(content) {
  tip.replaceChildren();
  if (content.title) tip.append(h("div", { class: "tip__title", text: content.title }));
  if (content.sub) tip.append(h("div", { class: "tip__sub", text: content.sub }));
  if (content.rows?.length) {
    const rows = h("div", { class: "tip__rows" });
    for (const r of content.rows) {
      const cls = r.total ? "tip__row--total" : "";
      rows.append(
        h("span", { class: cls }, r.key ? h("span", { class: `key key--${r.shape || "line"} ${r.key}` }) : ""),
        h("span", { class: `tip__value ${cls}`, text: r.value }),
        h("span", { class: `tip__label ${cls}`, text: r.label }),
      );
    }
    tip.append(rows);
  }
  if (content.note) tip.append(h("div", { class: "tip__note", text: content.note }));
  if (content.code) tip.append(h("div", { class: "tip__code", text: content.code }));
}

function placeAt(x, y) {
  const pad = 12;
  const { width, height } = tip.getBoundingClientRect();
  let left = x + 16;
  let top = y + 16;
  if (left + width > window.innerWidth - pad) left = x - width - 16;
  if (top + height > window.innerHeight - pad) top = y - height - 16;
  tip.style.transform = `translate(${Math.max(pad, left)}px, ${Math.max(pad, top)}px)`;
}

function placeAbove(rect) {
  const pad = 12;
  const { width, height } = tip.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.min(Math.max(pad, left), window.innerWidth - width - pad);
  let top = rect.top - height - 10;
  if (top < pad) top = rect.bottom + 10;
  tip.style.transform = `translate(${left}px, ${top}px)`;
}

export function hideTip() {
  tip.hidden = true;
  if (tipOwner) tipOwner.classList.remove("is-active");
  tipOwner = null;
}

/** Hover, focus and tap all show the same details. */
export function bindTip(el, content) {
  const show = (owner) => {
    if (tipOwner && tipOwner !== owner) tipOwner.classList.remove("is-active");
    tipOwner = owner;
    owner.classList.add("is-active");
    fillTip(typeof content === "function" ? content() : content);
    tip.hidden = false;
  };
  el.addEventListener("pointerenter", (e) => { show(el); placeAt(e.clientX, e.clientY); });
  el.addEventListener("pointermove", (e) => { if (tipOwner === el) placeAt(e.clientX, e.clientY); });
  el.addEventListener("pointerleave", (e) => { if (e.pointerType !== "touch" && tipOwner === el) hideTip(); });
  el.addEventListener("focus", () => { show(el); placeAbove(el.getBoundingClientRect()); });
  el.addEventListener("blur", () => { if (tipOwner === el) hideTip(); });
}

document.addEventListener("pointerdown", (e) => {
  if (tipOwner && !tipOwner.contains(e.target)) hideTip();
}, { passive: true });
window.addEventListener("scroll", () => { if (tipOwner && document.activeElement !== tipOwner) hideTip(); }, { passive: true });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });

// ------------------------------------------------------ roving tabindex
/** One tab stop for a set of marks; arrow keys move between them. */
export function rove(items, label) {
  if (!items.length) return;
  items.forEach((el, i) => {
    el.setAttribute("tabindex", i === 0 ? "0" : "-1");
    el.addEventListener("keydown", (e) => {
      const next = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1,
                     Home: 0, End: items.length - 1 }[e.key];
      if (next === undefined) return;
      e.preventDefault();
      const target = items[Math.max(0, Math.min(items.length - 1, next))];
      el.setAttribute("tabindex", "-1");
      target.setAttribute("tabindex", "0");
      target.focus();
    });
  });
  if (label) items[0].closest("svg")?.setAttribute("aria-label", label);
}

// ------------------------------------------------------- view toggles
export function setupViewToggles(onChange) {
  for (const holder of document.querySelectorAll(".view-toggle[data-view-for]")) {
    const section = document.getElementById(holder.dataset.viewFor);
    const views = section.querySelectorAll(":scope > .view, :scope > [data-view]");
    const buttons = ["chart", "table"].map((mode) => h("button", {
      type: "button", "aria-pressed": mode === "chart" ? "true" : "false",
      text: mode === "chart" ? "Chart" : "Table",
      onclick: () => {
        for (const b of buttons) b.setAttribute("aria-pressed", b === buttons[mode === "chart" ? 0 : 1] ? "true" : "false");
        for (const v of views) v.hidden = v.dataset.view !== mode;
        onChange?.(holder.dataset.viewFor, mode);
      },
    }));
    holder.setAttribute("role", "group");
    holder.setAttribute("aria-label", "Show as");
    holder.replaceChildren(...buttons);
  }
}

// --------------------------------------------------------- period menu
export function periodMenu({ button, menu, onSelect }) {
  let items = [];

  const close = (focusButton = false) => {
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (focusButton) button.focus();
  };
  const open = () => {
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    (items.find((i) => i.getAttribute("aria-checked") === "true") || items[0])?.focus();
  };

  button.addEventListener("click", () => (menu.hidden ? open() : close()));
  document.addEventListener("pointerdown", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !button.contains(e.target)) close();
  });
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const i = items.indexOf(document.activeElement);
    if (i === -1) return;
    e.preventDefault();
    const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
    items[(next + items.length) % items.length].focus();
  });
  menu.addEventListener("focusout", (e) => {
    if (!menu.hidden && !menu.contains(e.relatedTarget) && e.relatedTarget !== button) close();
  });

  /** presets: [{ id, group, label, from, to }], months: all month keys */
  function render(presets, selected, months, labels) {
    const groups = new Map();
    for (const p of presets) {
      if (!groups.has(p.group)) groups.set(p.group, []);
      groups.get(p.group).push(p);
    }
    const blocks = [];
    items = [];
    for (const list of groups.values()) {
      const block = h("div", { class: "menu__group", role: "group" });
      for (const p of list) {
        const item = h("button", {
          type: "button", class: "menu__item", role: "menuitemradio",
          "aria-checked": selected.id === p.id ? "true" : "false",
          onclick: () => { close(true); onSelect({ id: p.id, from: p.from, to: p.to }); },
        }, icon("check", "menu__check"), h("span", { class: "menu__label", text: p.label }),
           h("span", { class: "menu__range", text: labels.range(p.from, p.to) }));
        items.push(item);
        block.append(item);
      }
      blocks.push(block);
    }

    // Any range: two month pickers
    const opt = (k, sel) => h("option", { value: k, selected: k === sel, text: labels.month(k) });
    const fromSel = h("select", { "aria-label": "From month" }, months.map((k) => opt(k, selected.from)));
    const toSel = h("select", { "aria-label": "To month" }, months.map((k) => opt(k, selected.to)));
    const apply = h("button", {
      type: "button", class: "btn btn--primary", text: "Show",
      onclick: () => {
        let [f, t] = [fromSel.value, toSel.value];
        if (f > t) [f, t] = [t, f];
        close(true);
        onSelect({ id: "custom", from: f, to: t });
      },
    });
    const custom = h("div", { class: "menu__group menu__custom" },
      h("span", { class: "menu__custom-title", text: "Any range" }),
      h("div", { class: "menu__custom-row" },
        h("label", {}, "From", fromSel), h("label", {}, "To", toSel), apply));
    menu.replaceChildren(...blocks, custom);
  }

  return { render, close };
}

// ---------------------------------------------------------------- theme
const THEMES = ["system", "light", "dark"];
const THEME_NAMES = { system: "match system", light: "light", dark: "dark" };

export function themeSwitch(button, onChange) {
  let current = "system";
  try { current = localStorage.getItem("redtree-theme") || "system"; } catch { /* blocked */ }
  if (!THEMES.includes(current)) current = "system";

  const paint = () => {
    const root = document.documentElement;
    if (current === "system") delete root.dataset.theme;
    else root.dataset.theme = current;
    const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    button.setAttribute("aria-label", `Theme: ${THEME_NAMES[current]}. Switch to ${THEME_NAMES[next]}`);
    button.title = `Theme: ${THEME_NAMES[current]}`;
    if (current === "light") button.replaceChildren(icon("sun"));
    else if (current === "dark") button.replaceChildren(icon("moon"));
    else {
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("viewBox", "0 0 16 16");
      svg.setAttribute("class", "icon");
      svg.setAttribute("aria-hidden", "true");
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", "8"); c.setAttribute("cy", "8"); c.setAttribute("r", "5.5");
      const half = document.createElementNS(SVG_NS, "path");
      half.setAttribute("d", "M8 2.5a5.5 5.5 0 0 1 0 11z");
      half.setAttribute("class", "icon__fill");
      svg.append(c, half);
      button.replaceChildren(svg);
    }
  };

  button.addEventListener("click", () => {
    current = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    try {
      if (current === "system") localStorage.removeItem("redtree-theme");
      else localStorage.setItem("redtree-theme", current);
    } catch { /* blocked: the choice lasts until reload */ }
    paint();
    onChange?.();
  });
  paint();
}
