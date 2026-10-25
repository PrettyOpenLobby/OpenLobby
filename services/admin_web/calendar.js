// calendar.js -- the Calendar section: what every game has scheduled, and the panel's own entries.
// Reads /api/calendar?from=&to= (admincalendar.py); entries are unix seconds. Month, week and list
// views, in the viewer's time or UTC; each game can be hidden; the panel's own entries are edited
// here (calendar_edit). Game entries are read-only: they come from the games' own calendars.
"use strict";

const CAL = { view: "", anchor: new Date(), hide: new Set(), utc: false, data: null, req: 0 };
const CAL_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const CAL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                    "September", "October", "November", "December"];
const CAL_KINDS = { event: "Event", maintenance: "Maintenance", match: "Match", community: "Community night",
                    reminder: "Reminder" };
const CAL_LANES = 3;
// the panel's own entries, by one name everywhere: the chip, an entry's details, the list
const CAL_OWN = "Server entries", CAL_OWN_ONE = "Server entry";
const CAL_HOUR_PX = 28;

try {
  const saved = JSON.parse(localStorage.getItem("calPrefs") || "{}");
  CAL.utc = !!saved.utc;
  CAL.hide = new Set(saved.hide || []);
  if (["month", "week", "list"].includes(saved.view)) CAL.view = saved.view;
} catch (e) { /* no storage: defaults */ }
function calSave() {
  try { localStorage.setItem("calPrefs", JSON.stringify({ utc: CAL.utc, hide: [...CAL.hide], view: CAL.view })); }
  catch (e) { /* no storage */ }
}

// ---- time, in the viewer's zone or UTC ----
function calParts(ms) {
  const d = new Date(ms);
  return CAL.utc
    ? { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), dow: (d.getUTCDay() + 6) % 7 }
    : { y: d.getFullYear(), m: d.getMonth(), d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), dow: (d.getDay() + 6) % 7 };
}
const calAt = (y, m, d, h = 0, mi = 0) => (CAL.utc ? Date.UTC(y, m, d, h, mi) : new Date(y, m, d, h, mi).getTime());
const calDay = (ms) => { const p = calParts(ms); return calAt(p.y, p.m, p.d); };
const calAddDays = (ms, n) => { const p = calParts(ms); return calAt(p.y, p.m, p.d + n); };
const calKey = (ms) => { const p = calParts(ms); return `${p.y}-${p.m + 1}-${p.d}`; };
const pad2 = (n) => String(n).padStart(2, "0");
const calClock = (ms) => { const p = calParts(ms); return `${pad2(p.h)}:${pad2(p.mi)}`; };
const calDate = (ms) => { const p = calParts(ms); return `${CAL_DOW[p.dow]} ${p.d} ${CAL_MONTHS[p.m].slice(0, 3)}`; };
const calZone = () => (CAL.utc ? "UTC" : Intl.DateTimeFormat().resolvedOptions().timeZone || "local time");

function calRange() {
  const a = calParts(CAL.anchor.getTime());
  if (CAL.view === "week") {
    const lo = calAt(a.y, a.m, a.d - a.dow);
    return [lo, calAddDays(lo, 7)];
  }
  if (CAL.view === "list") {
    const lo = calAt(a.y, a.m, a.d);
    return [lo, calAddDays(lo, 42)];
  }
  const first = calAt(a.y, a.m, 1);
  const lo = calAddDays(first, -calParts(first).dow);
  return [lo, calAddDays(lo, 42)];
}

function calTitle() {
  const a = calParts(CAL.anchor.getTime());
  if (CAL.view === "month") return `${CAL_MONTHS[a.m]} ${a.y}`;
  const [lo, hi] = calRange();
  return `${calDate(lo)} - ${calDate(hi - 1)}`;
}

// ---- colours: one per game, stable; the panel's own entries by kind ----
function calColour(e) {
  if (e.source === "panel") return e.kind === "maintenance" ? "warn" : "own";
  const keys = ((CAL.data || {}).games || []).map((g) => g.key).sort();
  const i = keys.indexOf(e.game);
  return "c" + ((i < 0 ? 0 : i) % 8 + 1);
}
const calGameName = (key) => {
  const g = ((CAL.data || {}).games || []).find((x) => x.key === key);
  return g ? g.title : key;
};
const calShown = () => ((CAL.data || {}).entries || []).filter((e) => !CAL.hide.has(e.source === "panel" ? "_panel" : e.game));
const calMs = (e) => [e.start * 1000, Math.max(e.end, e.start + 1) * 1000];
const calTimed = (e) => !e.all_day && (e.end - e.start) < 86400 && calKey(e.start * 1000) === calKey(e.end * 1000 - 1);

