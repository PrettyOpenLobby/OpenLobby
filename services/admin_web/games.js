// games.js -- the Games section: every game module on this server, and a page
// per game (status, who is playing, services, actions, and the game's own
// tools). Data: /api/modules (adminmodules), /api/ops (the host checker).
//
// Loaded before app.js, which owns the shared helpers ($, esc, api, toast,
// writeHash, can, isOwner, runFix, ago); nothing here runs until app.js calls
// loadGames.
"use strict";

// #games              every game
// #games/ffxi         a game's overview
// #games/ffxi/chars   one of its tools (the same hash the old Games tab used)
let GAMES = [];          // /api/modules -> modules
let GAMES_OPS = null;    // /api/ops, or null without the health permission
let GAME_TOOL = "";      // the hash path last shown, so a tab click returns to it
let GAMES_TIMER = null;
let GAMES_WIRED = false;

const canSeeGames = () => isOwner() || (SESSION.perms || []).some((p) => p.startsWith("game.") || p === "pml");

async function fetchGames(fresh) {
  if (!GAMES_WIRED) wireGames();
  const [mods, ops] = await Promise.all([
    api("/api/modules" + (fresh ? "?fresh=1" : "")).catch((e) => { toast(e.message, true); return { modules: [] }; }),
    can("health") ? api("/api/ops").catch(() => null) : Promise.resolve(null),
  ]);
  GAMES = mods.modules || [];
  GAMES_OPS = ops && ops.available ? ops : null;
  GAMES_LOADED = true;
}

async function loadGames(refresh) {
  const want = parseHash().path || GAME_TOOL;
  await fetchGames(refresh);
  openGameTool(want, refresh);
  clearInterval(GAMES_TIMER);
  // the status changes while you look at it; the tool frames are left alone
  GAMES_TIMER = setInterval(async () => {
    if (CUR_TAB !== "games" || document.hidden) return;
    const [key, tool] = (GAME_TOOL || "").split("/");
    if (tool) return;
    if (gamesBusy() || gamesSelecting()) return;
    await fetchGames(false);
    renderGamesSide();
    if (gamesBusy() || gamesSelecting()) return;
    keepGamesView(() => { if (key) renderGame(key); else renderGameGrid(); });
  }, 10000);
}

// ---- status: one word and one colour per game ----
function opsCheck(key) {
  return GAMES_OPS ? (GAMES_OPS.checks || []).find((c) => c.id === key) || null : null;
}

// One set of health words for the pill, the stat tile and the card.
const HEALTH = { ok: ["good", "OK"], warn: ["warn", "Check"], bad: ["bad", "Trouble"],
                 down: ["bad", "Down"], unknown: ["off", "Unknown"] };
const health = (state) => HEALTH[state] || HEALTH.unknown;
const opsHealth = (ops) => health(ops.state === "ok" || ops.state === "warn" || ops.state === "bad" ? ops.state : "unknown");

function gameState(g) {
  const live = g.live, ops = opsCheck(g.key);
  const st = (h, why) => ({ cls: HEALTH[h][0], word: HEALTH[h][1], why: why || "" });
  if (ops && ops.state === "bad") return st("bad", ops.detail);
  if (live && live.up === false) return st("down", live.error);
  if (live && live.error) return st("warn", "Couldn't read its status.");
  if (ops && ops.state === "warn") return st("warn", ops.detail);
  if (!live && !ops) return st("unknown", "No live status.");
  return st("ok", "");
}

const num = (v) => (v === null || v === undefined ? "-" : String(v));
const factText = (f) => f.kind === "time" && typeof f.value === "number"
  ? new Date(f.value * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
  : String(f.value ?? "");

// ---- the sidebar's list of games (under Games) ----
// The PlayOnline Viewer first (it is the platform, not a title), then the games by where
// they run, each group alphabetical by the name shown, then the platform's own modules
// (kind "platform", such as the patch versions) last. The group is read from the module's platform.
const GAME_GROUPS = [["viewer", ""], ["pol", "PlayOnline"], ["ps2", "PlayStation 2"], ["other", "Other"],
                     ["platform", "Platform"]];
function gameGroup(g) {
  const p = String(g.platform || "").toLowerCase();
  if (g.key === "viewer") return "viewer";
  if (g.kind === "platform") return "platform";
  if (p === "playonline platform") return "viewer";
  if (p.startsWith("playonline")) return "pol";
  if (p.startsWith("ps2") || p.includes("playstation 2")) return "ps2";
  return "other";
}
const isGame = (g) => g.kind !== "platform";
// `shown` is the name the list shows: the sidebar shows the short name, the grid the title
function groupedGames(shown) {
  const name = (g) => String(shown(g) || g.key).toLowerCase();
  const groups = GAME_GROUPS.map(([id, label]) => [label, GAMES.filter((g) => gameGroup(g) === id)
    .sort((a, b) => name(a).localeCompare(name(b)))]).filter(([, gs]) => gs.length);
  // without the PlayOnline core one heading over every game says nothing
  return coreOff() && groups.length === 1 ? [["", groups[0][1]]] : groups;
}
const sideName = (g) => g.short || g.title;

// ---- keeping what the reader was doing across a status redraw ----
// The 10 s poll rebuilds #gamesMain. While text in it is selected the redraw waits; scroll
// positions of its scrolling boxes, open <details> and the focused element come back after it.
function gamesSelecting() {
  const sel = window.getSelection && window.getSelection();
  if (!sel || sel.isCollapsed || !String(sel).trim()) return false;
  const main = $("#gamesMain");
  return !!main && (main.contains(sel.anchorNode) || main.contains(sel.focusNode));
}
function keepGamesView(draw) {
  const main = $("#gamesMain");
  const boxes = (sel) => [...main.querySelectorAll(sel)];
  const keyOf = (el, i) => el.dataset.keep || el.id || String(i);
  const scrolls = {}, opens = new Set();
  boxes(".gx-scroll, .tablewrap").forEach((el, i) => { scrolls[keyOf(el, i)] = [el.scrollTop, el.scrollLeft]; });
  boxes("details[open]").forEach((el, i) => opens.add(keyOf(el, i)));
  const f = document.activeElement && main.contains(document.activeElement) ? document.activeElement : null;
  const fsel = f && f.id ? "#" + CSS.escape(f.id) : f && f.dataset.game !== undefined ? `[data-game="${CSS.escape(f.dataset.game)}"]`
    : f && f.dataset.act ? `[data-act="${CSS.escape(f.dataset.act)}"]` : "";
  const top = window.scrollY;
  draw();
  boxes(".gx-scroll, .tablewrap").forEach((el, i) => {
    const s = scrolls[keyOf(el, i)];
    if (s) { el.scrollTop = s[0]; el.scrollLeft = s[1]; }
  });
  boxes("details").forEach((el, i) => { if (opens.has(keyOf(el, i))) el.open = true; });
  if (fsel) { const n = main.querySelector(fsel); if (n) n.focus({ preventScroll: true }); }
  if (window.scrollY !== top) window.scrollTo(0, top);
  labelTables(main);
}
// a phone shows card tables with each cell labelled by its column (app.js labelCells);
// tables drawn after the page loaded are labelled here
function labelTables(root) {
  if (typeof labelCells !== "function" || !root) return;
  root.querySelectorAll("table.cards > tbody").forEach((tb) => labelCells(tb));
}

function renderGamesSide() {
  const cur = CUR_TAB === "pml" ? "viewer" : CUR_TAB === "games" ? (GAME_TOOL || "").split("/")[0] : null;
  $("#gamesList").innerHTML =
    `<button class="gx-item${cur === "" ? " on" : ""}" data-game="">All games</button>`
    + groupedGames(sideName).map(([label, gs]) => (label ? `<div class="gx-group">${esc(label)}</div>` : "") + gs.map((g) => {
      const st = gameState(g);
      const on = g.live && typeof g.live.online === "number" ? g.live.online : "";
      return `<button class="gx-item${cur === g.key ? " on" : ""}" data-game="${esc(g.key)}">`
        + `<span class="dot ${st.cls}" aria-hidden="true"></span>${esc(g.short || g.title)}`
        + `<span class="n">${esc(on)}</span></button>`;
    }).join("")).join("");
}

// ---- every game ----
function renderGameGrid() {
  GAME_SHOWN = null;
  const games = GAMES.filter(isGame);
  const total = games.reduce((n, g) => n + (g.live && typeof g.live.online === "number" ? g.live.online : 0), 0);
  const card = (g, group) => {
    const st = gameState(g), live = g.live || {};
    const ops = opsCheck(g.key);
    // only the numbers the game reports: no dashes for what it cannot tell
    const nums = [
      typeof live.online === "number" ? [live.online, "online"] : null,
      Array.isArray(live.rooms) ? [live.rooms.length, live.rooms_label || "rooms"] : null,
      ops ? [opsHealth(ops)[1], "services"] : null,
    ].filter(Boolean);
    const line = live.summary || st.why || g.about || "";
    return `<button class="panel gx-card" data-game="${esc(g.key)}">`
      + `<span class="gx-head"><span class="dot ${st.cls}" aria-hidden="true"></span>`
      + `<span class="gx-name">${esc(g.title)}<small>${esc(g.platform || group || "")}</small></span>`
      + `<span class="pill ${st.cls}">${esc(st.word)}</span></span>`
      + (nums.length ? `<span class="gx-nums">${nums.map(([v, k]) => `<span><b>${esc(v)}</b>${esc(k)}</span>`).join("")}</span>` : "")
      + (line ? `<span class="gx-line">${esc(line)}</span>` : "")
      + `</button>`;
  };
  // one grid across the width, in the sidebar's order (by where they run, then by title)
  const cards = groupedGames((g) => g.title).map(([label, gs]) => gs.map((g) => card(g, label)).join("")).join("");
  $("#gamesMain").innerHTML =
    `<div class="gx-top"><span class="muted">${games.length} game${games.length === 1 ? "" : "s"}, `
    + `${total} online</span></div>`
    + (cards ? `<div class="gx-grid">${cards}</div>`
      : `<div class="panel muted">No games yet.${isOwner()
        ? ' <a href="#modules">Add a game</a>' : ""}</div>`);
  labelTables($("#gamesMain"));
}

// ---- one game ----
function gameTabs(g, active) {
  const tabs = [`<a href="#games/${esc(g.key)}" class="${active ? "" : "on"}">Overview</a>`];
  // a page the panel draws itself (PML Preview) lives on its own tab for now
  const href = (t) => t.panel ? "#" + t.id : `#games/${g.key}/${t.id}`;
  const tab = (t, label, on) => `<a href="${esc(href(t))}" class="${on ? "on" : ""}">${esc(label)}</a>`;
  // tools of one group share a tab (it opens the first of them); while one of them is open,
  // the group's tools are a row under the bar. A group cut to one tool is just that tool.
  const done = new Set();
  let sub = "";
  for (const t of g.tools) {
    const members = t.group ? g.tools.filter((x) => x.group === t.group) : [t];
    if (members.length < 2) { tabs.push(tab(t, t.label, active === t.id)); continue; }
    if (done.has(t.group)) continue;
    done.add(t.group);
    const open = members.some((x) => x.id === active);
    tabs.push(tab(t, t.group, open));
    if (open) sub = `<nav class="gx-subtabs" aria-label="${esc(t.group)}">`
      + members.map((x) => tab(x, x.label, x.id === active)).join("") + `</nav>`;
  }
  if (canGameLogs(g)) tabs.push(`<a href="#games/${esc(g.key)}/logs" class="${active === "logs" ? "on" : ""}">Logs</a>`);
  // a bar with only Overview on it is no choice at all
  return tabs.length > 1 ? `<nav class="gx-tabs" aria-label="${esc(g.title)}">${tabs.join("")}</nav>${sub}` : "";
}

// the game's own update, as a pill beside its name when there is nothing to do about it
function gameUpdatePill(g) {
  const u = (g.live || {}).update;
  if (!u || u.apply !== true || !can(`game.${g.key}.update`)) return "";
  if (u.update === "current" && u.state !== "failed" && u.state !== "updating" && !APPLY[g.key])
    return ` <span class="pill good">Up to date</span>`;
  return "";
}

function gameHeader(g) {
  const st = gameState(g), live = g.live || {};
  const age = live.at ? "Status " + ago(live.at * 1000) : "";
  const sub = [g.platform, g.about].filter(Boolean).join(". ");
  return `<div class="gx-crumb"><a href="#games">Games</a> / ${esc(g.title)}</div>`
    + `<div class="gx-top"><h2 class="gx-title">${esc(g.title)}`
    + ` <span class="pill ${st.cls}"${st.why ? ` title="${esc(st.why)}"` : ""}><span class="dot ${st.cls}" aria-hidden="true"></span>${esc(st.word)}</span>`
    + `${gameUpdatePill(g)}</h2>`
    + `<span class="muted">${esc(sub)}${sub && age ? " · " : ""}${esc(age)}</span></div>`
    + (st.why && st.cls !== "good" ? `<div class="gx-why ${st.cls}">${esc(st.why)}</div>` : "");
}

// the verb on an action's button: the card already says what it is
const ACT_VERB = { kick: "Kick", announce: "Send", message: "Send", ban: "Ban", unban: "Lift ban", mute: "Mute",
                   unmute: "Unmute", warn: "Warn", restart: "Restart", gift: "Give", give: "Give" };
function actionVerb(a) {
  if (ACT_VERB[a.kind]) return ACT_VERB[a.kind];
  // "Kick a player", "Give the item", "Start a war": the first word is the verb
  const w = String(a.label || "").split(/\s+/);
  if (w.length > 1 && /^(a|an|the|all|every|this|one|someone|their|his|her|my)$/i.test(w[1])) return w[0];
  if (/^(start|stop|end|give|send|set|reset|clear|close|open|show|hide|lift|restart)$/i.test(w[0])) return cap(w[0].toLowerCase());
  return ACT_VERB[String(a.id || "").toLowerCase()] || (w.length === 1 && w[0] ? w[0] : "Run");
}

function renderGame(key, toolId) {
  const g = GAMES.find((x) => x.key === key);
  if (!g) return renderGameGrid();
  const head = gameHeader(g) + gameTabs(g, toolId);
  GAME_SHOWN = toolId ? null : key;
  if (toolId === "logs") return renderGameLogs(g, head);
  if (toolId) {
    const t = g.tools.find((x) => x.id === toolId);
    const src = "/games/" + g.key + "/" + t.path;
    const old = $("#gamesFrame");
    if (old && old.dataset.src === src) return;    // keep the tool's own state
    $("#gamesMain").innerHTML = head
      + `<iframe class="gx-frame" id="gamesFrame" title="${esc(g.title + ": " + t.label)}"`
      + ` data-src="${esc(src)}" src="${esc(src)}"></iframe>`;
    return;
  }
  const live = g.live || {};
  const players = live.players || [], facts = live.facts || [];
  const rooms = Array.isArray(live.rooms) ? live.rooms : null;
  const counted = typeof live.online === "number";
  const ops = opsCheck(g.key);
  // the numbers the game reports, its facts beside them; nothing for what it does not report
  const stats = [];
  if (counted) stats.push(["Online now", String(live.online), players.length && players.length < live.online ? `${players.length} listed` : ""]);
  if (rooms) stats.push([cap(live.rooms_label || "rooms"), String(rooms.length), ""]);
  if (ops) stats.push(["Services", opsHealth(ops)[1], ops.restarted ? "last start " + ago(ops.restarted.at * 1000) : ""]);
  if (live.summary) stats.push(["Now", live.summary, ""]);
  facts.forEach((f) => stats.push([f.label, factText(f), ""]));
  const statHtml = stats.map(([k, v, s]) => `<div class="panel gx-stat"><div class="k">${esc(k)}</div>`
    + `<div class="v${String(v).length > 12 ? " long" : ""}">${esc(v)}</div>${s ? `<div class="s">${esc(s)}</div>` : ""}</div>`).join("");

  // the boxes that sit side by side; an odd one out takes the whole row
  const halves = [];
  const playerRows = players.map((p) => `<tr><td class="strong">${esc(p.name)}</td><td>${esc(p.where)}</td>`
    + `<td class="muted">${esc(p.detail)}</td></tr>`).join("");
  // only with names, or a count of nobody: a count alone is already in the tile above
  if (playerRows || (counted && live.online === 0)) {
    halves.push(`<section class="panel"><h3>Who is playing <span class="muted">${esc(num(live.online))}</span></h3>`
      + (playerRows ? `<div class="tablewrap gx-scroll" data-keep="players"><table class="cards"><thead><tr><th>Player</th><th>Where</th><th>Detail</th></tr></thead><tbody>${playerRows}</tbody></table></div>`
        : `<p class="muted">Nobody right now.</p>`)
      + `</section>`);
  }
  if (rooms) {
    halves.push(`<section class="panel"><h3>${esc(cap(live.rooms_label || "rooms"))} <span class="muted">${rooms.length}</span></h3>`
      + (rooms.length ? `<div class="gx-scroll" data-keep="rooms">` + rooms.map((r) => `<div class="gx-row"><div><div class="strong">${esc(r.name)}</div>`
        + `<div class="muted">${esc(r.detail)}</div></div>${r.state ? `<span class="pill">${esc(r.state)}</span>` : ""}</div>`).join("") + `</div>`
        : `<p class="muted">None open right now.</p>`)
      + `</section>`);
  }

  const svcs = g.services || [];
  const fix = GAMES_OPS && GAMES_OPS.can_fix && GAMES_OPS.actions && GAMES_OPS.actions[g.key];
  if (svcs.length) {
    // what each service is, by name; the containers and ports are in the tooltip
    const tech = (s) => s.containers.join(", ") + Object.keys(s.ports || {}).map((p) => " :" + p).join("");
    halves.push(`<section class="panel"><h3>Services${fix
        ? ` <button class="sm ghost" data-fix="${esc(g.key)}">Restart ${esc(g.short || g.title)}</button>` : ""}</h3>`
      + (ops ? `<p class="gx-ops ${esc(ops.state)}"><span class="dot ${opsHealth(ops)[0]}" aria-hidden="true"></span>${esc(ops.detail)}`
        + (ops.restarted ? ` <span class="muted">Last start ${esc(ago(ops.restarted.at * 1000))}: ${esc(ops.restarted.reason || "")}</span>` : "") + `</p>`
        : !can("health") ? "" : `<p class="muted">${GAMES_OPS ? "No health check yet." : "Health checks unavailable."}</p>`)
      + svcs.map((s) => `<div class="gx-row"><div><div class="strong"${isOwner() ? ` title="${esc(tech(s))}"` : ""}>${esc(s.name || g.title)}</div>`
        + `<div class="muted">${esc([...new Set(Object.values(s.ports || {}))].join(", "))}</div></div></div>`).join("")
      + `</section>`);
  }
  if (halves.length % 2) halves[halves.length - 1] = halves[halves.length - 1].replace('<section class="panel"', '<section class="panel gx-wide"');

  const body = gameUpdateBox(g) + halves.join("") + gameActions(g) + gamePatchBox(g);
  $("#gamesMain").innerHTML = head + (statHtml ? `<div class="gx-stats">${statHtml}</div>` : "")
    + `<div class="gx-layout">${body || (statHtml ? "" : `<p class="muted gx-wide">Nothing to show yet.</p>`)}</div>`;
  labelTables($("#gamesMain"));
}

// ---- a game's actions: a button each, under Messages, Players and Server ----
// An action with fields opens them in a dialog (#gxActModal, outside #gamesMain, so the
// status redraw leaves it alone); one without fields runs on the click, after a question
// when it is a danger action.
const ACT_PLAYER_KINDS = /^(kick|ban|unban|mute|unmute|warn|jail|unjail)$/i;
const ACT_MESSAGE_WORDS = /\b(notice|message|announce|announcement|banner|broadcast|chat|motd|tip|news)\b/i;
function actionGroup(a) {
  const words = `${a.id} ${a.label}`;
  if (a.kind === "announce" || a.kind === "message") return "msg";
  if ((a.fields || []).some((f) => f.type === "player") || ACT_PLAYER_KINDS.test(a.kind || "")
      || ACT_PLAYER_KINDS.test(a.id) || ACT_PLAYER_KINDS.test(String(a.label || "").split(/\s+/)[0])
      || /\b(player|character)\b/i.test(a.label || "")) return "players";
  if (ACT_MESSAGE_WORDS.test(words)) return "msg";
  return "server";
}
const ACT_GROUPS = [["msg", "Messages"], ["players", "Players"], ["server", "Server"]];

function gameActions(g) {
  if (!g.actions.length) return "";
  const groups = ACT_GROUPS.map(([id, label]) => [label, g.actions.filter((a) => actionGroup(a) === id)])
    .filter(([, as]) => as.length);
  const btn = (a) => `<button type="button" class="${a.danger ? "danger" : "ghost"} gx-abtn" data-act="${esc(a.id)}"`
    + `${a.about ? ` title="${esc(a.about)}"` : ""}>${esc(a.label)}</button>`;
  const one = groups.length === 1;
  return `<section class="panel gx-wide gx-acts"><h3>Actions</h3>`
    + groups.map(([label, as]) => `<div class="gx-agroup">${one ? "" : `<div class="gx-ahead">${esc(label)}</div>`}`
      + `<div class="gx-abtns">${as.map(btn).join("")}</div></div>`).join("")
    + `</section>`;
}

// who an action is for, as its box shows it: the player fields, or the first field of a
// kick or a ban that asks for a name in plain text
function actWho(form, a) {
  let fs = (a.fields || []).filter((f) => f.type === "player");
  if (!fs.length && (ACT_PLAYER_KINDS.test(a.kind || "") || ACT_PLAYER_KINDS.test(a.id))) fs = (a.fields || []).slice(0, 1);
  return fs.map((f) => {
    const box = form.querySelector(`[data-pk-for="${CSS.escape(f.id)}"] .pk-q`);
    const plain = form.elements[f.id];
    return ((box && box.value) || (plain && plain.value) || "").trim();
  }).filter(Boolean).join(", ");
}

// a game that refuses answers 502 with its reason in `msg`; that reason is the error shown
async function postAction(key, a, params) {
  const opts = { method: "POST", headers: { "Content-Type": "application/json" },
                 body: JSON.stringify({ key, action: a.id, params }) };
  API_BUSY++;
  let r;
  try { r = await fetch("/api/modules/action", opts); } finally { API_BUSY--; }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && j.auth) { location.reload(); throw new Error("signed out"); }
  if (!r.ok) throw new Error(j.error || j.msg || r.statusText);
  return j;
}

