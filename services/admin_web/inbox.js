// inbox.js -- Inbox > All: GM calls, player reports, bug reports and every game's help desk in
// one list, newest first (admininbox.py). The hash is #inbox, or #inbox/person/<id> for one
// person's cases. A help request opens right here (helpdesk.js); a click on any other case
// goes straight to its own tab with that case open, where its actions are.
"use strict";

const IB = { source: "all", state: "waiting", game: "", offset: 0, limit: 50, cur: null, rows: [], seq: 0, last: null };
const IB_SRC_TAB = { gm: "gmcalls", report: "reports", issue: "issues", help: "gamehelp" };

function ibPersonOfHash() {
  const m = /^person\/(\d+)$/.exec(parseHash().path || "");
  return m ? m[1] : "";
}

async function loadInbox() {
  const seq = ++IB.seq;
  const person = ibPersonOfHash();
  const qs = new URLSearchParams({ source: IB.source, state: IB.state, game: IB.game,
                                   offset: IB.offset, limit: IB.limit });
  if (person) qs.set("person", person);
  if (!IB.last) $("#ibList").innerHTML = '<li class="empty">Loading...</li>';
  let r;
  try { r = await api("/api/inbox?" + qs); }
  catch (e) { if (seq === IB.seq) $("#ibList").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  if (seq !== IB.seq) return;
  IB.last = r;
  if (!person && !IB.game) ibPaintCounts(r.waiting);
  else loadInboxCounts();
  IB.rows = r.threads || [];
  ibRenderBar(r);
  ibRenderList(r);
  const cur = IB.cur && IB.rows.find((t) => ibKey(t) === IB.cur);
  if (cur) ibOpen(cur, true);
  else { IB.cur = null; delete $("#ibPane").dataset.game; $("#ibPane").innerHTML = ""; }
  ibPaneShown(!!cur);
}

// Nothing open: no empty reading pane, the list takes the whole width.
function ibPaneShown(on) {
  $("#ibPane").classList.toggle("idle", !on);
  $("#tab-inbox .ib-split").classList.toggle("solo", !on);
}

const ibKey = (t) => `${t.source}|${t.game}|${t.id}`;

function ibRenderBar(r) {
  const waiting = r.waiting || {};
  const total = Object.values(waiting).reduce((a, b) => a + b, 0);
  const n = (k) => (k === "all" ? total : waiting[k] || 0);
  const tabs = [{ key: "all", title: "Every source" }].concat(r.sources || []);
  if (IB.source !== "all" && !tabs.some((t) => t.key === IB.source)) IB.source = "all";
  $("#ibSource").innerHTML = tabs.map((t) => `<option value="${esc(t.key)}">${esc(t.title)}`
    + `${n(t.key) ? ` (${n(t.key)} waiting)` : ""}</option>`).join("");
  $("#ibSource").value = IB.source;
  $("#ibSource").hidden = tabs.length < 3;
  document.querySelectorAll("#ibState button").forEach((b) => b.classList.toggle("on", b.dataset.s === IB.state));
  const sel = $("#ibGame");
  const games = r.games || [];
  if (IB.game && !games.some((g) => g.key === IB.game)) games.push({ key: IB.game, title: IB.game });
  sel.innerHTML = `<option value="">Any game</option>` + games.map((g) => `<option value="${esc(g.key)}">${esc(g.title)}</option>`).join("");
  sel.value = IB.game;
  sel.hidden = games.length < 2 && !IB.game;
  $("#ibWho").innerHTML = r.person ? `<span class="ib-who">Cases of <a href="#people/${r.person.id}">${esc(r.person.name)}</a>`
    + `<button type="button" id="ibWhoX" aria-label="Show everyone's cases" title="Everyone's cases">&times;</button></span>` : "";
  const probs = r.problems || [];
  $("#ibProblems").textContent = probs.length ? probs.join(". ") + "." : "";
  $("#ibProblems").hidden = !probs.length;
}

// "Game help, Kart Club. Arden, 53 min ago": where it came from, who, when.
function ibWho(t, can_link) {
  const p = t.person;
  if (p && p.id) return `<a href="#people/${p.id}">${esc(p.name)}</a>`;
  const lg = t.login || {};
  const who = esc(t.who || lg.name || "someone");
  const link = can_link && lg.system && lg.system !== "pol" && IB.last && IB.last.people
    ? ` (<button type="button" class="ib-link" data-ib-link="${esc(ibKey(t))}">link to a person</button>)` : "";
  return who + link;
}

function ibMeta(t, can_link) {
  const when = t.at ? ago(t.at * 1000) : "";
  const who = ibWho(t, can_link);
  if (t.source === "gm") return `${esc(t.game_title)}. From ${who}${when ? ", " + esc(when) : ""}`;
  if (t.source === "report") return `${esc(t.source_title)}, ${esc(t.game_title)}. ${t.about ? "About " + esc(t.about) + ", from " : ""}${who}${when ? ", " + esc(when) : ""}`;
  if (t.source === "issue") return `${esc(t.source_title)}, ${esc(t.game_title)}. ${who}${t.about ? ", " + esc(t.about) : ""}${when ? ", " + esc(when) : ""}`;
  return `${esc(t.source_title)}, ${esc(t.game_title)}${t.about ? " (" + esc(t.about) + ")" : ""}. ${who}${when ? ", " + esc(when) : ""}`;
}

const ibTitle = (t) => t.source === "gm" ? "GM call (confidential)" : (t.subject || "(no subject)");
const ibState = (t) => t.state === "open" ? "waiting" : t.state;
const IB_STATE_WORD = { waiting: "Waiting", answered: "Answered", closed: "Closed" };

function ibRows(rows, can_link) {
  return rows.map((t) => `<li class="ib-row${ibKey(t) === IB.cur ? " on" : ""}" data-ib="${esc(ibKey(t))}" tabindex="0">`
    + `<div class="ib-top"><b>${esc(ibTitle(t))}</b><span class="ib-st ${ibState(t)}">${esc(IB_STATE_WORD[ibState(t)] || ibState(t))}</span></div>`
    + `<div class="ib-meta">${ibMeta(t, can_link)}</div></li>`).join("");
}

function ibRenderList(r) {
  const empty = { waiting: "Nobody is waiting on us.", open: "Nothing open.",
                  closed: "Nothing closed.", all: "Nothing here yet." }[IB.state] || "Nothing here.";
  const none = !(r.sources || []).length ? "You can't read any of the Inbox's sources." : empty;
  $("#ibList").innerHTML = ibRows(IB.rows, r.can_link) || `<li class="empty">${esc(none)}</li>`;
  const from = r.total ? r.offset + 1 : 0, to = r.offset + IB.rows.length;
  $("#ibPager").innerHTML = r.total > r.limit
    ? `<button class="ghost sm" data-page="-1"${r.offset ? "" : " disabled"}>Newer</button>`
      + `<span>${from} to ${to} of ${r.total}</span>`
      + `<button class="ghost sm" data-page="1"${to < r.total ? "" : " disabled"}>Older</button>` : "";
}

// ---- the reading pane: help requests. Every other case opens in its own tab. ----
async function ibOpen(t, quiet) {
  if (t.source !== "help") return ibGoTo(t);
  IB.cur = ibKey(t);
  ibPaneShown(true);
  document.querySelectorAll("#ibList li[data-ib]").forEach((li) => li.classList.toggle("on", li.dataset.ib === IB.cur));
  const pane = $("#ibPane");
  const can_link = IB.last && IB.last.can_link;
  const who = ibWho(t, can_link);
  const others = `<span class="ib-others"></span>`;
  const head = (th) => `<p class="muted">Game help in ${esc(th.game_title)}, from ${who}`
    + `${th.who.online === true ? " (online now)" : ""}.${th.closed ? " Closed." : ""} ${others}</p>`;
  const got = await renderHelpThread(pane, t.game, t.id, quiet, loadInbox, head);
  if (got) ibOthers(t);
  if (!quiet && got && window.matchMedia("(max-width: 760px)").matches) pane.scrollIntoView({ block: "start" });
}

// "Their other open cases": the same person's open cases from every source.
async function ibOthers(t) {
  const el = $("#ibPane .ib-others");
  if (!el || !t.person || !t.person.id) return;
  let r;
  try { r = await api(`/api/inbox?person=${t.person.id}&state=open&limit=6`); } catch (e) { return; }
  if (IB.cur !== ibKey(t)) return;
  const rest = (r.threads || []).filter((x) => ibKey(x) !== ibKey(t));
  const box = $("#ibPane .ib-others");
  if (!box) return;
  box.innerHTML = rest.length
    ? `Their other open cases: <a href="#inbox/person/${t.person.id}">${rest.length > 5 ? "more than 5" : rest.length}</a>.`
    : "Their other open cases: none.";
}

function ibGoTo(t) {
  const tab = IB_SRC_TAB[t.source];
  if (t.source === "gm") GM_SEL = t.id;
  if (t.source === "report") REPORT_SEL = t.id;
  if (t.source === "issue") ISSUE_SEL = t.id;
  if (t.source === "help") GH.cur = { game: t.game, id: t.id };
  location.hash = "#" + tab;
}

// ---- the badges: what is waiting on us in each source, the Inbox filter's own numbers ----
// GM calls' badge is the desk's too (gmBadge, every 4 s while it is open): "open" there is
// the same thing, a caller nobody has answered yet.
function ibPaintCounts(w) {
  w = w || {};
  for (const [src, sel] of [["gm", "#gmBadge"], ["report", "#reportsBadge"], ["issue", "#issuesBadge"], ["help", "#helpBadge"]]) {
    const b = $(sel), n = w[src] || 0;
    if (!b) continue;
    b.textContent = n; b.hidden = !n;
    b.title = n ? `${n} waiting on us` : "";
  }
}
let IB_COUNT_SEQ = 0;
async function loadInboxCounts() {
  const seq = ++IB_COUNT_SEQ;
  let r;
  try { r = await api("/api/inbox?state=waiting&limit=1"); } catch (e) { return; }
  if (seq === IB_COUNT_SEQ) ibPaintCounts(r.waiting);
}

// ---- events ----
$("#tab-inbox").addEventListener("click", (e) => {
  const lb = e.target.closest("[data-ib-link]");
  if (lb) {
    e.stopPropagation();
    const t = IB.rows.find((x) => ibKey(x) === lb.dataset.ibLink);
    return t && openLoginLinkDialog(t.login, loadInbox);
  }
  if (e.target.closest("a")) return;
  const st = e.target.closest("#ibState button[data-s]");
  if (st) { IB.state = st.dataset.s; IB.offset = 0; return loadInbox(); }
  const pg = e.target.closest("#ibPager button[data-page]");
  if (pg) { IB.offset = Math.max(0, IB.offset + (+pg.dataset.page) * IB.limit); return loadInbox(); }
  if (e.target.closest("#ibWhoX")) { location.hash = "#inbox"; return; }
  const li = e.target.closest("#ibList li[data-ib]");
  if (li) { const t = IB.rows.find((x) => ibKey(x) === li.dataset.ib); if (t) ibOpen(t); }
});
$("#ibList").addEventListener("keydown", (e) => {
  const li = e.target.closest("li[data-ib]");
  if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); li.click(); }
});
$("#ibSource").addEventListener("change", (e) => { IB.source = e.target.value; IB.offset = 0; loadInbox(); });
$("#ibGame").addEventListener("change", (e) => { IB.game = e.target.value; IB.offset = 0; loadInbox(); });