// ---- loading ----
async function loadCalendar() {
  if (!CAL.view) CAL.view = matchMedia("(max-width: 640px)").matches ? "list" : "month";
  const [lo, hi] = calRange();
  const n = ++CAL.req;
  $("#calTitle").textContent = calTitle();
  calToolbar();
  let d;
  try { d = await api(`/api/calendar?from=${Math.floor(lo / 1000)}&to=${Math.floor(hi / 1000)}`); }
  catch (e) { if (n === CAL.req) $("#calBody").innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  if (n !== CAL.req) return;                 // a newer request is on its way
  CAL.data = d;
  calRender();
}

function calToolbar() {
  document.querySelectorAll("#tab-calendar [data-calview]").forEach((b) => b.classList.toggle("active", b.dataset.calview === CAL.view));
  $("#calZone").textContent = CAL.utc ? "Times: UTC" : "Times: mine";
  $("#calZone").title = `Times are shown in ${calZone()}. Click to show them in ${CAL.utc ? "your own time" : "UTC"}.`;
  $("#calNew").hidden = !can("calendar_edit");
}

function calRender() {
  const d = CAL.data || {};
  const seen = new Set((d.entries || []).map((e) => (e.source === "panel" ? "_panel" : e.game)));
  const chips = (d.games || []).filter((g) => seen.has(g.key)).map((g) => [g.key, g.short || g.title, calColour({ game: g.key })])
    .sort((a, b) => a[1].localeCompare(b[1]));
  if (seen.has("_panel")) chips.push(["_panel", CAL_OWN, "own"]);
  $("#calChips").innerHTML = chips.map(([k, label, c]) =>
    `<button class="cal-chip${CAL.hide.has(k) ? " off" : ""}" data-calhide="${esc(k)}" aria-pressed="${!CAL.hide.has(k)}"`
    + ` title="${CAL.hide.has(k) ? "Show" : "Hide"} ${esc(label)}"><i class="cal-dot ${c}"></i>${esc(label)}</button>`).join("");
  $("#calChips").hidden = !chips.length;
  $("#calProblems").textContent = (d.problems || []).join("; ");
  $("#calProblems").hidden = !(d.problems || []).length;
  if (CAL.view === "week") calWeek();
  else if (CAL.view === "list") calList();
  else calMonth();
}

// One message when the range shows nothing: everything hidden by the names above, or nothing at all.
function calEmpty() {
  if (calShown().length) return "";
  const any = ((CAL.data || {}).entries || []).length > 0;
  const range = CAL.view === "month" ? "in " + calTitle() : "from " + calTitle().replace(" - ", " to ");
  return `<p class="hint cal-empty">${any ? "Everything here is hidden. Click a name above to show it again."
    : `Nothing scheduled ${esc(range)}.`}${!any && can("calendar_edit") ? " Use New entry, or the + on a day, to add one." : ""}</p>`;
}

// Bars for one row of days: [{e, s, n, lane}] with s the first column and n how many.
function calBars(entries, lo, days) {
  const hi = calAddDays(lo, days);
  const rows = entries.filter((e) => { const [s, t] = calMs(e); return t > lo && s < hi; })
    .map((e) => {
      const [s, t] = calMs(e);
      let a = 0; while (a < days - 1 && calAddDays(lo, a + 1) <= s) a++;
      let b = a; while (b < days - 1 && calAddDays(lo, b + 1) < t) b++;
      return { e, s: a, n: b - a + 1, from: s < lo, to: t > hi };
    })
    .sort((x, y) => x.s - y.s || y.n - x.n || x.e.start - y.e.start);
  const lanes = [];
  for (const r of rows) {
    let l = 0;
    while ((lanes[l] || []).some((o) => o.s < r.s + r.n && r.s < o.s + o.n)) l++;
    (lanes[l] = lanes[l] || []).push(r);
    r.lane = l;
  }
  return rows;
}

function calBarHtml(r) {
  const e = r.e;
  const time = calTimed(e) ? `<b>${esc(calClock(e.start * 1000))}</b> ` : "";
  return `<button class="cal-bar ${calColour(e)}${r.from ? " from" : ""}${r.to ? " to" : ""}" data-calentry="${esc(e.id)}"`
    + ` style="grid-column:${r.s + 1} / span ${r.n};grid-row:${r.lane + 1}" title="${esc(e.title)}">${time}${esc(e.title)}</button>`;
}

function calMonth() {
  const [lo] = calRange();
  const month = calParts(CAL.anchor.getTime()).m;
  const today = calDay(Date.now());
  const shown = calShown();
  const edit = can("calendar_edit");
  let html = `<div class="cal-head">${CAL_DOW.map((d) => `<div>${d}</div>`).join("")}</div>`;
  for (let w = 0; w < 6; w++) {
    const ws = calAddDays(lo, w * 7);
    const bars = calBars(shown, ws, 7);
    const more = [0, 0, 0, 0, 0, 0, 0];
    bars.filter((r) => r.lane >= CAL_LANES).forEach((r) => { for (let i = r.s; i < r.s + r.n; i++) more[i]++; });
    html += `<div class="cal-week">`;
    for (let i = 0; i < 7; i++) {
      const day = calAddDays(ws, i);
      const p = calParts(day);
      html += `<div class="cal-day${p.m !== month ? " out" : ""}${day === today ? " today" : ""}" data-calday="${day}">`
        + `<span class="cal-num">${p.d}</span>`
        + (edit ? `<button class="cal-add" data-caladd="${day}" title="Add an entry on ${esc(calDate(day))} (or double-click the day)"`
          + ` aria-label="Add an entry on ${esc(calDate(day))}">+</button>` : "")
        + (more[i] ? `<button class="cal-more" data-calday="${day}">+${more[i]} more</button>` : "") + `</div>`;
    }
    html += `<div class="cal-bars">${bars.filter((r) => r.lane < CAL_LANES).map(calBarHtml).join("")}</div></div>`;
  }
  // a phone's grid has room for coloured bars only: the month's entries are listed under it
  const first = calAt(calParts(CAL.anchor.getTime()).y, month, 1);
  const next = calAt(calParts(first).y, month + 1, 1);
  $("#calBody").innerHTML = calEmpty() + `<div class="cal-month">${html}</div>`
    + `<div class="cal-monthlist">${calListHtml(first, next)}</div>`;
}

function calWeek() {
  const [lo] = calRange();
  const today = calDay(Date.now());
  const shown = calShown();
  // a long entry with sessions shows its sessions in the hours below, and its span up top
  const spans = shown.filter((e) => !calTimed(e));
  const bars = calBars(spans, lo, 7);
  const lanes = Math.max(1, ...bars.map((r) => r.lane + 1));
  let head = `<div class="cal-wk-days"><div></div>`;
  for (let i = 0; i < 7; i++) {
    const day = calAddDays(lo, i); const p = calParts(day);
    head += `<div class="${day === today ? "today" : ""}">${CAL_DOW[i]} <b>${p.d}</b></div>`;
  }
  head += `</div><div class="cal-wk-all"><div class="muted">All day</div><div class="cal-bars" style="grid-template-rows:repeat(${lanes},22px)">`
    + bars.map(calBarHtml).join("") + `</div></div>`;
  let cols = "";
  for (let i = 0; i < 7; i++) {
    const day = calAddDays(lo, i), next = calAddDays(day, 1);
    const blocks = [];
    for (const e of shown) {
      const pieces = calTimed(e) ? [[e.start, e.end]] : (e.times || []);
      for (const [s, t] of pieces) {
        const sm = s * 1000, tm = Math.max(t, s + 900) * 1000;
        if (tm > day && sm < next) blocks.push({ e, sm: Math.max(sm, day), tm: Math.min(tm, next) });
      }
    }
    blocks.sort((a, b) => a.sm - b.sm);
    const ends = [];
    for (const b of blocks) {                       // side by side when they overlap
      let c = ends.findIndex((t) => t <= b.sm);
      if (c < 0) { c = ends.length; ends.push(0); }
      ends[c] = b.tm; b.col = c;
    }
    const width = 100 / Math.max(1, ends.length);
    cols += `<div class="cal-wk-col${day === today ? " today" : ""}">` + blocks.map((b) => {
      const top = (b.sm - day) / 3600000 * CAL_HOUR_PX, h = Math.max(18, (b.tm - b.sm) / 3600000 * CAL_HOUR_PX - 2);
      return `<button class="cal-block ${calColour(b.e)}${h < 40 ? " short" : ""}" data-calentry="${esc(b.e.id)}" style="top:${top}px;height:${h}px;`
        + `left:${b.col * width}%;width:calc(${width}% - 3px)" title="${esc(b.e.title)}"><b>${esc(calClock(b.sm))}</b> ${esc(b.e.title)}</button>`;
    }).join("");
    if (day === today) cols += `<div class="cal-now" style="top:${(Date.now() - day) / 3600000 * CAL_HOUR_PX}px"></div>`;
    cols += `</div>`;
  }
  const hours = Array.from({ length: 24 }, (_, h) => `<div>${pad2(h)}:00</div>`).join("");
  $("#calBody").innerHTML = calEmpty() + `<div class="cal-wk">${head}<div class="cal-wk-scroll" id="calScroll"><div class="cal-wk-grid">`
    + `<div class="cal-wk-hours">${hours}</div>${cols}</div></div></div>`;
  $("#calScroll").scrollTop = 8 * CAL_HOUR_PX;
}

function calList() {
  const [lo, hi] = calRange();
  $("#calBody").innerHTML = calListHtml(lo, hi) || calEmpty();
}
const calWho = (e) => (e.source === "panel" ? `${e.game ? calGameName(e.game) : "All games"}, ${CAL_OWN_ONE.toLowerCase()}` : calGameName(e.game));
function calListHtml(lo, hi) {
  const shown = calShown();
  const today = calDay(Date.now());
  let html = "";
  for (let day = lo; day < hi; day = calAddDays(day, 1)) {
    const next = calAddDays(day, 1);
    // an entry is listed on its first day in range; a long one says until when
    const here = shown.filter((e) => { const [s] = calMs(e); return (s >= day && s < next) || (day === lo && s < lo && e.end * 1000 > lo); });
    if (!here.length) continue;
    html += `<h3 class="cal-list-day${day === today ? " today" : ""}">${esc(calDate(day))}${day === today ? ", today" : ""}</h3><ul class="feed cal-list">`
      + here.map((e) => {
        const [s, t] = calMs(e);
        const when = e.all_day ? "All day" : calTimed(e) ? `${calClock(s)} - ${calClock(t)}`
          : `${calClock(s)} until ${calDate(t - 1)} ${calClock(t)}`;
        const sessions = (e.times || []).length ? `, ${e.times.length} session${e.times.length === 1 ? "" : "s"}` : "";
        return `<li data-calentry="${esc(e.id)}"><span class="t1"><i class="cal-dot ${calColour(e)}"></i>${esc(e.title)}`
          + `<span class="cal-sub">${esc(calWho(e))}</span></span>`
          + `<span class="t2">${esc(when)}${esc(sessions)}</span></li>`;
      }).join("") + `</ul>`;
  }
  return html;
}

// one day's entries, from "+N more" or a tap on a phone's day: the saved view stays as it is
function calDayOpen(day) {
  const next = calAddDays(day, 1);
  const here = calShown().filter((e) => { const [s, t] = calMs(e); return s < next && t > day; })
    .sort((a, b) => a.start - b.start);
  $("#calDetTitle").textContent = calDate(day);
  $("#calDetBody").innerHTML = here.length ? `<ul class="feed cal-list">` + here.map((e) => {
    const [s, t] = calMs(e);
    const when = e.all_day || !calTimed(e) ? "All day" : `${calClock(s)} - ${calClock(t)}`;
    return `<li data-calentry="${esc(e.id)}"><span class="t1"><i class="cal-dot ${calColour(e)}"></i>${esc(e.title)}`
      + `<span class="cal-sub">${esc(calWho(e))}</span></span><span class="t2">${esc(when)}</span></li>`;
  }).join("") + `</ul>` : `<p class="muted">Nothing that day.</p>`;
  $("#calDetEdit").hidden = true;
  $("#calDetDel").hidden = true;
  $("#calDetModal").classList.add("show");
}

// ---- one entry ----
function calOpen(id) {
  const e = ((CAL.data || {}).entries || []).find((x) => x.id === id);
  if (!e) return;
  const [s, t] = calMs(e);
  const who = e.source === "panel" && !e.game ? "All games" : calGameName(e.game);
  const when = e.all_day ? `${calDate(s)}${calKey(s) !== calKey(t - 1) ? " - " + calDate(t - 1) : ""}, all day`
    : `${calDate(s)} ${calClock(s)} - ${calKey(s) !== calKey(t - 1) ? calDate(t - 1) + " " : ""}${calClock(t)}`;
  const kind = CAL_KINDS[e.kind] || (e.kind ? e.kind[0].toUpperCase() + e.kind.slice(1) : "");
  const sessions = (e.times || []).map(([a, b]) => `<li><span class="t1">${esc(calDate(a * 1000))}</span>`
    + `<span class="t2">${esc(calClock(a * 1000))} - ${esc(calClock(b * 1000))}</span></li>`).join("");
  $("#calDetTitle").textContent = e.title;
  $("#calDetBody").innerHTML = `<div class="kv"><span class="muted">When</span><span style="display:block">${esc(when)}<span class="muted cal-z">${esc(calZone())}</span></span>`
    + `<span class="muted">Game</span><span><i class="cal-dot ${calColour(e)}"></i>${esc(who)}</span>`
    + (kind ? `<span class="muted">Kind</span><span>${esc(kind)}</span>` : "")
    + `<span class="muted">From</span><span>${e.source === "panel" ? `${CAL_OWN_ONE}, added by ${esc(e.by || "?")}` : `${esc(calGameName(e.game))}'s own schedule`}</span></div>`
    + (e.about ? `<p>${esc(e.about)}</p>` : "")
    + (sessions ? `<h4>Sessions</h4><ul class="feed cal-list">${sessions}</ul>` : "");
  const mine = e.editable && can("calendar_edit");
  $("#calDetEdit").hidden = !mine;
  $("#calDetDel").hidden = !mine;
  $("#calDetEdit").dataset.id = e.id;
  $("#calDetDel").dataset.id = e.id;
  $("#calDetModal").classList.add("show");
}

// ---- adding and editing the panel's own entries ----
const calInDate = (ms) => { const p = calParts(ms); return `${p.y}-${pad2(p.m + 1)}-${pad2(p.d)}`; };
function calFromInputs(date, time) {
  const [y, m, d] = (date || "").split("-").map(Number);
  const [h, mi] = (time || "00:00").split(":").map(Number);
  return y ? calAt(y, m - 1, d, h || 0, mi || 0) : NaN;
}

function calEdit(id, day) {
  const e = id ? ((CAL.data || {}).entries || []).find((x) => x.id === id) : null;
  const s = e ? e.start * 1000 : (day || calDay(Date.now())) + 19 * 3600000;
  const t = e ? e.end * 1000 : s + 3600000;
  $("#calEdTitle").textContent = e ? "Change entry" : "New calendar entry";
  $("#calEdId").value = e ? e.id : "";
  $("#calEdName").value = e ? e.title : "";
  $("#calEdGame").innerHTML = `<option value="">All games</option>`
    + ((CAL.data || {}).games || []).slice().sort((a, b) => String(a.title).localeCompare(String(b.title)))
      .map((g) => `<option value="${esc(g.key)}">${esc(g.title)}</option>`).join("");
  $("#calEdGame").value = e ? e.game || "" : "";
  $("#calEdKind").value = e ? e.kind || "event" : "event";
  $("#calEdAll").checked = e ? !!e.all_day : false;
  $("#calEdStartD").value = calInDate(s); $("#calEdStartT").value = calClock(s);
  $("#calEdEndD").value = calInDate(e && e.all_day ? t - 1 : t); $("#calEdEndT").value = calClock(t);
  $("#calEdAbout").value = e ? e.about || "" : "";
  $("#calEdZone").textContent = `Times are ${calZone()}.`;
  $("#calEdErr").hidden = true;
  calAllDay();
  $("#calDetModal").classList.remove("show");
  $("#calEdModal").classList.add("show");
  $("#calEdName").focus();
}
function calAllDay() {
  const all = $("#calEdAll").checked;
  $("#calEdStartT").hidden = all; $("#calEdEndT").hidden = all;
}

async function calSaveEntry() {
  const all = $("#calEdAll").checked;
  let s = calFromInputs($("#calEdStartD").value, all ? "00:00" : $("#calEdStartT").value);
  let t = calFromInputs($("#calEdEndD").value, all ? "00:00" : $("#calEdEndT").value);
  if (all && !isNaN(t)) { const p = calParts(t); t = calAt(p.y, p.m, p.d + 1); }
  const err = (m) => { $("#calEdErr").textContent = m; $("#calEdErr").hidden = false; };
  if (isNaN(s) || isNaN(t)) return err("Pick a start and an end.");
  if (t <= s) return err("It has to end after it starts.");
  const id = $("#calEdId").value;
  try {
    await api("/api/calendar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      action: id ? "edit" : "add", id, title: $("#calEdName").value, game: $("#calEdGame").value,
      kind: $("#calEdKind").value, all_day: all, start: Math.floor(s / 1000), end: Math.floor(t / 1000),
      about: $("#calEdAbout").value }) });
  } catch (e2) { return err(e2.message); }
  $("#calEdModal").classList.remove("show");
  toast(id ? "Entry changed" : "Added to the calendar");
  loadCalendar();
}