async function runBareAction(g, a, btn) {
  if (a.danger && !(await uiAsk({ title: `${a.label}?`, text: a.about || "", ok: actionVerb(a), danger: true }))) return;
  btn.disabled = true;
  try { toast((await postAction(g.key, a, {})).msg || "Done."); }
  catch (err) { toast(err.message, true); }
  btn.disabled = false;
}

let ACT_OPEN = null;     // { key, a, form, from, armed } while the action dialog is up
function openActionModal(g, a, from) {
  let m = $("#gxActModal");
  if (!m) {
    m = document.createElement("div");
    m.className = "modal"; m.id = "gxActModal";
    document.body.appendChild(m);
    m.addEventListener("mousedown", (e) => { if (e.target === m) closeActionModal(); });
    m.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !e.defaultPrevented) { e.stopPropagation(); closeActionModal(); }
    });
  }
  const pick = !!g.players;
  const field = (f, i) => {
    const id = `gxAF${i}`;
    return `<label for="${id}">${esc(f.label || f.id)}</label>`
      + (f.type === "player" && pick ? `<span data-pk="${esc(f.id)}" data-pk-id="${id}"></span>`
        : `<input type="text" id="${id}" name="${esc(f.id)}" autocomplete="off" spellcheck="false">`);
  };
  m.innerHTML = `<form class="card gx-amodal" role="dialog" aria-modal="true" aria-labelledby="gxATitle" novalidate>`
    + `<h3 id="gxATitle">${esc(a.label)}</h3>${a.about ? `<p>${esc(a.about)}</p>` : ""}`
    + (a.fields || []).map(field).join("")
    + `<p class="gx-aerr" id="gxAErr" role="alert" hidden></p>`
    + `<div class="foot"><span class="grow gx-aask" id="gxAAsk"></span>`
    + `<button type="button" class="ghost" id="gxANo">Cancel</button>`
    + `<button type="submit" class="${a.danger ? "danger" : "act"}" id="gxAYes">${esc(actionVerb(a))}</button></div></form>`;
  const form = m.querySelector("form");
  m.querySelectorAll("[data-pk]").forEach((slot) => {
    const pk = makePicker({
      name: slot.dataset.pk, id: slot.dataset.pkId, placeholder: "Search players",
      search: (q, limit) => api(`/api/games/players?game=${encodeURIComponent(g.key)}`
        + `&q=${encodeURIComponent(q)}&limit=${limit}`).then((r) => r.players || []),
      onpick: () => actChanged(),
    });
    pk.el.dataset.pkFor = slot.dataset.pk;
    slot.replaceWith(pk.el);
  });
  ACT_OPEN = { key: g.key, a, form, from, armed: false };
  form.addEventListener("input", actChanged);
  // the picker's clear button and Escape empty the box without an input event
  form.addEventListener("click", (e) => { if (e.target.closest(".pk-x")) setTimeout(actChanged); });
  form.addEventListener("keyup", (e) => { if (e.key === "Escape") actChanged(); });
  form.addEventListener("submit", submitAction);
  $("#gxANo").onclick = closeActionModal;
  actChanged();
  m.classList.add("show");
  const first = form.querySelector("input:not([type=hidden])");
  setTimeout(() => (first || $("#gxAYes")).focus(), 0);
}

function closeActionModal() {
  const m = $("#gxActModal");
  if (!m || !m.classList.contains("show")) return;
  m.classList.remove("show");
  const from = ACT_OPEN && ACT_OPEN.from;
  ACT_OPEN = null;
  m.innerHTML = "";
  if (from && from.isConnected) from.focus({ preventScroll: true });
  else if (from && from.dataset.act) {
    const b = document.querySelector(`#gamesMain [data-act="${CSS.escape(from.dataset.act)}"]`);
    if (b) b.focus({ preventScroll: true });
  }
}

// the danger button names who it is for ("Kick Arden"); a danger action without a player
// asks once more inside the dialog before it runs
function actChanged() {
  const o = ACT_OPEN;
  if (!o) return;
  const yes = $("#gxAYes"), ask = $("#gxAAsk");
  $("#gxAErr").hidden = true;
  const who = actWho(o.form, o.a);
  o.armed = false;
  ask.textContent = "";
  yes.textContent = o.a.danger && who ? `${actionVerb(o.a)} ${who}` : actionVerb(o.a);
  if (o.a.danger) yes.className = who ? "danger solid" : "danger";
}

async function submitAction(e) {
  e.preventDefault();
  const o = ACT_OPEN;
  if (!o) return;
  const yes = $("#gxAYes");
  if (yes.disabled) return;
  const params = Object.fromEntries(new FormData(o.form).entries());
  // a danger button that names who it is for has asked already
  if (o.a.danger && !actWho(o.form, o.a) && !o.armed) {
    o.armed = true;
    $("#gxAAsk").textContent = `${o.a.label}?`;
    yes.className = "danger solid";
    yes.focus();
    return;
  }
  yes.disabled = true;
  try {
    const r = await postAction(o.key, o.a, params);
    if (ACT_OPEN !== o) return;
    closeActionModal();
    toast(r.msg || "Done.");
  } catch (err) {
    if (ACT_OPEN !== o) return;
    actChanged();
    $("#gxAErr").textContent = err.message;
    $("#gxAErr").hidden = false;
  }
  yes.disabled = false;
}

// typing or picking on the game page: the status redraw waits
const gamesBusy = () => !!(document.activeElement && document.activeElement.closest
  && document.activeElement.closest("#gamesMain input, #gamesMain textarea, #gamesMain select"));

const cap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);

// ---- a game's server logs (the host runner reads them: pol-ops, data/ops/logs) ----
// The owner sees everything; a moderator with the game's serverlogs permission
// sees them with players' addresses masked by the panel.
const canGameLogs = (g) => (g.services || []).length > 0 && (isOwner() || can(`game.${g.key}.serverlogs`));
let LOGS = { key: "", doc: null, shown: 0, filter: "", busy: false, timer: null };

async function renderGameLogs(g, head) {
  if (LOGS.key !== g.key) LOGS = { key: g.key, doc: null, shown: 0, filter: "", busy: false, timer: null };
  $("#gamesMain").innerHTML = head + `<section class="panel"><div class="lg-tools">`
    + `<button class="act sm" id="lgFresh">Refresh</button><span class="muted" id="lgWhen"></span>`
    + `<span class="grow"></span><input type="text" id="lgFilter" autocomplete="off" placeholder="Filter" value="${esc(LOGS.filter)}"></div>`
    + `<div class="lg-ctrs" id="lgCtrs"></div><pre class="deploy-log lg-pre" id="lgPre">Loading...</pre></section>`;
  let r;
  try { r = await api("/api/logs?group=" + encodeURIComponent(g.key)); }
  catch (e) { $("#lgPre").textContent = e.message; return; }
  LOGS.doc = r.logs;
  LOGS.addresses = r.addresses;
  LOGS.available = r.available;
  drawLogs();
  if (r.available && (!r.logs || Date.now() / 1000 - r.logs.at > 60)) fetchLogs();
}

function drawLogs() {
  const d = LOGS.doc;
  if (!$("#lgPre")) return;
  $("#lgFresh").disabled = LOGS.busy || !LOGS.available;
  $("#lgWhen").textContent = LOGS.busy ? "Loading..."
    : !LOGS.available ? "Logs unavailable."
    : d ? `Updated ${ago(d.at * 1000)}` : "";
  const ctrs = (d && d.containers) || [];
  $("#lgCtrs").innerHTML = ctrs.length > 1 ? ctrs.map((c, i) => `<button class="cal-chip${i === LOGS.shown ? "" : " off-lite"}" data-lgctr="${i}">`
    + `${esc(c.name)}${c.missing ? " · missing" : ""}</button>`).join("") : "";
  const c = ctrs[Math.min(LOGS.shown, ctrs.length - 1)];
  if (!c) { $("#lgPre").textContent = LOGS.available ? "No logs yet." : ""; return; }
  const f = LOGS.filter.toLowerCase();
  const lines = (c.lines || []).filter((l) => !f || l.toLowerCase().includes(f));
  const pre = $("#lgPre");
  // follow the end only when the reader is already there (or it is the first text)
  const stick = !pre.dataset.drawn || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  pre.textContent = c.missing ? `${c.name} not found.` : lines.join("\n") || (f ? "No matches." : "Nothing logged.");
  pre.dataset.drawn = "1";
  if (stick) pre.scrollTop = pre.scrollHeight;
}

async function fetchLogs() {
  if (LOGS.busy) return;
  const key = LOGS.key, asked = Date.now() / 1000;
  LOGS.busy = true; drawLogs();
  try {
    await api("/api/logs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group: key }) });
    for (let i = 0; i < 30 && LOGS.key === key; i++) {
      await new Promise((ok) => setTimeout(ok, 1000));
      const r = await api("/api/logs?group=" + encodeURIComponent(key));
      if (r.logs && r.logs.at >= asked - 1) { LOGS.doc = r.logs; break; }
    }
  } catch (e) { toast(e.message, true); }
  LOGS.busy = false;
  if (LOGS.key === key) drawLogs();
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#lgFresh")) return fetchLogs();
  const b = e.target.closest("[data-lgctr]");
  if (b) { LOGS.shown = +b.dataset.lgctr; drawLogs(); }
});
document.addEventListener("input", (e) => { if (e.target.id === "lgFilter") { LOGS.filter = e.target.value; drawLogs(); } });

// ---- navigation ----
// `id` is the hash path: "", "<key>" or "<key>/<tool>". Unknown parts fall
// back to the nearest page that exists.
let GAMES_LOADED = false;

