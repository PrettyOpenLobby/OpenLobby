// picker.js -- a search box that finds a player and lets you pick one.
//
// Wherever the panel asks for a player (a game action's field of type
// "player", the PlayOnline ID on the account forms), the operator types part of
// a name or id and picks from a list instead of typing the id exactly. The
// chosen player's label stays in the box; their id is what the form sends.
//
//     const pk = makePicker({ name: "account", search: (q, limit) => rows });
//     form.append(pk.el);          pk.value()   pk.set(id, label)   pk.clear()
//
// search(q, limit) returns (a promise of) [{id, label, detail, online}], where
// online is true, false or null (unknown). Nothing picked, the typed text is
// the value, so an exact id still works when the search is down.
"use strict";

function makePicker(opts) {
  const o = Object.assign({ name: "", placeholder: "Search by name or id", limit: 20, delay: 250 }, opts);
  const uid = "pk" + (makePicker.n = (makePicker.n || 0) + 1);
  const el = document.createElement("div");
  el.className = "pk";
  el.innerHTML =
    `<input type="text" class="pk-q" role="combobox" autocomplete="off" spellcheck="false"` +
    ` aria-autocomplete="list" aria-expanded="false" aria-controls="${uid}-list">` +
    `<button type="button" class="pk-x" aria-label="Clear" title="Clear" hidden>&times;</button>` +
    `<input type="hidden">` +
    `<div class="pk-list" id="${uid}-list" role="listbox" hidden></div>`;
  const box = el.querySelector(".pk-q"), x = el.querySelector(".pk-x");
  const hid = el.querySelector("input[type=hidden]"), list = el.querySelector(".pk-list");
  if (o.id) box.id = o.id;
  if (o.name) hid.name = o.name;
  box.placeholder = o.placeholder;
  const st = { rows: [], active: -1, picked: null, timer: null, seq: 0, open: false };
  const escp = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const sync = () => {
    hid.value = st.picked ? st.picked.id : box.value.trim();
    x.hidden = !box.value;
    el.classList.toggle("picked", !!st.picked);
  };
  const close = () => {
    st.open = false; list.hidden = true; box.setAttribute("aria-expanded", "false");
    box.removeAttribute("aria-activedescendant");
  };
  const show = (html) => {
    list.innerHTML = html; list.hidden = false; st.open = true;
    box.setAttribute("aria-expanded", "true");
  };
  const dot = (on) => on === true ? `<span class="pk-dot on" title="Online"></span>`
    : on === false ? `<span class="pk-dot off" title="Offline"></span>` : `<span class="pk-dot"></span>`;
  const face = (r) => r.avatar ? `<img class="pk-ava" src="${escp(r.avatar)}" alt="" referrerpolicy="no-referrer">` : "";
  const draw = () => {
    if (!st.rows.length) return show(`<div class="pk-note">${box.value.trim() ? "No player matches." : "No one is online."}</div>`);
    show(st.rows.map((r, i) =>
      `<div class="pk-row${i === st.active ? " on" : ""}" role="option" id="${uid}-${i}" data-i="${i}"` +
      ` aria-selected="${i === st.active}">${dot(r.online)}${face(r)}<span class="pk-who"><b>${escp(r.label || r.id)}</b>` +
      (r.detail ? `<span class="pk-det">${escp(r.detail)}</span>` : "") + `</span></div>`).join(""));
    if (st.active >= 0) {
      box.setAttribute("aria-activedescendant", `${uid}-${st.active}`);
      const row = list.querySelector(".pk-row.on");
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }
  };
  const run = async () => {
    const q = box.value.trim(), seq = ++st.seq;
    if (!list.innerHTML || list.hidden) show(`<div class="pk-note">Searching...</div>`);
    let rows;
    try { rows = await o.search(q, o.limit); } catch (e) {
      if (seq !== st.seq) return;
      st.rows = []; st.active = -1;
      return show(`<div class="pk-note bad">The search failed: ${escp(e.message || e)}</div>`);
    }
    if (seq !== st.seq || document.activeElement !== box) return;   // a newer keystroke won, or we left
    st.rows = Array.isArray(rows) ? rows : [];
    st.active = st.rows.length ? 0 : -1;
    draw();
  };
  const soon = () => { clearTimeout(st.timer); st.timer = setTimeout(run, o.delay); };
  const pick = (i) => {
    const r = st.rows[i];
    if (!r) return;
    st.picked = { id: String(r.id), label: String(r.label || r.id) };
    box.value = st.picked.label;
    sync(); close();
    if (o.onpick) o.onpick(st.picked);
  };

  box.addEventListener("input", () => { st.picked = null; sync(); soon(); });
  box.addEventListener("focus", () => { if (!st.picked) soon(); });
  box.addEventListener("click", () => { if (!st.open && !st.picked) soon(); });
  box.addEventListener("blur", () => { clearTimeout(st.timer); st.seq++; setTimeout(close, 120); });
  box.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!st.open) { run(); return; }
      if (!st.rows.length) return;
      const n = st.rows.length;
      st.active = e.key === "ArrowDown" ? (st.active + 1) % n : (st.active - 1 + n) % n;
      draw();
    } else if (e.key === "Enter") {
      if (st.open && st.active >= 0) { e.preventDefault(); pick(st.active); }
    } else if (e.key === "Escape") {
      if (st.open) { e.preventDefault(); e.stopPropagation(); close(); }
      else if (box.value) { e.preventDefault(); api.clear(); }
    }
  });
  // mousedown, not click: the box keeps its focus, so blur does not close the list first
  list.addEventListener("mousedown", (e) => {
    const row = e.target.closest(".pk-row");
    e.preventDefault();
    if (row) pick(+row.dataset.i);
  });
  x.addEventListener("click", () => { api.clear(); box.focus(); });

  const api = {
    el, box,
    value: () => hid.value,
    picked: () => st.picked,
    set(id, label) {
      st.picked = id ? { id: String(id), label: String(label || id) } : null;
      box.value = st.picked ? st.picked.label : "";
      sync(); close();
    },
    clear() { st.picked = null; box.value = ""; st.rows = []; st.seq++; sync(); close(); },
  };
  // a form's reset leaves a hidden input's value alone; clear it with the rest
  setTimeout(() => {
    const f = el.closest("form");
    if (f) f.addEventListener("reset", () => api.clear());
  });
  el.picker = api;
  return api;
}