async function calDelete(id) {
  const e = ((CAL.data || {}).entries || []).find((x) => x.id === id);
  if (!e || !confirm(`Delete "${e.title}" from the calendar?`)) return;
  try {
    await api("/api/calendar", { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify({ action: "delete", id }) });
  } catch (err) { return toast(err.message, true); }
  $("#calDetModal").classList.remove("show");
  toast("Deleted");
  loadCalendar();
}

function calMove(step) {
  const a = calParts(CAL.anchor.getTime());
  if (CAL.view === "month") CAL.anchor = new Date(calAt(a.y, a.m + step, 1));
  else CAL.anchor = new Date(calAt(a.y, a.m, a.d + step * (CAL.view === "week" ? 7 : 42)));
  loadCalendar();
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#tab-calendar, #calDetModal, #calEdModal")) return;
  const t = e.target;
  const entry = t.closest("[data-calentry]");
  if (entry) return calOpen(entry.dataset.calentry);
  const v = t.closest("[data-calview]");
  if (v) { CAL.view = v.dataset.calview; calSave(); return loadCalendar(); }
  const chip = t.closest("[data-calhide]");
  if (chip) {
    const k = chip.dataset.calhide;
    if (CAL.hide.has(k)) CAL.hide.delete(k); else CAL.hide.add(k);
    calSave(); return calRender();
  }
  const more = t.closest(".cal-more");
  if (more) return calDayOpen(+more.dataset.calday);
  const add = t.closest("[data-caladd]");
  if (add) return calEdit(null, +add.dataset.caladd);
  const cell = t.closest(".cal-day");
  if (cell && can("calendar_edit") && e.detail === 2) return calEdit(null, +cell.dataset.calday);
  // a phone's day is too small for its bars to be tapped: the day opens with its entries
  if (cell && matchMedia("(max-width: 640px)").matches) return calDayOpen(+cell.dataset.calday);
  if (t.closest("#calPrev")) return calMove(-1);
  if (t.closest("#calNext")) return calMove(1);
  if (t.closest("#calToday")) { CAL.anchor = new Date(); return loadCalendar(); }
  if (t.closest("#calZone")) { CAL.utc = !CAL.utc; calSave(); return loadCalendar(); }
  if (t.closest("#calNew")) return calEdit(null);
  if (t.closest("#calDetEdit")) return calEdit(t.closest("#calDetEdit").dataset.id);
  if (t.closest("#calDetDel")) return calDelete(t.closest("#calDetDel").dataset.id);
  if (t.closest("#calDetClose") || t.id === "calDetModal") return $("#calDetModal").classList.remove("show");
  if (t.closest("#calEdCancel") || t.id === "calEdModal") return $("#calEdModal").classList.remove("show");
  if (t.closest("#calEdSave")) return calSaveEntry();
});
document.addEventListener("change", (e) => { if (e.target.id === "calEdAll") calAllDay(); });