function openGameTool(id, reload) {
  // before the list has come in (a link straight to #games/<key>), remember
  // where to go; loadGames opens it
  if (!GAMES_LOADED) { GAME_TOOL = String(id || ""); return; }
  let [key, tool] = String(id || "").split("/");
  const g = GAMES.find((x) => x.key === key);
  if (!g) key = tool = "";
  else if (tool && !g.tools.some((t) => t.id === tool && !t.panel) && !(tool === "logs" && canGameLogs(g))) tool = "";
  if (reload && $("#gamesFrame")) $("#gamesFrame").dataset.src = "";
  GAME_TOOL = key ? key + (tool ? "/" + tool : "") : "";
  writeHash("games", GAME_TOOL);
  renderGamesSide();
  if (key) renderGame(key, tool); else renderGameGrid();
}

// The PML preview is the Viewer's page; the sidebar marks the Viewer while it
// is open.
function markViewerInSide() {
  if (!GAMES_LOADED) return;
  renderGamesSide();
  // PML Preview is drawn by the panel, outside #gamesMain; it still gets the Viewer's
  // heading and tabs, so the Viewer's other pages are one click away
  const g = GAMES.find((x) => x.tools.some((t) => t.panel && t.id === "pml"));
  $("#pmlHead").innerHTML = g && CUR_TAB === "pml" ? gameHeader(g) + gameTabs(g, "pml") : "";
}

function wireGames() {
  GAMES_WIRED = true;
  const go = (e) => {
    const b = e.target.closest("[data-game]");
    if (!b) return;
    const want = "#games" + (b.dataset.game ? "/" + b.dataset.game : "");
    if (location.hash === want) openGameTool(b.dataset.game);
    else location.hash = want;
    document.body.classList.remove("sideopen");
  };
  $("#gamesList").addEventListener("click", go);
  $("#gamesMain").addEventListener("click", (e) => {
    go(e);
    const f = e.target.closest("[data-fix]");
    if (f && GAMES_OPS) runFix(f.dataset.fix, GAMES_OPS.actions[f.dataset.fix] || {});
    const b = e.target.closest("button[data-act]");
    if (b) {
      const g = GAMES.find((x) => x.key === (GAME_TOOL || "").split("/")[0]);
      const a = g && g.actions.find((x) => x.id === b.dataset.act);
      if (!a) return;
      if ((a.fields || []).length) openActionModal(g, a, b);
      else runBareAction(g, a, b);
    }
  });
  // leaving the page (the browser's back button) takes an open action dialog with it
  window.addEventListener("hashchange", closeActionModal);
}

// ---- Overview: a tile per game ----
function renderOverviewGames() {
  const panel = $("#ovGamesPanel");
  panel.hidden = !GAMES.length;
  const total = GAMES.reduce((n, g) => n + (g.live && typeof g.live.online === "number" ? g.live.online : 0), 0);
  $("#ovGamesSum").textContent = `${total} player${total === 1 ? "" : "s"} online`;
  $("#ovGames").innerHTML = GAMES.filter((g) => g.kind !== "platform").map((g) => {
    const st = gameState(g), live = g.live || {};
    const rooms = Array.isArray(live.rooms) && live.rooms.length
      ? `${live.rooms.length} ${live.rooms_label || "rooms"}` : "";
    const line = st.cls === "good" ? (live.summary || rooms || (g.live ? "Quiet" : "")) : st.word + (st.why ? ": " + st.why : "");
    return `<a class="ov-tile" href="#games/${esc(g.key)}"><span class="nm"><span class="dot ${st.cls}" aria-hidden="true"></span>`
      + `<span>${esc(g.title)}</span></span>`
      + `<span class="n">${num(live.online)}<small>online</small></span>`
      + `<span class="d" title="${esc(line)}">${esc(line || "\u00a0")}</span></a>`;
  }).join("");
}

// ---- Settings > Modules (owner): every module the panel found ----
// where a module came from, in words; the setting behind it is in the cell's tooltip
const ORIGIN = { "built-in": ["Built in", ""], file: ["Modules folder", ""],
                 game: ["Game", ""], legacy: ["Old setting", ""] };

const PORT_FROM = { env: "Server settings", stored: "Added here" };
let ADDED = [];

async function loadModules() {
  if (!DEPLOY) await probeDeploy();
  let r;
  try { r = await api("/api/modules?fresh=1"); } catch (e) { toast(e.message, true); return; }
  const live = Object.fromEntries((r.modules || []).map((m) => [m.key, m.live]));
  // a cell with nothing to say is marked, so a phone's card leaves it out
  const nil = (html) => `<td class="muted nil">${html}</td>`;
  $("#modulesBody").innerHTML = (r.found || []).map((m) => {
    const st = live[m.key];
    const liveText = !m.live ? ""
      : !m.shown ? "Hidden"
      : !st ? "Waiting"
      : st.error ? "Trouble"
      : "OK" + (typeof st.online === "number" ? ", " + st.online + " online" : "");
    const tools = m.tools ? `${m.tools} page${m.tools === 1 ? "" : "s"}${m.has_tools ? "" : ", not connected"}` : "";
    const acts = m.actions ? `${m.actions} action${m.actions === 1 ? "" : "s"}` : "";
    const [from, fromTip] = ORIGIN[m.origin] || [m.origin, ""];
    return `<tr><td class="strong">${m.shown ? `<a href="#games/${esc(m.key)}">${esc(m.title)}</a>` : esc(m.title)}</td>`
      + `<td class="mono">${esc(m.key)}</td><td${fromTip ? ` title="${esc(fromTip)}"` : ""}>${esc(from)}</td>`
      + (PORT_FROM[m.port] ? `<td>${esc(PORT_FROM[m.port])}</td>` : nil("-"))
      + (m.shown ? `<td class="nil">Yes</td>` : "<td>No</td>")
      + ([tools, acts].some(Boolean) ? `<td>${esc([tools, acts].filter(Boolean).join(", "))}</td>` : nil("None"))
      + (liveText ? `<td class="${st && st.error ? "bad" : "muted"}"${st && st.error ? ` title="${esc(st.error)}"` : ""}>${esc(liveText)}</td>` : nil("None"))
      + `</tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">No modules found.</td></tr>`;
  labelTables($("#tab-modules"));
  const probs = r.problems || [];
  $("#modulesProblems").hidden = !probs.length;
  $("#modulesProblemList").innerHTML = probs.map((p) => `<li>${esc(p)}</li>`).join("");
  ADDED = r.added || [];
  renderAdded();
  loadDevCodes();
}

// ---- Settings > Modules: games added here, by hand or by their setup program (adminpair.py) ----
// a paired game's updater (docs/installs.md, "Updates"): what its setup program asks the
// deployer to run, and the owner's Approve or Revoke
function updaterRow(g) {
  const u = g.updater;
  if (!u) return "";
  const ok = u.state === "approved";
  const when = ok ? (u.approved_at ? `Approved ${ago(u.approved_at * 1000)}` : "")
    : (u.requested_at ? `Requested ${ago(u.requested_at * 1000)}` : "");
  const noDeployer = !(DEPLOY && DEPLOY.available)
    ? `<span class="muted">Can't run until game installs are set up.</span>` : "";
  const chans = u.channels || [];
  return `<tr class="ga-upd"><td colspan="5" data-label=""><div class="ga-upd-box${ok ? " ok" : ""}">`
    + `<div class="ga-upd-head">Updater <span class="pill ${ok ? "live" : "wait"}">${ok ? "Approved" : "Needs approval"}</span>`
    + `<span class="muted">${esc(when)}</span></div>`
    + `<dl class="ga-upd-grid"><dt>Folder</dt><dd><code class="mono">${esc(u.folder)}</code></dd>`
    + `<dt>Command</dt><dd><code class="mono">${esc(u.shown)}</code></dd>`
    + `<dt>Timeout</dt><dd>${esc(u.timeout_minutes)} minutes</dd>`
    + (chans.length ? `<dt>Channels</dt><dd>${esc(chans.map(chanWord).join(", "))}</dd>` : "") + `</dl>`
    + `<div class="ga-upd-acts">${ok
      ? `<button class="ghost sm" data-ga-revoke="${esc(g.key)}">Revoke</button>`
      : `<button class="act sm" data-ga-approve="${esc(g.key)}">Approve</button>`}`
    + `${noDeployer}</div>`
    + (ok && chans.length > 1 && DEPLOY && DEPLOY.available ? gaChanBox(g, u) : "")
    + `</div></td></tr>`;
}

// ---- a paired game's update channel (docs/installs.md, "Update channels"), owner only ----
// Which channel it follows, here beside its updater. It is switched in one place, the Update
// channel list under Install games, where the one access code box is too.
const chanWord = (c) => c.charAt(0).toUpperCase() + c.slice(1);

function gaChanBox(g, u) {
  const on = u.channel || "stable";
  const when = u.channel_at ? `Changed ${ago(u.channel_at * 1000)}` : "";
  return `<div class="ga-chan"><span class="ga-chan-lab">Update channel</span>`
    + `<span class="pill ${on === "dev" ? "dev" : "env"}">${esc(chanWord(on))}</span>`
    + (when ? `<span class="muted">${esc(when)}</span>` : "")
    + `<button type="button" class="ghost sm" data-ga-chantab="${esc(g.key)}">Change</button></div>`;
}

// ---- Settings > Modules, "Dev access codes": this panel as the issuer (admindevcodes.py) ----
let DEVCODES = null;

async function loadDevCodes() {
  const box = $("#devCodesPanel");
  if (!box || !isOwner()) return;
  try { DEVCODES = await api("/api/devcodes"); } catch (e) { box.hidden = true; return; }
  renderDevCodes();
}

const DC_WORD = { unused: ["live", "Unused"], used: ["env", "In use"], revoked: ["off", "Revoked"] };

function renderDevCodes() {
  const d = DEVCODES || {}, box = $("#devCodesPanel");
  const codes = d.codes || [], kinds = d.kinds || [];
  box.hidden = !d.issuing && !codes.length && !(d.problems || []).length;
  if (box.hidden) return;
  // One sentence per kind (what its codes redeem to), then any setting that could not be read.
  const named = kinds.length > 1 || kinds.some((k) => k.key);
  $("#dcWhy").innerHTML = kinds.filter((k) => k.key && !k.issuing && k.why).map((k) => `<p class="dc-problem">${esc(k.why)}</p>`).join("")
    + (d.problems || []).map((p) => `<p class="dc-problem">${esc(p)}</p>`).join("");
  const sel = $("#dcKind"), was = sel.value;
  const ready = kinds.filter((k) => k.issuing);
  sel.innerHTML = ready.map((k) => `<option value="${esc(k.key)}">${esc(k.label || k.key)}</option>`).join("");
  if (ready.some((k) => k.key === was)) sel.value = was;
  $("#dcKindBox").hidden = ready.length < 2;
  dcReady();
  const kindCol = named || codes.some((c) => !c.kind_ok);
  box.querySelectorAll(".dc-kindcol").forEach((el) => { el.hidden = !kindCol; });
  $("#dcBody").innerHTML = codes.map((c) => {
    const [cls, word] = DC_WORD[c.state] || DC_WORD.unused;
    const used = c.redeemed_at ? `${ago(c.redeemed_at * 1000)}<div class="muted mono">${esc(c.install || "")}</div>` : '<span class="muted">-</span>';
    const act = c.state !== "revoked" || c.token_left
      ? `<button class="${c.token_left ? "ghost" : "danger"} sm" data-dc-revoke="${esc(c.code)}">${c.token_left ? "Delete token" : "Revoke"}</button>` : "";
    const kind = c.kind_ok ? esc(c.kind_label || c.kind || "")
      : `${esc(c.kind_label || c.kind || "Unnamed")}<div class="muted">Not set up</div>`;
    return `<tr><td class="strong" data-label="Code"><code class="mono dc-code">${esc(c.code)}</code></td>`
      + (kindCol ? `<td data-label="Kind">${kind}</td>` : "")
      + `<td data-label="For">${esc(c.note || "")}<div class="muted">${esc([c.made_by, c.created_at ? ago(c.created_at * 1000) : ""].filter(Boolean).join(", "))}</div></td>`
      + `<td data-label="State"><span class="pill ${cls}">${esc(word)}</span></td>`
      + `<td data-label="Redeemed">${used}</td>`
      + `<td data-label="">${act}</td></tr>`;
  }).join("") || `<tr><td colspan="${kindCol ? 6 : 5}" class="muted">No codes yet.</td></tr>`;
}

async function dcMake() {
  const count = parseInt($("#dcCount").value, 10), note = $("#dcNote").value.trim();
  const res = $("#dcResult");
  res.hidden = true;
  const body = { action: "make", count, note };
  if ($("#dcKind").value) body.kind = $("#dcKind").value;      // '' is the unnamed kind
  try {
    const r = await api("/api/devcodes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    $("#dcNote").value = "";
    dcReady();
    $("#dcNew").hidden = false;
    $("#dcNewList").textContent = r.codes.join("\n");
    $("#dcNewHead").textContent = `${r.codes.length} new ${r.kind_label ? r.kind_label + " " : ""}code${r.codes.length === 1 ? "" : "s"} for ${r.note}`;
    loadDevCodes();
  } catch (e) { res.hidden = false; res.textContent = e.message; }
}

async function dcRevoke(code) {
  const c = ((DEVCODES || {}).codes || []).find((x) => x.code === code) || { code };
  const sure = c.token_left ? true : confirm(`Revoke ${code}?` + (c.state === "used" ? "\n\nThe server using it loses dev builds." : ""));
  if (!sure) return;
  try {
    const r = await api("/api/devcodes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "revoke", code }) });
    if (r.warning) toast(r.warning, true);
    else toast("Revoked.");
  } catch (e) { toast(e.message, true); }
  loadDevCodes();
}

// "For" says who the codes are for; Make codes waits for it
const dcReady = () => { $("#dcMake").disabled = !(DEVCODES || {}).issuing || !$("#dcNote").value.trim(); };
document.addEventListener("input", (e) => { if (e.target.id === "dcNote") dcReady(); });

document.addEventListener("click", (e) => {
  const t = e.target;
  const ct = t.closest("[data-ga-chantab]");
  if (ct) {
    location.hash = "#deploy";
    // the Update channel list is folded away lower down: open it and bring it into view
    setTimeout(() => { const p = $("#chanPanel"); if (p && !p.hidden) { $("#chanFold").open = true; p.scrollIntoView({ block: "start" }); } }, 400);
    return;
  }
  if (t.closest("#dcMake")) return dcMake();
  if (t.closest("#dcCopy")) return copyText($("#dcNewList").textContent, "the codes");
  if (t.closest("#dcNewClose")) { $("#dcNew").hidden = true; return; }
  const rv = t.closest("[data-dc-revoke]");
  if (rv) return dcRevoke(rv.dataset.dcRevoke);
});

async function gaUpdater(key, approve) {
  const g = ADDED.find((x) => x.key === key);
  const u = g && g.updater;
  if (!u) return;
  const sure = approve
    ? confirm(`Approve the ${g.title} updater?\n\nIt gets full control of the server.`)
    : confirm(`Revoke the ${g.title} updater?`);
  if (!sure) return;
  try {
    await api("/api/modules/game", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(approve ? { action: "approve_updater", key, id: u.id } : { action: "revoke_updater", key }) });
    toast(approve ? "Approved." : "Revoked.");
  } catch (e) { toast(e.message, true); }
  loadModules();
}

function renderAdded() {
  $("#addedBody").innerHTML = ADDED.map((g) => {
    const how = g.how === "paired" && g.paired ? "Paired" : "Added";
    const when = g.added_at ? ago(g.added_at * 1000) : "";
    return `<tr${g.updater ? ' class="has-upd"' : ""}><td class="strong">${esc(g.title)}<div class="muted mono">${esc(g.key)}</div></td>`
      + `<td><span class="mono">${esc(g.url)}</span>`
      + `${g.env ? `<div class="patch-why">Server settings override this address.</div>` : ""}</td>`
      + `<td>${esc(how)}<div class="muted">${esc([g.added_by, when].filter(Boolean).join(", "))}</div></td>`
      + `<td>${g.token_set ? "Set" : '<span class="muted">None</span>'}</td>`
      + `<td><div class="deploy-acts"><button class="ghost sm" data-ga-edit="${esc(g.key)}">Change</button>`
      + `${g.paired ? `<button class="ghost sm" data-ga-unpair="${esc(g.key)}">Unpair</button>` : ""}`
      + `<button class="danger sm" data-ga-remove="${esc(g.key)}">Remove</button></div></td></tr>`
      + updaterRow(g);
  }).join("") || `<tr><td colspan="5" class="muted">None yet.</td></tr>`;
}

let GA_EDIT = null;   // the key being changed, or null for a new game

function gaOpen(key) {
  const g = key ? ADDED.find((x) => x.key === key) : null;
  GA_EDIT = g ? g.key : null;
  $("#pairBox").hidden = true;
  stopPairPoll();
  $("#gaForm").hidden = false;
  $("#gaTitle").textContent = g ? `Change ${g.title}` : "Add a game";
  $("#gaKey").value = g ? g.key : "";
  $("#gaKey").readOnly = !!g;
  $("#gaUrl").value = g ? g.url : "";
  $("#gaToken").value = "";
  $("#gaToken").placeholder = g && g.token_set ? "Leave empty to keep" : "";
  $("#gaResult").hidden = true;
  (g ? $("#gaUrl") : $("#gaKey")).focus();
}

function gaBody() {
  return { key: $("#gaKey").value.trim(), url: $("#gaUrl").value.trim(), token: $("#gaToken").value };
}

function gaShow(ok, html) {
  const box = $("#gaResult");
  box.hidden = false;
  box.classList.toggle("bad", !ok);
  box.innerHTML = html;
}

async function gaTest() {
  const b = gaBody();
  $("#gaTest").disabled = true;
  try {
    const r = await api("/api/modules/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
    const g = r.game || {};
    const list = (xs) => xs.length ? xs.map((x) => esc(x.label)).join(", ") : "none";
    gaShow(true, `${esc(g.title)} answered.`
      + `<span class="muted">Pages: ${list(g.tools || [])}</span>`
      + `<span class="muted">Actions: ${list(g.actions || [])}</span>`);
  } catch (e) {
    gaShow(false, esc(e.message));
  } finally {
    $("#gaTest").disabled = false;
  }
}

async function gaSave() {
  const b = gaBody();
  $("#gaSave").disabled = true;
  try {
    const r = await api("/api/modules/game", { method: "POST", headers: { "Content-Type": "application/json" },
                                               body: JSON.stringify(Object.assign({ action: "save" }, b)) });
    toast(r.added ? "Added." : "Saved.");
    $("#gaForm").hidden = true;
    loadModules();
  } catch (e) {
    gaShow(false, esc(e.message));
  } finally {
    $("#gaSave").disabled = false;
  }
}

async function gaAct(action, key) {
  const g = ADDED.find((x) => x.key === key) || { title: key };
  const sure = action === "remove"
    ? confirm(`Remove ${g.title} from the panel?\n\nThe game keeps running.`)
    : confirm(`Unpair ${g.title}?`);
  if (!sure) return;
  try {
    await api("/api/modules/game", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ action, key }) });
    toast(action === "remove" ? "Removed." : "Unpaired.");
    if (GA_EDIT === key) $("#gaForm").hidden = true;
    loadModules();
  } catch (e) { toast(e.message, true); }
}

// ---- Pair with a setup program: a one-time code, 10 minutes ----
let PAIR = null, PAIR_TIMER = null;

function stopPairPoll() { clearInterval(PAIR_TIMER); PAIR_TIMER = null; }

async function pairNew() {
  $("#gaForm").hidden = true;
  stopPairPoll();
  let r;
  try { r = await api("/api/pair/code", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { toast(e.message, true); return; }
  PAIR = { code: r.code, until: Date.now() + (r.ttl || 600) * 1000, polled: 0 };
  $("#pairBox").hidden = false;
  $("#pairCode").innerHTML = `<span>${esc(r.code.slice(0, 4))}</span>${esc(r.code.slice(4))}`;
  $("#pairCode").title = r.code;
  pairTick();
  PAIR_TIMER = setInterval(pairTick, 1000);
}

function pairSay(text, good) {
  $("#pairState").textContent = text;
  $("#pairState").classList.toggle("good", !!good);
}

async function pairTick() {
  if (!PAIR) return;
  if (CUR_TAB !== "modules") { stopPairPoll(); return; }
  const left = Math.max(0, Math.round((PAIR.until - Date.now()) / 1000));
  if (!left) { pairSay("Code expired. Make a new one."); $("#pairCode").classList.add("muted"); stopPairPoll(); return; }
  $("#pairCode").classList.remove("muted");
  pairSay(`Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`);
  if (++PAIR.polled % 3) return;          // ask the panel every 3 s whether it was used
  let s;
  try { s = await api("/api/pair/code?code=" + encodeURIComponent(PAIR.code)); } catch (e) { return; }
  if (s.state === "used") {
    stopPairPoll();
    pairSay(`Paired with ${s.title}.`, true);
    $("#pairCode").classList.add("muted");
    loadModules();
  } else if (s.state === "expired") {
    stopPairPoll();
    pairSay("Code expired. Make a new one.");
  }
}

document.addEventListener("click", (e) => {
  const t = e.target;
  if (t.closest("#gaOpen")) return gaOpen(null);
  if (t.closest("#gaCancel")) { $("#gaForm").hidden = true; return; }
  if (t.closest("#gaTest")) return gaTest();
  if (t.closest("#gaSave")) return gaSave();
  if (t.closest("#pairOpen") || t.closest("#pairNew")) return pairNew();
  if (t.closest("#pairClose")) { stopPairPoll(); PAIR = null; $("#pairBox").hidden = true; return; }
  const ed = t.closest("[data-ga-edit]");
  if (ed) return gaOpen(ed.dataset.gaEdit);
  const rm = t.closest("[data-ga-remove]");
  if (rm) return gaAct("remove", rm.dataset.gaRemove);
  const up = t.closest("[data-ga-unpair]");
  if (up) return gaAct("unpair", up.dataset.gaUnpair);
  const ap = t.closest("[data-ga-approve]");
  if (ap) return gaUpdater(ap.dataset.gaApprove, true);
  const rv = t.closest("[data-ga-revoke]");
  if (rv) return gaUpdater(rv.dataset.gaRevoke, false);
});

// ---- Updates: the core and each game (adminupdates.py), owner only ----
// In Install games when a deployer answers (with its Update buttons), else in Settings > Modules.
let UPD = null, UPD_TIMER = null;
// the last updater job each paired game ran from this page: its state and the game's own last line
const UPD_RUN = {};
const UPD_WORD = { available: ["live", "Update available"], current: ["env", "Up to date"], unknown: ["off", "Unknown"] };

const short12 = (c) => (c ? String(c).slice(0, 12) : "");
function updDate(iso) {
  const ms = Date.parse(iso || "");
  if (isNaN(ms)) return "";
  return new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// the deployer can update the core and the games it installed
function updByDeployer(key) {
  // a deployer for paired games only (POL_DEPLOY_MODE=updaters) installs and updates nothing itself
  if (!(DEPLOY && DEPLOY.available) || DEPLOY.mode === "updaters") return false;
  return key === "core" || (DEPLOY.games || []).some((g) => g.key === key && g.installed);
}

function updItem(it) {
  const [cls, word] = UPD_WORD[it.update] || UPD_WORD.unknown;
  const run = it.running || {}, lat = it.latest || {};
  const behind = typeof it.behind === "number" && it.update === "available"
    ? `${it.behind} change${it.behind === 1 ? "" : "s"} behind` : "";
  const vers = [
    run.commit && run.built_at ? [run.commit, `Installed ${esc(updDate(run.built_at))}`] : null,
    lat.commit && lat.date ? [lat.commit, `Latest ${esc(updDate(lat.date))}`] : null]
    .filter(Boolean).map(([c, s]) => `<span title="${esc(short12(c))}">${s}</span>`).join("");
  let tail = "";
  const running = it.state === "updating" || it.applying || APPLY[it.key];
  if (running) {
    // the game is applying its own update: its message, live (applyFollow)
    tail = `<div class="upd-live" data-upd-live="${esc(it.key)}"><span class="spin" aria-hidden="true"></span>`
      + `<span>${esc(applyLine(it.key, it.name, it.message))}</span></div>`;
  } else if (it.ops && !updByDeployer(it.key) && it.via !== "game") {
    // the server's ops runner updates it: its Update button and when it updates (opsBox)
    tail = (it.update === "unknown" && it.why && it.state !== "failed" ? `<div class="upd-why">${esc(it.why)}</div>` : "")
      + opsBox(it);
  } else if (it.update === "available") {
    const btn = (attr, how) => `<div class="upd-act"><button class="act sm" ${attr}="${esc(it.key)}">Update</button>`
      + `${how ? `<span class="muted">${how}</span>` : ""}</div>`;
    tail = updByDeployer(it.key) ? btn("data-upd-go", "")
      : it.via === "game" ? btn("data-upd-apply", "")
      : it.via === "updater" && DEPLOY && DEPLOY.available ? btn("data-upd-run", "")
      : (it.how ? `<div class="upd-how">${esc(it.how)}</div>` : "");
  } else if (it.update === "unknown" && it.why && it.state !== "failed") {
    tail = `<div class="upd-why">${esc(it.why)}</div>`;
  }
  if (it.state === "failed" && !running) tail = `<div class="upd-why bad">Update failed: ${esc(it.why || "")}</div>` + tail;
  const lastRun = UPD_RUN[it.key];
  if (lastRun && lastRun.state === "failed" && !running) {
    // the deployer ran the game's updater and it failed: the game's last line, and its whole log
    const line = String(lastRun.summary || "").replace(/^Update failed:\s*/i, "");
    tail = `<div class="upd-why bad">Update failed: ${esc(line || "the updater stopped.")}`
      + ` <button class="ghost sm" data-upd-log="${esc(lastRun.job)}">Show log</button></div>` + tail;
  }
  return `<div class="upd-item" data-upd="${esc(it.key)}"><div class="upd-head"><span class="nm">${esc(it.name || it.key)}</span>`
    + `<span class="pill ${cls}">${esc(word)}</span>${it.channel === "dev" ? '<span class="pill dev">Dev build</span>' : ""}`
    + `${behind ? `<span class="muted">${esc(behind)}</span>` : ""}</div>`
    + `${vers ? `<div class="upd-vers">${vers}</div>` : ""}${tail}${updNews(it)}</div>`;
}

// What's new, Show all and the logs stay as the reader left them across the refreshes
const UPD_OPEN = new Set();    // "news:<key>", "log:<key>" opened; "shut:<key>" a short list closed
const UPD_ALL = new Set();     // keys whose whole list was asked for
function updNews(it) {
  const ch = it.changes || [];
  if (!ch.length) return "";
  const li = (c) => `<li><span class="s">${esc(c.subject)}</span><span class="d">${esc(updDate(c.date))}</span></li>`;
  const open = UPD_OPEN.has("news:" + it.key) || (ch.length <= 3 && !UPD_OPEN.has("shut:" + it.key));
  const all = UPD_ALL.has(it.key);
  return `<details data-updopen="news:${esc(it.key)}"${open ? " open" : ""}><summary>What's new (${ch.length})</summary>`
    + `<ul class="upd-changes">${(all ? ch : ch.slice(0, 10)).map(li).join("")}</ul>`
    + (ch.length > 10 && !all ? `<button class="ghost sm" data-upd-all="${esc(it.key)}" style="margin-top:6px">Show all ${ch.length}</button>` : "")
    + `</details>`;
}
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (!(d instanceof HTMLElement) || !d.dataset || !d.dataset.updopen) return;
  const k = d.dataset.updopen, key = k.slice(k.indexOf(":") + 1);
  if (d.open) { UPD_OPEN.add(k); if (k.startsWith("news:")) UPD_OPEN.delete("shut:" + key); }
  else { UPD_OPEN.delete(k); if (k.startsWith("news:")) UPD_OPEN.add("shut:" + key); }
}, true);

// ---- a game the server's ops runner updates (adminupdates.py, ops_merge) ----
// Its Update button runs the game's own deploy job on the server now; the setting beside it
// turns that job's timer on (it deploys by itself within minutes of a push) or off. The
// runner picks a request up within a few seconds, so the list is asked again every 5 s
// while something it was asked to do has not shown up yet.
const OPS_ASKED = {};   // key -> { what, at, auto } asked from this page, until the runner shows it
const OPS_WAS = {};     // key -> true while its update ran at the last look

function inMin(sec) {
  const m = Math.round((sec * 1000 - Date.now()) / 60000);
  return m <= 0 ? "in under a minute" : `in ${m} min`;
}

function opsSettled(it) {
  // has the runner shown what this page asked of it?
  const a = OPS_ASKED[it.key], o = it.ops;
  if (!a) return true;
  if (Date.now() - a.at > 60000) return true;
  if (a.what === "update") return !!o.running || !!(o.last && o.last.at * 1000 >= a.at - 5000);
  return o.auto === a.auto;
}

function opsBox(it) {
  const o = it.ops, name = it.name || it.key;
  const busy = o.running || (OPS_ASKED[it.key] && OPS_ASKED[it.key].what === "update");
  let html = "";
  if (busy) {
    html += `<div class="upd-live"><span class="spin" aria-hidden="true"></span>`
      + `<span>${esc(`${name} is updating...`)}</span></div>`;
  } else if (it.update !== "current" || o.auto) {
    const label = it.update === "available" ? "Update" : "Update now";
    html += `<div class="upd-act"><button class="act sm" data-upd-ops="${esc(it.key)}">${label}</button></div>`;
  }
  if (o.on && o.off) {
    const checking = o.service && o.service.state === "activating";
    const state = o.auto ? (checking ? "Checking now" : o.next ? `Next check ${inMin(o.next)}` : "") : "";
    const asked = OPS_ASKED[it.key] && OPS_ASKED[it.key].what !== "update";
    html += `<div class="upd-auto"><span>Updates</span>`
      + `<div class="seg" role="group" aria-label="When ${esc(name)} updates">`
      + `<button data-upd-auto="${esc(it.key)}" data-on="1" class="${o.auto ? "on" : ""}" aria-pressed="${o.auto}"${asked ? " disabled" : ""}>Automatic</button>`
      + `<button data-upd-auto="${esc(it.key)}" data-on="0" class="${o.auto ? "" : "on"}" aria-pressed="${!o.auto}"${asked ? " disabled" : ""}>Manual</button></div>`
      + `<span class="muted">${esc(asked ? "Changing..." : state)}</span></div>`;
  }
  if (o.last && !busy) {
    const l = o.last;
    html += `<div class="upd-why${l.ok ? "" : " bad"}">${esc(`Last update ${l.ok ? "finished" : "failed"}`
      + `${l.at ? " " + ago(l.at * 1000) : ""}.${!l.ok && l.result ? " " + l.result : ""}`)}</div>`
      + (l.log && l.log.length ? `<details class="upd-log" data-updopen="log:${esc(it.key)}"${UPD_OPEN.has("log:" + it.key) ? " open" : ""}>`
        + `<summary>Show log</summary><pre>${esc(l.log.join("\n"))}</pre></details>` : "");
  }
  return html;
}

// after each look: say how a finished update ended, and keep looking while one runs
function opsFollow(items) {
  let again = false;
  for (const it of items) {
    if (!it.ops) continue;
    const o = it.ops;
    if (OPS_WAS[it.key] && !o.running && o.last) {
      toast(o.last.ok ? `${it.name} updated.` : `Couldn't update ${it.name}.${o.last.result ? " " + o.last.result : ""}`, !o.last.ok);
    }
    OPS_WAS[it.key] = !!o.running;
    if (opsSettled(it)) delete OPS_ASKED[it.key];
    if (o.running || OPS_ASKED[it.key]) again = true;
  }
  return again;
}

async function opsAsk(key, what) {
  const it = ((UPD || {}).items || []).find((x) => x.key === key) || { name: key, ops: {} };
  const name = it.name || key;
  if (what === "update" && !confirm(`Update ${name} now?\n\nEveryone playing it is disconnected.`)) return;
  if (what === "auto-on" && !confirm(`Update ${name} automatically?\n\nNew versions restart the game.`)) return;
  try {
    const r = await api("/api/updates/ops", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key, what }) });
    toast(r.message || (what === "update" ? "Update started." : "Saved."));
  } catch (e) { toast(e.message, true); return; }
  OPS_ASKED[key] = { what, at: Date.now(), auto: what === "auto-on" };
  if (UPD) renderUpdates();
  clearTimeout(UPD_TIMER);
  UPD_TIMER = setTimeout(() => loadUpdates(false), 3000);
}

// ---- a game that applies its own update (path B): the Update button, then its status every 5 s ----
const APPLY = {};   // key -> { name, message, lost, timer } while the panel follows an update

function applyLine(key, name, message) {
  const st = APPLY[key];
  if (st && st.lost) return st.message;
  const m = (st && st.message) || message;
  return m ? `Updating: ${m}` : `${name || key} is updating...`;
}

function paintApply(key) {
  const st = APPLY[key];
  document.querySelectorAll(`[data-upd-live="${CSS.escape(key)}"]`).forEach((el) => {
    el.lastElementChild.textContent = applyLine(key, st && st.name, "");
    el.classList.toggle("bad", !!(st && st.lost));
  });
}

function applyEnded(key) {
  delete APPLY[key];
  if (isOwner()) loadUpdates(false);
  if (CUR_TAB === "games") fetchGames(true).then(() => { const [k, tool] = (GAME_TOOL || "").split("/"); if (k && !tool) renderGame(k); });
}

function applyFollow(key, name) {
  if (APPLY[key] && APPLY[key].timer) return;
  const st = APPLY[key] = Object.assign(APPLY[key] || {}, { name: name || key });
  const tick = async () => {
    st.timer = null;
    if (APPLY[key] !== st) return;
    let r = null;
    try { r = await api("/api/updates/state?key=" + encodeURIComponent(key)); } catch (e) { /* the panel restarting */ }
    if (r) {
      if (r.state === "updating" && r.message) st.message = r.message;
      if (r.done) { toast(`${st.name} updated.`); return applyEnded(key); }
      if (r.state === "failed") { toast(`Couldn't update ${st.name}.${r.why ? " " + r.why : ""}`, true); return applyEnded(key); }
      if (r.lost) { st.lost = true; st.message = r.message; paintApply(key); return; }
      if (r.reachable && r.state !== "updating" && !r.applying) return applyEnded(key);
    }
    paintApply(key);
    st.timer = setTimeout(tick, 5000);
  };
  st.timer = setTimeout(tick, 5000);
}

async function applyStart(key) {
  const it = ((UPD || {}).items || []).find((x) => x.key === key);
  const g = GAMES.find((x) => x.key === key);
  const name = (it && it.name) || (g && g.title) || key;
  if (!confirm(`Update ${name} now?\n\nEveryone playing it is disconnected.`)) return;
  let r;
  try {
    r = await api("/api/updates/apply", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ key }) });
  } catch (e) { toast(e.message, true); return; }
  toast(r.msg || "Update started.");
  APPLY[key] = { name, message: "" };
  applyFollow(key, name);
  if (UPD && CUR_TAB === "deploy") renderUpdates();
  if (CUR_TAB === "games") renderGame(key);
}

async function updaterStart(key) {
  const it = ((UPD || {}).items || []).find((x) => x.key === key) || { name: key };
  const name = it.name || key;
  if (!confirm(`Update ${name}?\n\nEveryone playing it may be disconnected.`)) return;
  try {
    const r = await api("/api/updates/run", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key }) });
    toast("Update started.");
    UPD_RUN[key] = { job: r.job, state: "running" };
    await probeDeploy(); renderDeploy();
    followJob(r.job);
  } catch (e) { toast(e.message, true); }
}

// the game's page: its own Update box, for the owner and a moderator with game.<key>.update
function gameUpdateBox(g) {
  const u = (g.live || {}).update;
  if (!u || u.apply !== true || !can(`game.${g.key}.update`)) return "";
  const running = u.state === "updating" || APPLY[g.key];
  if (running && !APPLY[g.key]) applyFollow(g.key, g.title);
  let body;
  if (running) {
    body = `<div class="upd-live${APPLY[g.key] && APPLY[g.key].lost ? " bad" : ""}" data-upd-live="${esc(g.key)}">`
      + `<span class="spin" aria-hidden="true"></span><span>${esc(applyLine(g.key, g.title, u.message))}</span></div>`;
  } else if (u.update === "available") {
    const n = typeof u.behind === "number" ? `${u.behind} change${u.behind === 1 ? "" : "s"} behind` : "";
    body = `<div class="upd-act"><button class="act sm" data-upd-apply="${esc(g.key)}">Update</button>`
      + `${n ? `<span class="muted">${esc(n)}</span>` : ""}</div>`;
  } else if (u.state === "failed") {
    body = "";
  } else {
    // up to date (a pill beside the name says so) or it cannot tell: nothing to do here
    return "";
  }
  if (u.state === "failed" && !running) body = `<div class="upd-why bad">Update failed: ${esc(u.why || "")}</div>` + body;
  return `<section class="panel gx-wide gx-upd"><h3>Update</h3>${body}</section>`;
}

// The Updates list is always on this tab (Settings > Install games, called Updates without a
// deployer). With a deployer, what it updates (the core, the games it installed) is shown on
// that game's row under "Games this server can run", with its Update button; the list has the rest.
function updWhen() {
  const r = UPD || {};
  return r.checking ? "Checking..." : (r.checked_at ? "Checked " + ago(Date.parse(r.checked_at)) : "");
}
function renderUpdates() {
  const box = $("#updDeploy");
  const r = UPD || {};
  const all = r.items || [];
  const items = all.filter((it) => !updByDeployer(it.key));
  const folded = all.length - items.length;
  const check = `<button class="ghost sm" data-upd-check${r.checking ? " disabled" : ""}>Check for updates</button>`;
  // with every item on a deployer row, the check sits on that list instead
  box.hidden = !items.length && folded > 0;
  const dc = $("#deployCheck");
  if (dc) dc.innerHTML = folded ? `${check}<span class="muted">${esc(updWhen())}</span>` : "";
  box.innerHTML = `<h2>${folded ? "Other updates" : "Updates"} <span class="h2sub">${esc(updWhen())}</span>`
    + `${r.channel === "dev" ? ' <span class="pill dev">Dev</span>' : ""}</h2>`
    + (folded ? "" : `<div class="toolbar">${check}</div>`)
    + `<div class="upd-list">${items.map(updItem).join("") || '<p class="hint">Nothing to update.</p>'}</div>`;
  if (DEPLOY && DEPLOY.available) renderDeployRows();
  // an update a game applies itself, started here or before a reload: follow it
  for (const it of all) if ((it.state === "updating" || it.applying) && !APPLY[it.key]) applyFollow(it.key, it.name);
}

async function loadUpdates(refresh) {
  if (!isOwner()) return;
  clearTimeout(UPD_TIMER);
  try { UPD = await api("/api/updates" + (refresh ? "?refresh=1" : "")); }
  catch (e) { toast(e.message, true); return; }
  const opsBusy = opsFollow(UPD.items || []);
  if (CUR_TAB === "deploy") renderUpdates();
  // while a check runs, ask again every 3 s; while the ops runner works on a game, every 5 s
  if (UPD.checking && ["deploy", "overview"].includes(CUR_TAB))
    UPD_TIMER = setTimeout(() => loadUpdates(false), 3000);
  else if (opsBusy && CUR_TAB === "deploy")
    UPD_TIMER = setTimeout(() => loadUpdates(false), 5000);
  return UPD;
}

// Overview: one line when anything has an update, linking to where the list is
async function loadUpdateNotice() {
  if (!isOwner()) return;
  let r;
  try { r = await api("/api/updates"); } catch (e) { return; }
  const avail = (r.items || []).filter((it) => it.update === "available");
  const ul = $("#ovAttention");
  ul.querySelectorAll(".upd-note").forEach((li) => li.remove());
  if (!avail.length || CUR_TAB !== "overview") return;
  if (!DEPLOY) await probeDeploy();
  ul.querySelectorAll("li.empty").forEach((li) => li.remove());
  ul.insertAdjacentHTML("afterbegin", `<li class="upd-note" data-go="deploy"><span class="t1">Updates for `
    + `${esc(avail.map((it) => it.name || it.key).join(", "))}</span><span class="t2">Settings, ${DEPLOY && DEPLOY.available ? "Install games" : "Updates"}</span></li>`);
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (t.closest("[data-upd-check]")) { t.closest("[data-upd-check]").disabled = true; return loadUpdates(true); }
  const lg = t.closest("[data-upd-log]");
  if (lg) {
    await showJob(lg.dataset.updLog);
    const panel = $("#deployJobPanel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const all = t.closest("[data-upd-all]");
  if (all) {
    const it = ((UPD || {}).items || []).find((x) => x.key === all.dataset.updAll);
    if (!it) return;
    UPD_ALL.add(it.key);
    UPD_OPEN.add("news:" + it.key);
    all.closest("details").outerHTML = updNews(it);
    return;
  }
  const ap = t.closest("[data-upd-apply]");
  if (ap) return applyStart(ap.dataset.updApply);
  const op = t.closest("[data-upd-ops]");
  if (op) return opsAsk(op.dataset.updOps, "update");
  const au = t.closest("[data-upd-auto]");
  if (au) {
    if (au.classList.contains("on") || au.disabled) return;
    return opsAsk(au.dataset.updAuto, au.dataset.on === "1" ? "auto-on" : "auto-off");
  }
  const ru = t.closest("[data-upd-run]");
  if (ru) return updaterStart(ru.dataset.updRun);
  const go = t.closest("[data-upd-go]");
  if (!go) return;
  const key = go.dataset.updGo;
  const name = deployName(key);
  if (!confirmDeploy("update", key)) return;
  try {
    const r = await api("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ action: "update", game: key }) });
    toast("Update started.");
    await probeDeploy(); renderDeploy();
    followJob(r.job);
  } catch (err) { toast(err.message, true); }
});

// ---- Settings > Install games (owner): the deployer (admindeploy.py) ----
// The deployer runs one job at a time and keeps its jobs, so a panel that was
// restarted finds a running job in the list and follows it again.
let DEPLOY = null, DEPLOY_JOB = null, DEPLOY_OFFSET = 0, DEPLOY_TIMER = null;
const JOB_DONE = new Set(["ok", "failed", "needs_data"]);
const JOB_WORD = { queued: "Waiting", running: "Running", ok: "Done", failed: "Failed",
                   needs_data: "Needs client data" };

async function probeDeploy() {
  if (!isOwner()) return;
  try { DEPLOY = await api("/api/deploy"); } catch (e) { DEPLOY = null; }
  // the tab is always there for the owner: it holds the Updates list either way
  const tab = $("#deployTab");
  tab.hidden = false;
  tab.textContent = DEPLOY && DEPLOY.available && DEPLOY.mode !== "updaters" ? "Install games" : "Updates";
}

function deployState(g) {
  if (!g.installed) return ["off", "Not installed", ["install"]];
  if (!g.enabled) return ["off", "Off", ["update", "enable", "check"]];
  if (g.running === false) return ["bad", "Not running", ["update", "restart", "disable", "check"]];
  return ["good", g.running ? "Running" : "On", ["update", "restart", "disable", "check"]];
}
const ACT_WORD = { install: "Install", enable: "Turn on", disable: "Turn off", update: "Update",
                   restart: "Restart", check: "Check", updater: "Update", fix: "Fix" };

// a game's name as the deployer lists it, else the panel's own title for it
function deployName(key) {
  if (key === "core") return "OpenLobby";
  const d = ((DEPLOY || {}).games || []).find((g) => g.key === key);
  const m = (GAMES || []).find((g) => g.key === key);
  const u = ((UPD || {}).items || []).find((x) => x.key === key);
  return (d && d.name) || (m && m.title) || (u && u.name) || key;
}

function confirmDeploy(act, key) {
  const name = deployName(key);
  if (act === "update") {
    return confirm(key === "core"
      ? `Update ${name}?\n\nThe panel restarts and is back in a minute or two.`
      : `Update ${name}?\n\nEveryone playing it is disconnected.`);
  }
  const what = { install: `Install ${name}?\n\nIt stays off until you turn it on.`,
                 enable: `Turn on ${name}?`,
                 disable: `Turn off ${name}?\n\nEveryone playing it is disconnected.`,
                 restart: `Restart ${name}?` }[act];
  return confirm(what || `${ACT_WORD[act] || act} ${name}?`);
}

function renderDeploy() {
  const d = DEPLOY || {};
  $("#deployOff").hidden = !!d.available;
  $("#deployPanel").hidden = !d.available || d.mode === "updaters";
  $("#deployJobsPanel").hidden = !d.available;
  if (!d.available) {
    // without a deployer this tab is the Updates list, and how to add the deployer
    const notSetUp = !d.why || d.why === "not set up";
    $("#deployWhy").innerHTML = notSetUp
      ? `To install games here, run OpenLobby Setup again and add game installs.`
      : `<span title="${esc(d.why)}">Couldn't reach the game installer. Try again in a minute.</span>`;
    return;
  }
  // the deployer found that Docker sees the checkouts elsewhere: it runs no compose command
  const problem = d.mode === "updaters" ? "" : (d.options || {}).problem || "";
  $("#deployProblem").hidden = !problem;
  $("#deployProblem").textContent = problem;
  renderDeployRows();
  $("#deployJobs").innerHTML = (d.jobs || []).map((j) =>
    `<li data-job="${esc(j.id)}"><span class="t1">${j.action === "channel" ? (j.key ? `Update channel: ${esc(deployName(j.key))} to ${esc(chanWord(j.to || "stable"))}` : "Update channel")
      : `${esc(ACT_WORD[j.action] || j.action)} ${esc(j.title || deployName(j.game))}`}: `
    + `${esc(JOB_WORD[j.state] || j.state)}${j.summary ? " - " + esc(j.summary) : ""}</span>`
    + `<span class="t2">${esc(jobWhen(j))}</span></li>`).join("")
    || '<li class="empty">No jobs yet.</li>';
  if (CHAN) renderChannel();
}

// What a game still needs, short: files that share instructions are one line (their folder
// once, then the names), each with its Import button; the instructions and the game's notes
// fold away under "How to get them" and "Notes".
function deployNeeds(g, busy) {
  const groups = [];
  for (const m of g.missing || []) {
    const item = m.import && m.item ? m.item : "";
    const k = [m.hint || "", m.required === false, item].join("\u0001");
    let gr = groups.find((x) => x.k === k);
    if (!gr) groups.push(gr = { k, hint: m.hint || "", optional: m.required === false, item, paths: [] });
    gr.paths.push(m.path);
  }
  const shown = new Set();
  const lines = groups.map((gr) => {
    const parts = gr.paths.map((p) => String(p).split("/"));
    let dir = parts.length > 1 ? parts[0].slice(0, -1) : [];
    for (const p of parts.slice(1)) {
      let i = 0;
      while (i < dir.length && i < p.length - 1 && dir[i] === p[i]) i++;
      dir = dir.slice(0, i);
    }
    const label = parts.length > 1 && dir.length
      ? `<span class="mono">${esc(dir[dir.length - 1])}/</span> ${esc(parts.map((p) => p.slice(dir.length).join("/")).join(", "))}`
      : `<span class="mono">${esc(parts[0].slice(-2).join("/"))}</span>`;
    const btn = gr.item && !shown.has(gr.item)
      ? (shown.add(gr.item), ` <button class="ghost sm" data-import="${esc(g.key)}" data-item="${esc(gr.item)}"`
        + `${busy ? " disabled" : ""}>Import...</button>`) : "";
    return `<div class="dn-need" title="${esc(gr.paths.join("\n"))}">${label}`
      + `${gr.optional ? ' <span class="muted">optional</span>' : ""}${btn}`
      + (gr.hint ? `<details class="dn-more"><summary>How to get ${gr.paths.length > 1 ? "them" : "it"}</summary>`
        + `<div class="muted">${esc(gr.hint)}</div></details>` : "") + `</div>`;
  }).join("");
  return lines
    + (g.installed && g.enabled && (g.admin_env || []).length
      ? `<div class="muted">Connect its admin page under Modules.</div>` : "")
    + (g.notes ? `<details class="dn-more"><summary>Notes</summary><div class="muted">${esc(g.notes)}</div></details>` : "");
}

// one row per thing the deployer runs, with its update state and its one Update button
function renderDeployRows() {
  const d = DEPLOY || {};
  if (!d.available) return;
  const busy = (d.jobs || []).some((j) => !JOB_DONE.has(j.state));
  const upd = Object.fromEntries(((UPD || {}).items || []).map((it) => [it.key, it]));
  const rows = [{ key: "core", name: "OpenLobby", installed: true, enabled: true,
                  running: (d.core || {}).running, missing: [], admin_env: [], core: true }]
    .concat(d.games || []);
  $("#deployBody").innerHTML = rows.map((g) => {
    const [cls, word, acts] = g.core ? ["good", "Running", ["update", "restart", "check"]] : deployState(g);
    const it = g.installed ? upd[g.key] : null;
    const avail = it && it.update === "available";
    const needs = deployNeeds(g, busy);
    // its update: the state beside its running state, What's new under its name
    const behind = avail && typeof it.behind === "number" ? `${it.behind} change${it.behind === 1 ? "" : "s"} behind` : "";
    const updPill = !it ? "" : avail ? ` <span class="pill live">Update available</span>`
      : it.update === "current" ? ` <span class="pill env">Up to date</span>` : "";
    const failed = it && it.state === "failed" ? `<div class="patch-why">Update failed: ${esc(it.why || "")}</div>` : "";
    // a known problem the deployer found in the game's logs, with the fix it can run (docs/deployer.md)
    const probs = (g.problems || []).map((p) => `<div class="deploy-problem"><div class="patch-why">${esc(p.say)}</div>`
      + `<button class="act sm" data-deploy-fix="${esc(p.id)}" data-game="${esc(g.key)}" data-label="${esc(p.fix_label)}"`
      + ` data-about="${esc(p.fix_about || "")}"${busy ? " disabled" : ""}>${esc(p.fix_label)}</button></div>`).join("");
    const buttons = acts.map((a) =>
      `<button class="${a === "disable" ? "danger" : a === "update" && avail ? "act" : "ghost"} sm" `
      + `data-deploy="${esc(a)}" data-game="${esc(g.key)}"${busy ? " disabled" : ""}>${esc(ACT_WORD[a])}</button>`).join("");
    return `<tr><td class="strong">${esc(g.name || g.key)}<div class="muted">${esc(behind)}</div>${it ? updNews(it) : ""}</td>`
      + `<td><span class="pill ${cls === "good" ? "live" : cls === "bad" ? "off" : "env"}">${esc(word)}</span>${updPill}${failed}${probs}</td>`
      + (needs ? `<td>${needs}</td>` : `<td class="nil"><span class="muted">-</span></td>`)
      + `<td><div class="deploy-acts">${buttons}</div></td></tr>`;
  }).join("");
  labelTables($("#tab-deploy"));
}

// ---- Install games > Update channel (owner): an access code unlocks the dev builds, then each ----
// server (the core, each installed game with a dev build, each paired game whose updater offers dev)
// has its own Stable/Dev switch. The code goes from the form to the deployer (admindeploy.set_channel)
// and is cleared from the page at once; the deployer keeps it and no answer carries it back.
let CHAN = null;

async function loadChannel() {
  if (!isOwner()) return;
  try { CHAN = await api("/api/deploy/channel"); } catch (e) { CHAN = { available: false, why: e.message }; }
  renderChannel();
}

function chanForm(open, again) {
  $("#chanForm").hidden = !open;
  $("#chanResult").hidden = true;
  $("#chanCode").value = "";
  $("#chanFormHead").textContent = again ? "New access code" : "Unlock dev builds";
  if (open) $("#chanCode").focus();
}

// every row of the list: the deployer's keys, then the paired games
function chanRows(c) {
  const imgs = c.images || [];
  const rows = (c.keys || []).map((k) => ({
    kind: "key", key: k.key, name: k.key === "core" ? "OpenLobby" : k.name, sub: "",
    on: k.on, since: k.since, enabled: k.enabled, applies: k.applies, note: k.note, problem: k.problem,
    would: k.would || [], back: k.back, services: k.services || [],
    images: imgs.filter((r) => r.key === k.key && r.dev_name)
  }));
  for (const p of c.paired || []) {
    // the switch offers what the game's updater declares (stable, dev, and e.g. preview)
    const channels = p.channels && p.channels.length ? p.channels : ["stable", "dev"];
    const known = channels.includes(p.on);
    rows.push({ kind: "paired", key: p.key, name: p.title, sub: "Paired game", on: known ? p.on : "stable",
                other: known ? null : p.on, note: p.note, problem: null, channels,
                would: p.would || [], images: [], enabled: true, applies: true });
  }
  return rows;
}

function renderChannel() {
  const box = $("#chanPanel");
  const c = CHAN;
  if (!(DEPLOY && DEPLOY.available) || !c) { box.hidden = true; return; }
  box.hidden = false;
  const busy = (DEPLOY.jobs || []).some((j) => !JOB_DONE.has(j.state));
  const old = c.available !== false && !Array.isArray(c.keys);
  if (c.available === false || old) {
    $("#chanPill").className = "pill env";
    $("#chanPill").textContent = "Stable";
    if (!$("#chanFold").dataset.set) { $("#chanFold").open = false; $("#chanFold").dataset.set = "1"; }
    $("#chanHint").textContent = old ? "Update OpenLobby to switch games one at a time." : (c.why || "Channel unknown.");
    ["#chanBar", "#chanWarn", "#chanForm", "#chanGaps", "#chanSource", "#chanAdv"].forEach((s) => { $(s).hidden = true; });
    $("#chanBody").innerHTML = "";
    return;
  }
  const rows = chanRows(c);
  const onDev = rows.filter((r) => r.on === "dev");
  // folded away unless something here is in use; opened once, it stays as the reader left it
  const fold = $("#chanFold");
  if (!fold.dataset.set) { fold.open = !!c.unlocked || onDev.length > 0 || c.mode === "updaters"; fold.dataset.set = "1"; }
  $("#chanPill").className = "pill " + (onDev.length ? "dev" : "env");
  $("#chanPill").textContent = onDev.length ? `Dev: ${onDev.length}` : (c.unlocked ? "Unlocked" : "Stable");
  $("#chanBar").hidden = false;
  $("#chanUnlock").hidden = !!c.unlocked;
  $("#chanNewCode").hidden = !c.unlocked;
  $("#chanLock").hidden = !c.unlocked;
  const keysOnDev = (c.keys || []).filter((k) => k.on === "dev");
  $("#chanLock").disabled = busy || keysOnDev.length > 0;
  $("#chanLock").title = keysOnDev.length ? "Switch everything back to Stable first" : "";
  ["#chanUnlock", "#chanNewCode", "#chanGo"].forEach((s) => { $(s).disabled = busy; });
  $("#chanHint").textContent = "";
  if (c.mode === "updaters") {
    // a deployer for paired games only: nothing to unlock here; the code goes with a game's switch
    ["#chanUnlock", "#chanNewCode", "#chanLock", "#chanGo", "#chanCancel"].forEach((s) => { $(s).hidden = true; });
    $("#chanForm").hidden = false;
    $("#chanFormHead").textContent = "Access code";
    $("#chanHint").textContent = "Enter the code, then pick Dev on a game.";
  }
  // a code that no longer works is said on each row on Dev; here only what .channel itself says
  const warn = c.problem || "";
  $("#chanWarn").hidden = !warn;
  $("#chanWarn").textContent = warn;
  const img = (ref) => `<code class="mono chan-img">${esc(ref).replace(/\//g, "/<wbr>")}</code>`;
  // The source offer (GPL, AGPL) for the owner on Dev: the panel redirects to it, so the
  // address is in no answer this page reads.
  const srcLinks = (r) => r.images.filter((x) => x.has_source).map((x) =>
    `<a href="/api/deploy/channel/source?stable=${encodeURIComponent(x.stable)}" target="_blank" `
    + `rel="noopener noreferrer">${esc(x.license ? x.license + " source" : "Source")}</a>`).join("<br>");
  const anyDev = onDev.length > 0;
  $("#chanSrcHead").hidden = !anyDev;
  $("#chanBody").innerHTML = rows.map((r) => {
    const dev = r.on === "dev";
    let build;
    if (r.kind === "paired") build = "";
    else {
      build = r.images.map((x) => (x.applies ? img(x.dev_name) : `${img(x.dev_name)}<span class="chan-what">${esc(x.note || "")}</span>`)
        + (x.what ? `<span class="chan-what">${esc(x.what)}</span>` : "")).join("<br>");
      if (!r.enabled) build += '<span class="chan-what">Off</span>';
    }
    const lockedOut = r.kind === "key" && !c.unlocked && !dev;
    const seg = (r.channels || ["stable", "dev"]).map((to) => {
      const here = r.on === to;
      const off = busy || (to !== "stable" && lockedOut && !here);
      return `<button type="button" data-ck="${esc(r.key)}" data-kind="${r.kind}" data-to="${to}"`
        + `${here ? ' class="on" aria-pressed="true"' : ' aria-pressed="false"'}${off ? " disabled" : ""}`
        + `${to !== "stable" && lockedOut ? ' title="Enter an access code first"' : ""}>${esc(chanWord(to))}</button>`;
    }).join("");
    const notes = (r.problem ? `<span class="chan-note bad">${esc(r.problem)}</span>` : "")
      + (r.note ? `<span class="chan-note warn">${esc(r.note)}</span>` : "")
      + (r.other ? `<span class="chan-note">On ${esc(r.other)}</span>` : "");
    return `<tr><td class="strong" data-label="Server"><div>${esc(r.name)}${r.sub ? `<div class="muted">${esc(r.sub)}</div>` : ""}${notes}</div></td>`
      + `<td data-label="Dev build"><div>${build || '<span class="muted">-</span>'}</div></td>`
      + `<td data-label="Follows"><div><div class="seg" role="group" aria-label="${esc(r.name)} follows">${seg}</div>`
      + `${dev && r.since ? `<span class="chan-when">On Dev since ${esc(updDate(r.since))}</span>` : ""}</div></td>`
      + (anyDev ? `<td data-label="Source"><div>${dev && srcLinks(r) ? srcLinks(r) : '<span class="muted">-</span>'}</div></td>` : "")
      + "</tr>";
  }).join("") || `<tr><td colspan="${anyDev ? 4 : 3}" class="muted">No dev builds yet.</td></tr>`;
  const gapNames = [...new Set((c.images || []).filter((r) => !r.dev_name).map((r) => r.stable.split("/").pop()))];
  $("#chanGaps").hidden = !gapNames.length;
  $("#chanGaps").innerHTML = "Stable only: "
    + gapNames.map((n) => `<code class="mono">${esc(n)}</code>`).join(", ") + ".";
  $("#chanSource").hidden = true;
  renderIssuer(c);
}

const ISSUER_FROM = { env: "Set in server settings." };

function renderIssuer(c) {
  const iss = c.issuer;
  $("#chanAdv").hidden = !iss;
  if (!iss) return;
  const inp = $("#chanIssuer");
  if (document.activeElement !== inp) inp.value = iss.value;
  const env = iss.from === "env";
  inp.readOnly = env;
  $("#chanIssuerSave").hidden = env;
  $("#chanIssuerDefault").hidden = env || iss.from === "default";
  $("#chanIssuerHint").textContent = ISSUER_FROM[iss.from] || (iss.value === "off" ? "Short codes are off." : "Type off to turn short codes off.");
}

async function chanPost(body, okText) {
  const r = await api("/api/deploy/channel", { method: "POST", headers: { "Content-Type": "application/json" },
                                               body: JSON.stringify(body) });
  if (okText) toast(okText);
  if (r.job) { await probeDeploy(); renderDeploy(); followJob(r.job); }
  return r;
}

async function chanUnlock(code) {
  try {
    await chanPost({ unlock: code }, "Checking the code...");
    chanForm(false);
    return true;
  } catch (err) {
    $("#chanResult").hidden = false;
    $("#chanResult").textContent = err.message;
    return false;
  }
}

function chanConfirm(r, to) {
  const word = chanWord(to);
  let text = `Switch ${r.name} to ${word}?`;
  if (r.kind === "paired" && to !== "stable" && !$("#chanCode").value.trim()) text += "\n\nNo access code entered.";
  else if (r.enabled) text += "\n\nEveryone playing it may be disconnected.";
  if (to === "stable" && r.back) text += "\n\n" + r.back;
  if ((r.would || []).length) text += "\n\n" + r.would.join("\n");
  return confirm(text);
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  const sw = t.closest("#chanBody [data-ck]");
  if (sw && !sw.disabled) {
    const r = chanRows(CHAN || {}).find((x) => x.key === sw.dataset.ck && x.kind === sw.dataset.kind);
    const to = sw.dataset.to;
    if (!r || r.on === to) return;
    if (!chanConfirm(r, to)) return;
    try {
      if (r.kind === "paired") {
        // the access code box is the one place a code is typed; a switch off Stable takes it along
        const code = to !== "stable" ? $("#chanCode").value.trim() : "";
        $("#chanCode").value = "";
        const j = await api("/api/updates/channel", { method: "POST", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify(code ? { key: r.key, channel: to, code } : { key: r.key, channel: to }) });
        toast(`Switching to ${chanWord(to)}...`);
        await probeDeploy(); renderDeploy(); followJob(j.job);
      } else {
        await chanPost({ key: r.key, to }, `Switching to ${chanWord(to)}...`);
      }
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.closest("#chanUnlock")) return chanForm($("#chanForm").hidden, false);
  if (t.closest("#chanNewCode")) return chanForm($("#chanForm").hidden, true);
  if (t.closest("#chanCancel")) return chanForm(false);
  if (t.closest("#chanLock")) {
    if (!confirm("Forget the access code?")) return;
    try { await chanPost({ lock: true }, "Forgetting the code..."); } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.closest("#chanIssuerSave") || t.closest("#chanIssuerDefault")) {
    const value = t.closest("#chanIssuerDefault") ? "" : $("#chanIssuer").value.trim();
    const res = $("#chanIssuerResult");
    res.hidden = true;
    try {
      await chanPost({ issuer: value }, value ? "Saved." : "Reset to default.");
      loadChannel();
    } catch (err) { res.hidden = false; res.textContent = err.message; }
    return;
  }
  if (t.closest("#chanGo")) {
    const code = $("#chanCode").value.trim();
    $("#chanCode").value = "";
    if (!code) { $("#chanResult").hidden = false; $("#chanResult").textContent = "Enter the access code."; return; }
    const btn = t.closest("#chanGo");
    btn.disabled = true;
    $("#chanResult").hidden = true;
    btn.textContent = "Checking...";
    if (!(await chanUnlock(code)) && FEED && !FEED.feed) {
      // a code that only opens patches from the developers: try it there before saying no
      try {
        await api("/api/deploy/patches/feed", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ enabled: true, code }) });
        chanForm(false);
        toast("Developer patches on.");
        loadFeed();
      } catch (x) { /* the channel's answer stays shown */ }
    }
    btn.textContent = "Use this code";
    btn.disabled = false;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "chanCode") {
    e.preventDefault();
    if (CHAN && CHAN.mode === "updaters") return;    // the code waits for a game's switch
    $("#chanGo").click();
  }
});

// job times are ISO strings (or epoch seconds from an older deployer), null until set
function jobWhen(j) {
  const t = j.started || j.created;
  if (t === null || t === undefined || t === "") return "";
  const ms = typeof t === "number" ? t * 1000 : Date.parse(t);
  return isNaN(ms) ? "" : ago(ms);
}

// ---- Install games > Patch bundles (the deployer's patch actions) ----
let PATCHES = null;
const PATCH_WORD = { patch_import: "Import bundle", patch_build: "Build bundle", patch_rollback: "Roll back",
                     patch_remove: "Remove bundle", patch_check: "Check bundles", patch_fetch: "Get developer patches" };
Object.assign(ACT_WORD, PATCH_WORD);

async function loadPatches(recheck) {
  // a deployer for paired games only (POL_DEPLOY_MODE=updaters) serves no patches
  if (!DEPLOY || !DEPLOY.available || DEPLOY.mode === "updaters") { $("#patchPanel").hidden = true; return; }
  let r;
  try { r = await api("/api/deploy/patches" + (recheck ? "?recheck=1" : "")); }
  catch (e) {
    $("#patchPanel").hidden = false;
    $("#patchBody").innerHTML = `<tr><td colspan="6" class="muted">${esc(e.message)}</td></tr>`;
    return;
  }
  PATCHES = r;
  $("#patchPanel").hidden = false;
  if (recheck && r.job) followJob(r.job);
  const busy = ((DEPLOY || {}).jobs || []).some((j) => !JOB_DONE.has(j.state));
  ["#patchImport", "#patchBuild", "#patchCheck"].forEach((s) => { $(s).disabled = busy; });
  $("#patchNote").textContent = (r.bundles || []).length ? `${r.bundles.length} bundle${r.bundles.length === 1 ? "" : "s"}` : "";
  $("#patchBody").innerHTML = (r.bundles || []).map((b) => {
    const key = `${b.region}-${b.product}`;
    const sc = b.selfcheck === "ok" ? '<span class="pill live">OK</span>' : b.selfcheck === "failed" ? '<span class="pill off">Failed</span>'
      : '<span class="pill env">Not run</span>';
    const older = (b.versions || []).slice(0, -1);
    return `<tr><td class="strong">${esc(b.title || key)}<div class="muted mono">${esc(key)}${(b.aliases || []).length ? " · " + esc(b.aliases.join(", ")) : ""}</div>`
      + `${b.error ? `<div class="patch-why">${esc(b.error)}</div>` : ""}</td>`
      + `<td><span class="mono">${esc(b.latest || "-")}</span><div class="muted"><a href="#" data-prov="${esc(key)}">${esc((b.versions || []).length)} version${(b.versions || []).length === 1 ? "" : "s"}</a></div></td>`
      + `<td>${b.served ? '<span class="pill live">Yes</span>' : '<span class="pill off">No</span>'}</td>`
      + `<td>${sc}${b.selfcheck_at ? `<div class="muted">${esc(jobWhen({ started: b.selfcheck_at }))}</div>` : ""}`
      + `${b.selfcheck_why ? `<div class="patch-why">${esc(b.selfcheck_why)}</div>` : ""}</td>`
      + `<td>${esc(impBytes(b.size))}</td>`
      + `<td><div class="deploy-acts">${older.length ? `<select data-rollto="${esc(key)}"><option value="">Roll back to...</option>`
        + older.slice().reverse().map((v) => `<option>${esc(v)}</option>`).join("") + `</select>` : ""}`
      + `<button class="danger sm" data-patchrm="${esc(key)}"${busy ? " disabled" : ""}>Remove</button></div></td></tr>`
      + `<tr class="prov-row" data-provrow="${esc(key)}" hidden><td colspan="6" data-label=""></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">No patch bundles yet.</td></tr>';
  PROV = null;
}

// ---- where each version of a bundle comes from (GET /api/deploy/patches/provenance) ----
let PROV = null;
const PROV_WORD = { retail: ["Retail", "live"], ours: ["Ours", "wait"], "our-label": ["Renumbered", "env"],
                    unknown: ["Unknown", "off"], disc: ["Disc", "env"], missing: ["Missing", "off"] };
// newest first. A retail version (20250916_1) by its date, then its suffix, shorter first (a
// same-day _9 before _10); anything else (a disc's 1.30, our own numbers) by its numbers.
const PROV_RETAIL = /^\d{8}_\w{1,2}$/;
const provKey = (v) => [v.slice(0, 8), String(v.length - 9).padStart(2, "0"), v.slice(9)].join("|");
function provNewer(x, y) {
  const a = String(x.version || ""), b = String(y.version || "");
  if (PROV_RETAIL.test(a) && PROV_RETAIL.test(b)) return provKey(b) < provKey(a) ? -1 : provKey(b) > provKey(a) ? 1 : 0;
  return b.localeCompare(a, undefined, { numeric: true });
}

function provHtml(p) {
  if (p.error) return `<div class="patch-why">${esc(p.error)}</div>`;
  const row = (r) => {
    const [word, pill] = PROV_WORD[r.origin] || [r.origin, "env"];
    return `<tr><td class="mono">${esc(r.version)}</td><td><span class="pill ${pill}">${esc(word)}</span></td>`
      + `<td class="muted">${esc(r.why || "")}</td></tr>`;
  };
  const held = (p.versions || []).slice().reverse();
  const lost = (p.missing || []).slice().reverse();
  const top = [p.final_list ? "" : "Missing list may be incomplete.", p.note || ""].filter(Boolean).join(" ");
  return (top ? `<div class="muted" style="margin:2px 0 6px">${esc(top)}</div>` : "")
    + `<table class="cards"><thead><tr><th>Version</th><th>Source</th><th>Note</th></tr></thead><tbody>`
    + [].concat(held, lost.map((r) => Object.assign({}, r, { origin: "missing" })), p.discs || [])
      .sort(provNewer)
      .map(row).join("") + `</tbody></table>`;
}

// ---- a game's patches, on its page (adminpatches.py, GET /api/games/patches) ----
// One row per bundle the patch service hands out for the game: the version it serves as
// newest, what the versions here are, and the retail ones known to exist that are not;
// a row opens to the whole timeline (provHtml). A game with no bundles shows nothing.
const GAME_PATCHES = {};          // key -> {at, loading, data | error}
const GAME_PATCH_OPEN = new Set();  // "<key>:<REGION-PRODUCT>" rows that are open
let GAME_SHOWN = null;            // the game whose Overview is on screen (renderGame)
async function loadGamePatches(key) {
  const c = GAME_PATCHES[key];
  if (c && c.loading) return;
  GAME_PATCHES[key] = Object.assign({}, c, { at: Date.now(), loading: true });
  let got;
  try { got = { data: await api("/api/games/patches?key=" + encodeURIComponent(key)) }; }
  catch (e) { got = { error: e.message }; }
  GAME_PATCHES[key] = Object.assign({ at: Date.now(), loading: false }, got);
  if (GAME_SHOWN === key && $("#gamesMain .gx-layout") && !gamesBusy() && !gamesSelecting()) keepGamesView(() => renderGame(key));
}
function gamePatchBox(g) {
  const c = GAME_PATCHES[g.key];
  if (!c || Date.now() - c.at > 60000) loadGamePatches(g.key);
  if (!c || (!c.data && !c.error)) return "";
  if (c.error) return `<section class="panel gx-wide"><h3>Patches</h3><p class="muted">${esc(c.error)}</p></section>`;
  const bs = c.data.bundles || [];
  if (!bs.length) return "";
  const many = new Set(bs.map((b) => b.product)).size > 1;
  const word = (o) => (PROV_WORD[o] || [o, "env"]);
  const rows = bs.map((b) => {
    const id = `${b.region}-${b.product}`, open = GAME_PATCH_OPEN.has(g.key + ":" + id);
    if (b.error) return `<tr><td colspan="4" data-label=""><b>${esc(id)}</b> <span class="patch-why">${esc(b.error)}</span></td></tr>`;
    const n = {};
    (b.versions || []).forEach((r) => { n[r.origin] = (n[r.origin] || 0) + 1; });
    const here = Object.entries(n).map(([o, k]) => `${k} ${word(o)[0].toLowerCase()}`).join(", ");
    const top = (b.versions || []).find((r) => r.version === b.latest);
    const where = many ? b.title : (String(b.title || "").match(/\(([^)]*)\)\s*$/) || [, b.title])[1];
    const lost = (b.missing || []).length;
    return `<tr class="gp-row" data-gp="${esc(g.key + ":" + id)}" tabindex="0" aria-expanded="${open}">`
      + `<td><span class="gp-caret" aria-hidden="true">${open ? "▾" : "▸"}</span> <b>${esc(where)}</b> <span class="muted mono">${esc(id)}</span></td>`
      + `<td>${b.latest ? `<span class="mono">${esc(b.latest)}</span> ${top ? `<span class="pill ${word(top.origin)[1]}">${esc(word(top.origin)[0])}</span>` : ""}` : '<span class="muted">-</span>'}</td>`
      + `<td>${(b.versions || []).length}${here ? ` <span class="muted">· ${esc(here)}</span>` : ""}</td>`
      + `<td>${lost ? `<span class="pill off">${lost} missing</span>` : `<span class="muted">None</span>`}`
      + `${b.final_list || bs.every((x) => !x.final_list) ? "" : ' <span class="muted">*</span>'}</td></tr>`
      + `<tr data-gpbody="${esc(g.key + ":" + id)}"${open ? "" : " hidden"}><td colspan="4" data-label="">${open ? provHtml(b) : ""}</td></tr>`;
  }).join("");
  const at = Date.parse(c.data.at || "");
  const when = isNaN(at) ? (c.data.at || "") : ago(at);
  // Square Enix's full list is unknown for some: said once, for all of them or the starred ones
  const open = bs.filter((b) => !b.error && !b.final_list).length;
  const unknown = !open ? "" : open === bs.filter((b) => !b.error).length
    ? "Missing counts may be incomplete."
    : "* Missing count may be incomplete.";
  return `<section class="panel gx-wide gx-patches"><h3>Patches <span class="muted">${bs.length} bundle${bs.length === 1 ? "" : "s"}</span></h3>`
    + (when || unknown ? `<p class="muted gx-note">${esc([when ? "Updated " + when + "." : "", unknown].filter(Boolean).join(" "))}</p>` : "")
    + `<div class="tablewrap"><table class="cards"><thead><tr><th>Bundle</th><th>Serving</th><th>Versions</th><th>Missing</th></tr></thead>`
    + `<tbody>${rows}</tbody></table></div></section>`;
}
function toggleGamePatch(row) {
  const id = row.dataset.gp, c = GAME_PATCHES[id.split(":")[0]];
  const body = document.querySelector(`[data-gpbody="${CSS.escape(id)}"]`);
  if (!body || !c || !c.data) return;
  const open = body.hidden;
  if (open) GAME_PATCH_OPEN.add(id); else GAME_PATCH_OPEN.delete(id);
  if (open && !body.firstElementChild.innerHTML) {
    const b = c.data.bundles.find((x) => `${x.region}-${x.product}` === id.slice(id.indexOf(":") + 1));
    body.firstElementChild.innerHTML = b ? provHtml(b) : "";
    labelTables(body);
  }
  body.hidden = !open;
  row.setAttribute("aria-expanded", String(open));
  row.querySelector(".gp-caret").textContent = open ? "▾" : "▸";
}
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-gp]");
  if (row) toggleGamePatch(row);
});
document.addEventListener("keydown", (e) => {
  const row = e.target.closest && e.target.closest("tr[data-gp]");
  if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleGamePatch(row); }
});

document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-prov]");
  if (!a) return;
  e.preventDefault();
  const tr = document.querySelector(`[data-provrow="${a.dataset.prov}"]`);
  if (!tr) return;
  if (!tr.hidden) { tr.hidden = true; return; }
  try {
    if (!PROV) PROV = await api("/api/deploy/patches/provenance");
    const p = (PROV.bundles || []).find((x) => `${x.region}-${x.product}` === a.dataset.prov);
    tr.firstElementChild.innerHTML = p ? provHtml(p) : '<span class="muted">No history.</span>';
    labelTables(tr);
  } catch (x) { tr.firstElementChild.innerHTML = `<span class="patch-why">${esc(x.message)}</span>`; }
  tr.hidden = false;
});

// ---- Install games > From the developers (the dev channel's patch feed) ----
let FEED = null;
const FEED_STATE = { new: ["New", "env"], behind: ["Behind", "wait"], current: ["Up to date", "live"],
                     ahead: ["Newer here", "live"], custom: ["Own versions", "off"], unavailable: ["Unavailable", "off"] };

async function loadFeed(refresh) {
  const panel = $("#feedPanel");
  if (!DEPLOY || !DEPLOY.available || DEPLOY.mode === "updaters") { panel.hidden = true; return; }
  let r;
  try { r = await api("/api/deploy/patches/feed" + (refresh ? "?refresh=1" : "")); }
  catch (e) {
    panel.hidden = false;
    $("#feedOff").hidden = true;
    $("#feedOnBlock").hidden = false;
    $("#feedBody").innerHTML = `<tr><td colspan="5" class="muted">${esc(e.message)}</td></tr>`;
    return;
  }
  FEED = r;
  // only for a server whose access code includes patches (typed once, in the access code box)
  panel.hidden = !r.feed && !r.can_enable;
  $("#feedPill").textContent = r.feed ? "On" : "Off";
  $("#feedPill").className = "pill " + (r.feed ? "live" : "env");
  $("#feedOff").hidden = !!r.feed;
  $("#feedOnBlock").hidden = !r.feed;
  if (!r.feed) return;
  const busy = ((DEPLOY || {}).jobs || []).some((j) => !JOB_DONE.has(j.state));
  $("#feedRefresh").disabled = busy;
  $("#feedBody").innerHTML = (r.bundles || []).map((b) => {
    const key = `${b.region}-${b.product}`;
    const [word, pill] = FEED_STATE[b.status] || [b.status, "env"];
    const can = (b.status === "new" || b.status === "behind") && b.files > 0;
    return `<tr><td class="strong">${esc(b.title || key)}<div class="muted mono">${esc(key)}</div></td>`
      + `<td><span class="mono">${esc(b.have || "-")}</span></td>`
      + `<td><span class="mono">${esc(b.latest)}</span></td>`
      + `<td><span class="pill ${pill}">${esc(word)}</span>${b.why ? `<div class="patch-why">${esc(b.why)}</div>` : ""}</td>`
      + `<td>${can ? `<button class="act sm" data-feedget="${esc(key)}"${busy ? " disabled" : ""}>`
        + `${b.status === "new" ? "Get" : "Update"} (${esc(impBytes(b.size))})</button>` : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="muted">No patches yet.</td></tr>';
}

async function patchAct(body, sure) {
  if (sure === false) return;
  try {
    const r = await api("/api/deploy/patch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("Started.");
    await probeDeploy(); renderDeploy();
    followJob(r.job);
  } catch (e) { toast(e.message, true); }
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#patchImport")) return openPatchDialog("patch_import");
  if (e.target.closest("#patchBuild")) return openPatchDialog("patch_build");
  if (e.target.closest("#patchCheck")) return loadPatches(true);
  if (e.target.closest("#feedRefresh")) return loadFeed(true);
  if (e.target.closest("#feedOn") || e.target.closest("#feedOffBtn")) {
    const on = !!e.target.closest("#feedOn");
    // on: the deployer uses the access code it keeps (entered in the access code box)
    const body = { enabled: on };
    return (async () => {
      try {
        await api("/api/deploy/patches/feed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        toast(on ? "Developer patches on." : "Developer patches off.");
      } catch (x) { toast(x.message, true); }
      loadFeed();
    })();
  }
  const get = e.target.closest("[data-feedget]");
  if (get) {
    const b = (FEED.bundles || []).find((x) => `${x.region}-${x.product}` === get.dataset.feedget);
    return patchAct({ action: "patch_fetch", region: b.region, product: b.product },
      confirm(`${b.status === "new" ? "Get" : "Update"} ${b.title || get.dataset.feedget}?\n\nDownloads ${impBytes(b.size)}.`));
  }
  const rm = e.target.closest("[data-patchrm]");
  if (rm) {
    const b = (PATCHES.bundles || []).find((x) => `${x.region}-${x.product}` === rm.dataset.patchrm);
    const name = b.title || rm.dataset.patchrm;
    // Remove and Roll back both take files out: the same dialog, naming the bundle
    return uiAsk({ title: `Remove ${name}?`, danger: true, ok: "Remove",
      text: "Players can no longer download it." })
      .then((sure) => patchAct({ action: "patch_remove", region: b.region, product: b.product }, !!sure));
  }
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-rollto]");
  if (!sel || !sel.value) return;
  const b = (PATCHES.bundles || []).find((x) => `${x.region}-${x.product}` === sel.dataset.rollto);
  const to = sel.value;
  sel.value = "";
  const name = b.title || sel.dataset.rollto;
  const gone = (b.versions || []).slice((b.versions || []).indexOf(to) + 1);
  uiAsk({ title: `Roll ${name} back to ${to}?`, danger: true, ok: "Roll back",
    text: `Removes ${gone.join(", ")}.` })
    .then((sure) => patchAct({ action: "patch_rollback", region: b.region, product: b.product, to }, !!sure));
});

async function loadDeploy() {
  await probeDeploy();
  renderDeploy();
  loadPatches();
  loadFeed();
  loadUpdates(false);
  if (!DEPLOY || !DEPLOY.available) { renderChannel(); return; }
  loadChannel();
  // a job's log shows while it runs (or when it is picked under Recent jobs), not the last one's every visit
  const live = (DEPLOY.jobs || []).find((j) => !JOB_DONE.has(j.state));
  if (live && live.id !== DEPLOY_JOB) followJob(live.id);
}

// the log goes under the panel whose button started the job
function placeJob(j) {
  const act = String(j.action || "");
  const after = act === "patch_fetch" ? "#feedPanel" : act.startsWith("patch_") ? "#patchPanel"
    : act === "channel" ? "#chanPanel" : act === "updater" && !updByDeployer(j.game) ? "#updDeploy" : "#deployPanel";
  const anchor = $(after), panel = $("#deployJobPanel");
  const at = anchor && !anchor.hidden ? anchor : $("#deployPanel");
  if (at && at.nextElementSibling !== panel) at.after(panel);
}

async function showJob(id) {
  let j;
  try { j = await api("/api/deploy/job?id=" + encodeURIComponent(id)); } catch (e) { return; }
  DEPLOY_JOB = id;
  placeJob(j);
  $("#deployJobPanel").hidden = false;
  $("#deployJobTitle").textContent = `${ACT_WORD[j.action] || j.action} ${j.title || j.game}`;
  $("#deployJobState").textContent = JOB_WORD[j.state] || j.state;
  $("#deployJobSummary").textContent = j.summary || "";
  // new client data reaches a game that is already on only when it is turned on again
  const jg = ((DEPLOY || {}).games || []).find((g) => g.key === j.game);
  if (j.action === "import" && j.state === "ok" && jg && jg.enabled) {
    $("#deployJobSummary").insertAdjacentHTML("beforeend", ` <span>Turn it on again to use the new data.</span>`
      + ` <button class="act sm" data-deploy="enable" data-game="${esc(jg.key)}">Turn on again</button>`);
  }
  const pre = $("#deployLog");
  pre.textContent = j.log || "";
  pre.scrollTop = pre.scrollHeight;
  DEPLOY_OFFSET = j.log_size || (j.log || "").length;
  return j;
}

async function followJob(id) {
  stopDeployPoll();
  const j = await showJob(id);
  if (!j || JOB_DONE.has(j.state)) return;
  DEPLOY_TIMER = setInterval(async () => {
    if (CUR_TAB !== "deploy") return;
    let r;
    try { r = await api(`/api/deploy/log?id=${encodeURIComponent(id)}&offset=${DEPLOY_OFFSET}`); }
    catch (e) { return; }                   // the deployer or the panel restarting: keep trying
    if (r.text) {
      const pre = $("#deployLog");
      const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
      pre.textContent += r.text;
      if (stick) pre.scrollTop = pre.scrollHeight;
    }
    // the deployer says where the text it sent started; the next read follows on from it
    DEPLOY_OFFSET = typeof r.next === "number" ? r.next
      : DEPLOY_OFFSET + new TextEncoder().encode(r.text || "").length;
    if (JOB_DONE.has(r.state)) {
      stopDeployPoll();
      const done = await showJob(id);
      await probeDeploy();
      renderDeploy();
      if (j.action === "channel") { loadChannel(); loadUpdates(false); loadFeed(); }
      if (j.action === "updater") {
        if (done) UPD_RUN[done.game || j.game] = { job: id, state: done.state, summary: done.summary || "" };
        loadUpdates(false);
        // a paired game's switch: the panel stores its channel once it has seen the job end
        setTimeout(loadChannel, 1500);
      }
      if (String(j.action || "").startsWith("patch_") || PATCHES) { loadPatches(); if (FEED && FEED.feed) loadFeed(); }
    }
  }, 1500);
}

function stopDeployPoll() { clearInterval(DEPLOY_TIMER); DEPLOY_TIMER = null; }

// ---- Install games > Import from client: hand the deployer a game client ----
// Two ways in: an archive the browser uploads (streamed through the panel to the deployer),
// or a folder in the deployer's clients folder (POL_DEPLOY_CLIENTS). The deployer checks the
// item's expected files before it runs anything; a 400 lists what is missing.
let IMP = null;

function impBytes(n) {
  if (!(n >= 0)) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const impMatch = (ms) => (ms || []).find((m) => m.game === IMP.game && m.item === IMP.item);
const impPatchMode = () => IMP && (IMP.mode === "patch_import" || IMP.mode === "patch_build");
const impMatches = (ms) => !!impMatch(ms);
// an upload's match may name the folder inside it that holds the item (its "subdir")
const impUploadSource = (id, ms) => { const m = impMatch(ms); return m && m.subdir ? { upload: id, subdir: m.subdir } : { upload: id }; };

function impChoose(src, words) {
  IMP.source = src;
  $("#impChosen").textContent = src ? "From " + words : "";
  $("#impGo").disabled = !src;
  $("#impMissing").hidden = true;
  document.querySelectorAll("#impModal .imp-list li").forEach((li) =>
    li.classList.toggle("chosen", !!src && li.dataset.key === JSON.stringify(src)));
}

function impTab(which) {
  document.querySelectorAll("#impModal [data-imp]").forEach((b) => b.classList.toggle("active", b.dataset.imp === which));
  $("#impUpload").hidden = which !== "upload";
  $("#impBrowse").hidden = which !== "browse";
  impChoose(null);
  if (which === "browse") impBrowse(IMP.path || "");
  else impUploads();
}

async function impUploads() {
  let r;
  try { r = await api("/api/deploy/uploads"); } catch (e) { $("#impUploads").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  $("#impUploads").innerHTML = (r.uploads || []).map((u) => {
    if (IMP.mode === "patch_import" && u.kind !== "polpatch") return "";
    if (IMP.mode === "patch_build" && u.kind === "polpatch") return "";
    const sub = (impMatch(u.matches) || {}).subdir || "";
    const fits = IMP.mode === "patch_import" ? u.kind === "polpatch" : impMatches(u.matches);
    const what = u.polpatch && !u.polpatch.error ? ` ${u.polpatch.title || ""} ${u.polpatch.region}-${u.polpatch.product}, ${(u.polpatch.versions || []).join(", ")}` : "";
    return `<li data-key="${esc(JSON.stringify(impUploadSource(u.id, u.matches)))}" data-upload="${esc(u.id)}" data-sub="${esc(sub)}" data-name="${esc(u.name)}" class="${fits ? "match" : ""}">`
      + `<span class="t1">${esc(u.name || u.id)}${what ? `<span class="muted">${esc(what)}</span>` : ""}</span><span class="t2">${esc(impBytes(u.size))}, ${esc(u.files)} files`
      + ` <button class="ghost sm" data-imp-del="${esc(u.id)}">Remove</button></span></li>`;
  }).join("") || '<li class="empty">No uploads yet.</li>';
}

async function impBrowse(path) {
  let r;
  try { r = await api("/api/deploy/browse?path=" + encodeURIComponent(path)); }
  catch (e) { $("#impDirs").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  IMP.path = r.path || "";
  IMP.parent = r.parent;
  $("#impPath").textContent = "/" + IMP.path;
  $("#impUp").disabled = r.parent === null || r.parent === undefined;
  const here = JSON.stringify({ path: IMP.path });
  $("#impDirs").innerHTML = (IMP.mode === "patch_import" ? "" : `<li data-key="${esc(here)}" data-use="${esc(IMP.path)}" class="${impMatches(r.matches) ? "match" : ""}">`
      + `<span class="t1">Use this folder</span><span class="t2">${(r.files || []).length === 1 ? "1 file" : esc((r.files || []).length) + " files"}</span></li>`)
    + (r.dirs || []).map((d) => `<li data-dir="${esc((IMP.path ? IMP.path + "/" : "") + d)}"><span class="t1">${esc(d)}/</span></li>`).join("")
    + (IMP.mode === "patch_import" ? (r.files || []).filter((f) => /\.polpatch$/i.test(f.name)).map((f) => {
        const rel = (IMP.path ? IMP.path + "/" : "") + f.name, pp = f.polpatch || {};
        return `<li data-key="${esc(JSON.stringify({ path: rel }))}" data-usefile="${esc(rel)}" class="${pp.error ? "" : "match"}">`
          + `<span class="t1">${esc(f.name)}<span class="muted"> ${esc(pp.error || ((pp.title || "") + " " + (pp.versions || []).join(", ")))}</span></span>`
          + `<span class="t2">${esc(impBytes(f.size))}</span></li>`;
      }).join("") : "")
    + (r.truncated ? '<li class="empty">Some folders not shown.</li>' : "");
  if (!impPatchMode() && impMatches(r.matches)) impChoose({ path: IMP.path }, "/" + IMP.path);
}

// the same dialog imports a .polpatch or builds one from an install
async function openPatchDialog(mode) {
  IMP = { mode, game: null, item: null, path: "", source: null };
  $("#impVerb").textContent = mode === "patch_import" ? "Import" : "Build";
  $("#impWhat").textContent = mode === "patch_import" ? "a patch bundle" : "a patch bundle from an install";
  $("#impHint").textContent = mode === "patch_import" ? "Upload a .polpatch file or pick one." : "Upload the install or pick its folder.";
  $("#impBuild").hidden = mode !== "patch_build";
  $("#impProgress").hidden = true;
  $("#impGo").textContent = mode === "patch_import" ? "Import" : "Build and import";
  $("#impModal").classList.add("show");
  impTab("upload");
  let canBrowse = true;
  try { await api("/api/deploy/browse?path="); } catch (e) { canBrowse = false; }
  $("#impBrowseTab").hidden = !canBrowse;
}

async function openImport(game, item) {
  const g = ((DEPLOY || {}).games || []).find((x) => x.key === game) || {};
  const m = (g.missing || []).find((x) => x.item === item && x.import) || {};
  IMP = { mode: "data", game, item, path: "", source: null };
  $("#impBuild").hidden = true;
  $("#impVerb").textContent = "Import";
  $("#impGo").textContent = "Import";
  $("#impWhat").textContent = `${g.name || game}: ${item}`;
  $("#impHint").textContent = (m.import || {}).hint ? `Pick ${m.import.hint}.` : "";
  $("#impProgress").hidden = true;
  $("#impModal").classList.add("show");
  impTab("upload");
  // the folder tab only when the deployer has a clients folder
  let canBrowse = true;
  try { await api("/api/deploy/browse?path="); } catch (e) { canBrowse = false; }
  $("#impBrowseTab").hidden = !canBrowse;
}

function impSend(file) {
  if (!file || !IMP) return;
  const x = new XMLHttpRequest();
  x.open("POST", "/api/deploy/upload");
  x.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  x.setRequestHeader("Content-Type", "application/octet-stream");
  $("#impProgress").hidden = false;
  $("#impBar").style.width = "0";
  $("#impPct").textContent = "Uploading " + file.name;
  $("#impGo").disabled = true;
  x.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.floor(100 * e.loaded / e.total);
    $("#impBar").style.width = pct + "%";
    $("#impPct").textContent = pct < 100 ? `${pct}% of ${impBytes(e.total)}` : "Unpacking...";
  };
  x.onload = async () => {
    let j = {}; try { j = JSON.parse(x.responseText || "{}"); } catch (e) { /* not JSON */ }
    if (x.status !== 201) { $("#impPct").textContent = j.error || "Upload failed. Try again."; return; }
    $("#impPct").textContent = `Uploaded ${j.files} files.`;
    await impUploads();
    impChoose(impUploadSource(j.upload, j.matches), file.name);
  };
  x.onerror = () => { $("#impPct").textContent = "Upload stopped. Check the connection and try again."; };
  x.send(file);
}

async function impGo() {
  if (!IMP || !IMP.source) return;
  $("#impGo").disabled = true;
  let r;
  if (impPatchMode()) {
    const body = { action: IMP.mode, source: IMP.source };
    if (IMP.mode === "patch_build") {
      Object.assign(body, { region: $("#impRegion").value.trim().toUpperCase(), product: $("#impProduct").value.trim(),
        version: $("#impVersion").value.trim(), walk: $("#impWalk").checked,
        aliases: $("#impAliases").value.toUpperCase().split(/[\s,]+/).filter(Boolean) });
    }
    r = await fetch("/api/deploy/patch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } else {
    r = await fetch("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "import", game: IMP.game, item: IMP.item, source: IMP.source }) });
  }
  let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status !== 202) {
    const miss = (j.problems || []).map((p) => `<div>${esc(p)}</div>`).join("") + (j.missing || []).map((m) => `<div><span class="mono">${esc(m.path)}</span>${m.hint ? ` <span class="muted">${esc(m.hint)}</span>` : ""}</div>`).join("");
    $("#impMissing").innerHTML = esc(j.error || "Couldn't import. Check the files and try again.") + miss;
    $("#impMissing").hidden = false;
    $("#impGo").disabled = false;
    return;
  }
  $("#impModal").classList.remove("show");
  toast("Import started.");
  await probeDeploy(); renderDeploy();
  followJob(j.job);
}

document.addEventListener("click", async (e) => {
  const opener = e.target.closest("[data-import]");
  if (opener) return openImport(opener.dataset.import, opener.dataset.item);
  if (!e.target.closest("#impModal")) return;
  const tab = e.target.closest("[data-imp]");
  if (tab) return impTab(tab.dataset.imp);
  const del = e.target.closest("[data-imp-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("Remove this upload?")) return;
    try { await api("/api/deploy/upload/delete", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify({ upload: del.dataset.impDel }) }); }
    catch (err) { toast(err.message, true); }
    impChoose(null);
    return impUploads();
  }
  const li = e.target.closest(".imp-list li");
  if (li && li.dataset.upload) return impChoose(li.dataset.sub ? { upload: li.dataset.upload, subdir: li.dataset.sub }
                                                                : { upload: li.dataset.upload }, li.dataset.name || li.dataset.upload);
  if (li && li.dataset.use !== undefined) return impChoose({ path: li.dataset.use }, "/" + li.dataset.use);
  if (li && li.dataset.usefile) return impChoose({ path: li.dataset.usefile }, "/" + li.dataset.usefile);
  if (li && li.dataset.dir) return impBrowse(li.dataset.dir);
  if (e.target.closest("#impUp")) return impBrowse(IMP.parent || "");
  if (e.target.closest("#impCancel") || e.target.id === "impModal") return $("#impModal").classList.remove("show");
  if (e.target.closest("#impGo")) return impGo();
});
document.addEventListener("change", (e) => { if (e.target.id === "impFile") impSend(e.target.files[0]); });
["dragover", "dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => {
  const drop = e.target.closest && e.target.closest("#impDrop");
  if (!drop) return;
  e.preventDefault();
  drop.classList.toggle("over", ev === "dragover");
  if (ev === "drop") impSend(e.dataTransfer.files[0]);
}));

document.addEventListener("click", async (e) => {
  const fx = e.target.closest("[data-deploy-fix]");
  if (fx) {
    const game = fx.dataset.game;
    if (!confirm(`${fx.dataset.label}?${fx.dataset.about ? "\n\n" + fx.dataset.about : ""}`)) return;
    try {
      const r = await api("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ action: "fix", game, item: fx.dataset.deployFix }) });
      toast("Started.");
      await probeDeploy(); renderDeploy();
      followJob(r.job);
    } catch (err) { toast(err.message, true); }
    return;
  }
  const b = e.target.closest("[data-deploy]");
  if (b) {
    const act = b.dataset.deploy, game = b.dataset.game;
    if (act !== "check" && !confirmDeploy(act, game)) return;
    try {
      const r = await api("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ action: act, game }) });
      toast(act === "update" ? "Update started." : "Started.");
      await probeDeploy(); renderDeploy();
      followJob(r.job);
    } catch (err) { toast(err.message, true); }
    return;
  }
  const li = e.target.closest("#deployJobs [data-job]");
  if (li) followJob(li.dataset.job);
  if (e.target.closest("#deployJobClose")) $("#deployJobPanel").hidden = true;
});
