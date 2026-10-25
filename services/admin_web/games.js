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
    if (gamesBusy()) return;
    await fetchGames(false);
    if (gamesBusy()) return;
    renderGamesSide();
    if (key) renderGame(key); else renderGameGrid();
  }, 10000);
}

// ---- status: one word and one colour per game ----
function opsCheck(key) {
  return GAMES_OPS ? (GAMES_OPS.checks || []).find((c) => c.id === key) || null : null;
}

function gameState(g) {
  const live = g.live, ops = opsCheck(g.key);
  if (ops && ops.state === "bad") return { cls: "bad", word: "Trouble", why: ops.detail };
  if (live && live.up === false) return { cls: "bad", word: "Down", why: live.error || "" };
  if (live && live.error) return { cls: "warn", word: "Status unreadable", why: live.error };
  if (ops && ops.state === "warn") return { cls: "warn", word: "Check", why: ops.detail };
  if (!live && !ops) return { cls: "off", word: "No status", why: "This game reports no live status." };
  return { cls: "good", word: "Running", why: "" };
}

const num = (v) => (v === null || v === undefined ? "\u2013" : String(v));
const factText = (f) => f.kind === "time" && typeof f.value === "number"
  ? new Date(f.value * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
  : String(f.value ?? "");

function serviceCount(g) {
  const ops = opsCheck(g.key);
  if (!ops) return null;
  return ops.state === "ok" ? "up" : ops.state;
}

// ---- the sidebar's list of games (under Games) ----
// The PlayOnline Viewer first (it is the platform, not a title), then the games by where
// they run, each group alphabetical. The group is read from the module's platform.
const GAME_GROUPS = [["viewer", ""], ["pol", "PlayOnline"], ["ps2", "PlayStation 2"], ["other", "Other"]];
function gameGroup(g) {
  const p = String(g.platform || "").toLowerCase();
  if (g.key === "viewer" || p === "playonline platform") return "viewer";
  if (p.startsWith("playonline")) return "pol";
  if (p.startsWith("ps2") || p.includes("playstation 2")) return "ps2";
  return "other";
}
function groupedGames() {
  const name = (g) => String(g.short || g.title || g.key).toLowerCase();
  const groups = GAME_GROUPS.map(([id, label]) => [label, GAMES.filter((g) => gameGroup(g) === id)
    .sort((a, b) => name(a).localeCompare(name(b)))]).filter(([, gs]) => gs.length);
  // without the PlayOnline core one heading over every game says nothing
  return coreOff() && groups.length === 1 ? [["", groups[0][1]]] : groups;
}

function renderGamesSide() {
  const cur = CUR_TAB === "pml" ? "viewer" : CUR_TAB === "games" ? (GAME_TOOL || "").split("/")[0] : null;
  $("#gamesList").innerHTML =
    `<button class="gx-item${cur === "" ? " on" : ""}" data-game="">All games</button>`
    + groupedGames().map(([label, gs]) => (label ? `<div class="gx-group">${esc(label)}</div>` : "") + gs.map((g) => {
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
  const total = GAMES.reduce((n, g) => n + (g.live && typeof g.live.online === "number" ? g.live.online : 0), 0);
  const card = (g) => {
    const st = gameState(g), live = g.live || {};
    const rooms = Array.isArray(live.rooms) ? live.rooms.length : null;
    const svc = serviceCount(g);
    const line = live.summary || st.why || g.about || "";
    const chips = g.tools.map((t) => `<span class="pill">${esc(t.label)}</span>`).join("");
    return `<button class="panel gx-card" data-game="${esc(g.key)}">`
      + `<span class="gx-head"><span class="dot ${st.cls}" aria-hidden="true"></span>`
      + `<span class="gx-name">${esc(g.title)}<small>${esc(g.platform || "")}</small></span>`
      + `<span class="pill ${st.cls}">${esc(st.word)}</span></span>`
      + `<span class="gx-nums"><span><b>${num(live.online)}</b>online</span>`
      + `<span><b>${num(rooms)}</b>${esc(live.rooms_label || "rooms")}</span>`
      + (svc === null ? "" : `<span><b>${esc(svc === "up" ? "OK" : svc)}</b>services</span>`) + `</span>`
      + `<span class="gx-line">${esc(line)}</span>`
      + (chips ? `<span class="gx-chips">${chips}</span>` : "")
      + `</button>`;
  };
  const cards = groupedGames().map(([label, gs]) => (label ? `<h3 class="gx-grouphead">${esc(label)}</h3>` : "")
    + `<div class="gx-grid">${gs.map(card).join("")}</div>`).join("");
  $("#gamesMain").innerHTML =
    `<div class="gx-top"><h2 class="gx-title">Games</h2>`
    + `<span class="muted">${GAMES.length} on this server, ${total} players online</span></div>`
    + (cards ? cards
      : `<div class="panel muted">No games are set up on this server yet. See docs/admin-modules.md.</div>`);
}

// ---- one game ----
function gameTabs(g, active) {
  const tabs = [`<a href="#games/${esc(g.key)}" class="${active ? "" : "on"}">Overview</a>`];
  for (const t of g.tools) {
    // a page the panel draws itself (PML Preview) lives on its own tab for now
    const href = t.panel ? "#" + t.id : `#games/${g.key}/${t.id}`;
    tabs.push(`<a href="${esc(href)}" class="${active === t.id ? "on" : ""}">${esc(t.label)}</a>`);
  }
  if (canGameLogs(g)) tabs.push(`<a href="#games/${esc(g.key)}/logs" class="${active === "logs" ? "on" : ""}">Logs</a>`);
  return `<nav class="gx-tabs" aria-label="${esc(g.title)}">${tabs.join("")}</nav>`;
}

function gameHeader(g) {
  const st = gameState(g);
  return `<div class="gx-crumb"><a href="#games">Games</a> / ${esc(g.title)}</div>`
    + `<div class="gx-top"><h2 class="gx-title">${esc(g.title)}`
    + ` <span class="pill ${st.cls}"><span class="dot ${st.cls}" aria-hidden="true"></span>${esc(st.word)}</span></h2>`
    + `<span class="muted">${esc([g.platform, g.about].filter(Boolean).join(". "))}</span></div>`;
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
  const live = g.live || { players: [], rooms: [], facts: [] };
  const st = gameState(g);
  const players = live.players || [], rooms = live.rooms || [], facts = live.facts || [];
  const stats = [
    ["Online now", num(live.online), players.length && players.length < (live.online || 0) ? `${players.length} listed` : ""],
    [cap(live.rooms_label || "rooms"), num(rooms.length || (g.live ? 0 : null)), ""],
  ];
  const ops = opsCheck(g.key);
  if (ops) stats.push(["Services", ops.state === "ok" ? "OK" : cap(ops.state),
    ops.restarted ? "last start " + ago(ops.restarted.at * 1000) : ""]);
  if (live.summary) stats.push(["Now", live.summary, ""]);
  const statHtml = stats.map(([k, v, s]) => `<div class="panel gx-stat"><div class="k">${esc(k)}</div>`
    + `<div class="v${String(v).length > 12 ? " long" : ""}">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");

  const playerRows = players.map((p) => `<tr><td class="strong">${esc(p.name)}</td><td>${esc(p.where)}</td>`
    + `<td class="muted">${esc(p.detail)}</td></tr>`).join("");
  const playersHtml = `<section class="panel gx-wide"><h3>Who is playing <span class="muted">${esc(num(live.online))}</span></h3>`
    + (playerRows ? `<div class="tablewrap"><table class="cards"><thead><tr><th>Player</th><th>Where</th><th>Detail</th></tr></thead><tbody>${playerRows}</tbody></table></div>`
      : `<p class="muted">${live.online ? "This game reports a count, not names." : "Nobody right now."}</p>`)
    + `</section>`;

  const roomsHtml = `<section class="panel"><h3>${esc(cap(live.rooms_label || "rooms"))}</h3>`
    + (rooms.length ? `<div class="gx-scroll">` + rooms.map((r) => `<div class="gx-row"><div><div class="strong">${esc(r.name)}</div>`
      + `<div class="muted">${esc(r.detail)}</div></div>${r.state ? `<span class="pill">${esc(r.state)}</span>` : ""}</div>`).join("") + `</div>`
      : `<p class="muted">None right now.</p>`)
    + (facts.length ? `<dl class="gx-facts">${facts.map((f) => `<dt>${esc(f.label)}</dt><dd>${esc(factText(f))}</dd>`).join("")}</dl>` : "")
    + `</section>`;

  const svcs = g.services || [];
  const fix = GAMES_OPS && GAMES_OPS.can_fix && GAMES_OPS.actions && GAMES_OPS.actions[g.key];
  const svcHtml = svcs.length ? `<section class="panel"><h3>Services${fix
      ? ` <button class="sm ghost" data-fix="${esc(g.key)}">Restart ${esc(g.short || g.title)}</button>` : ""}</h3>`
    + (ops ? `<p class="gx-ops ${esc(ops.state)}"><span class="dot ${ops.state === "ok" ? "good" : esc(ops.state)}" aria-hidden="true"></span>${esc(ops.detail)}`
      + (ops.restarted ? ` <span class="muted">Last start ${esc(ago(ops.restarted.at * 1000))}: ${esc(ops.restarted.reason || "")}</span>` : "") + `</p>`
      : `<p class="muted">${!can("health") ? "Server checks need the service status permission."
        : GAMES_OPS ? "The server checker has no check for this game yet. It picks up new games when it is updated."
        : "The server checker is not reporting."}</p>`)
    + svcs.map((s) => `<div class="gx-row"><div><div class="mono">${esc(s.containers.join(", "))}</div>`
      + `<div class="muted">${esc(Object.entries(s.ports || {}).map(([p, w]) => w + " :" + p).join(", "))}</div></div></div>`).join("")
    + `</section>` : "";

  // a field of type "player" is a search box with a pick-list when the game can search its
  // players (picker.js); without that it stays a plain text box
  const field = (f) => f.type === "player" && g.players
    ? `<label>${esc(f.label || f.id)}<span data-pk="${esc(f.id)}"></span></label>`
    : `<label>${esc(f.label || f.id)}<input type="text" name="${esc(f.id)}" autocomplete="off"></label>`;
  const actSig = esc(g.key + ":" + JSON.stringify(g.actions.map((a) => [a.id, a.fields])) + ":" + !!g.players);
  const actHtml = g.actions.length ? `<section class="panel gx-wide"><h3>Actions</h3><div class="gx-acts" data-sig="${actSig}">`
    + g.actions.map((a) => `<form class="gx-act" data-action="${esc(a.id)}">`
      + `<div class="strong">${esc(a.label)}</div>${a.about ? `<div class="muted">${esc(a.about)}</div>` : ""}`
      + (a.fields || []).map(field).join("")
      + `<button type="submit" class="${a.danger ? "danger" : "act"}">${esc(a.label)}</button></form>`).join("")
    + `</div></section>` : "";

  const age = live.at ? ago(live.at * 1000) : "";
  const about = `<section class="panel gx-wide"><h3>About this module</h3><dl class="gx-facts">`
    + `<dt>Live status</dt><dd>${g.live ? esc((live.source || "read") + (age ? ", " + age : "")) : "none: this game reports no live status yet"}</dd>`
    + (live.error ? `<dt>Last problem</dt><dd>${esc(live.error)}</dd>` : "")
    + `<dt>Tools</dt><dd>${g.tools.length ? esc(g.tools.map((t) => t.label).join(", ")) : "none"}${g.tools.some((t) => !t.panel) && !g.has_tools ? " (the game's tool port is not set up)" : ""}</dd>`
    + `<dt>Actions</dt><dd>${g.actions.length ? esc(g.actions.map((a) => a.label).join(", ")) : "none"}</dd>`
    + `<dt>Why it shows</dt><dd>${esc(st.why || "running normally")}</dd>`
    + `</dl></section>`;

  // the status redraw keeps the action forms as they are, with what was typed and picked
  const kept = $("#gamesMain .gx-acts");
  $("#gamesMain").innerHTML = head + `<div class="gx-stats">${statHtml}</div>`
    + `<div class="gx-layout">${gameUpdateBox(g)}${playersHtml}${actHtml}${roomsHtml}${svcHtml}${gamePatchBox(g)}${about}</div>`;
  const fresh = $("#gamesMain .gx-acts");
  if (kept && fresh && kept.dataset.sig === fresh.dataset.sig) fresh.replaceWith(kept);
  else mountGamePickers(g);
}

function mountGamePickers(g) {
  document.querySelectorAll("#gamesMain [data-pk]").forEach((slot) => {
    const pk = makePicker({
      name: slot.dataset.pk, placeholder: "Search players by name or id",
      search: (q, limit) => api(`/api/games/players?game=${encodeURIComponent(g.key)}`
        + `&q=${encodeURIComponent(q)}&limit=${limit}`).then((r) => r.players || []),
    });
    slot.replaceWith(pk.el);
  });
}

// typing or picking in an action form: the status redraw waits
const gamesBusy = () => !!(document.activeElement && document.activeElement.closest
  && document.activeElement.closest("#gamesMain .gx-acts"));

const cap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);

// ---- a game's server logs (the host runner reads them: pol-ops, data/ops/logs) ----
// The owner sees everything; a moderator with the game's serverlogs permission
// sees them with players' addresses masked by the panel.
const canGameLogs = (g) => (g.services || []).length > 0 && (isOwner() || can(`game.${g.key}.serverlogs`));
let LOGS = { key: "", doc: null, shown: 0, filter: "", busy: false, timer: null };

async function renderGameLogs(g, head) {
  if (LOGS.key !== g.key) LOGS = { key: g.key, doc: null, shown: 0, filter: "", busy: false, timer: null };
  $("#gamesMain").innerHTML = head + `<section class="panel"><div class="lg-tools">`
    + `<button class="act sm" id="lgFresh">Fetch the latest</button><span class="muted" id="lgWhen"></span>`
    + `<span class="grow"></span><input type="text" id="lgFilter" autocomplete="off" placeholder="Only lines with..." value="${esc(LOGS.filter)}"></div>`
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
  $("#lgWhen").textContent = LOGS.busy ? "Asking the server..."
    : !LOGS.available ? "The server checker (pol-ops) is not reading logs on this server."
    : d ? `Read ${ago(d.at * 1000)}${d.by ? " for " + d.by : ""}${LOGS.addresses ? "" : ". Players' addresses are hidden."}` : "";
  const ctrs = (d && d.containers) || [];
  $("#lgCtrs").innerHTML = ctrs.length > 1 ? ctrs.map((c, i) => `<button class="cal-chip${i === LOGS.shown ? "" : " off-lite"}" data-lgctr="${i}">`
    + `${esc(c.name)}${c.missing ? " (not found)" : ""}</button>`).join("") : "";
  const c = ctrs[Math.min(LOGS.shown, ctrs.length - 1)];
  if (!c) { $("#lgPre").textContent = LOGS.available ? "No logs read yet." : ""; return; }
  const f = LOGS.filter.toLowerCase();
  const lines = (c.lines || []).filter((l) => !f || l.toLowerCase().includes(f));
  const pre = $("#lgPre");
  pre.textContent = c.missing ? `${c.name} is not on this server.` : lines.join("\n") || (f ? "No line has that." : "Nothing logged.");
  pre.scrollTop = pre.scrollHeight;
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
  });
  $("#gamesMain").addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-action]");
    if (!form) return;
    e.preventDefault();
    const key = (GAME_TOOL || "").split("/")[0];
    const g = GAMES.find((x) => x.key === key);
    const a = g && g.actions.find((x) => x.id === form.dataset.action);
    if (!a) return;
    if (a.danger && !confirm(a.label + "?")) return;
    const params = Object.fromEntries(new FormData(form).entries());
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const r = await api("/api/modules/action", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, action: a.id, params }) });
      toast(r.msg || "Done.");
      form.reset();
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  });
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
const ORIGIN = { "built-in": "OpenLobby", file: "this server's modules folder",
                 game: "the game itself", legacy: "POL_ADMIN_GAMES_EXTRA" };

const PORT_FROM = { env: "the environment", stored: "this panel" };
let ADDED = [];

async function loadModules() {
  // no deployer: the update notices live here, since the Install games tab is hidden
  if (!DEPLOY) await probeDeploy();
  if (!(DEPLOY && DEPLOY.available)) loadUpdates(false);
  else $("#updModules").hidden = true;
  let r;
  try { r = await api("/api/modules?fresh=1"); } catch (e) { toast(e.message, true); return; }
  const live = Object.fromEntries((r.modules || []).map((m) => [m.key, m.live]));
  $("#modulesBody").innerHTML = (r.found || []).map((m) => {
    const st = live[m.key];
    const liveText = !m.live ? "none"
      : !m.shown ? "not read (not shown)"
      : !st ? "nothing yet"
      : st.error ? "problem: " + st.error
      : (st.source || "read") + (typeof st.online === "number" ? ", " + st.online + " online" : "");
    const tools = m.tools ? m.tools + (m.has_tools ? "" : " (port not set up)") : "none";
    return `<tr><td class="strong">${m.shown ? `<a href="#games/${esc(m.key)}">${esc(m.title)}</a>` : esc(m.title)}</td>`
      + `<td class="mono">${esc(m.key)}</td><td>${esc(ORIGIN[m.origin] || m.origin)}</td>`
      + `<td>${esc(PORT_FROM[m.port] || "-")}</td>`
      + `<td>${m.shown ? "yes" : "no"}</td><td>${esc(tools)}${m.actions ? `, ${m.actions} action${m.actions === 1 ? "" : "s"}` : ""}</td>`
      + `<td class="${st && st.error ? "bad" : "muted"}">${esc(liveText)}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">No modules found.</td></tr>`;
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
  const when = ok ? (u.approved_at ? `Approved by ${u.approved_by || "the owner"}, ${ago(u.approved_at * 1000)}` : "Approved")
    : (u.requested_at ? `Sent by its setup program ${ago(u.requested_at * 1000)}` : "Sent by its setup program");
  const noDeployer = !(DEPLOY && DEPLOY.available)
    ? `<span class="muted">No deployer is set up on this server, so it cannot run yet.</span>` : "";
  const chans = u.channels || [];
  return `<tr class="ga-upd"><td colspan="5" data-label=""><div class="ga-upd-box${ok ? " ok" : ""}">`
    + `<div class="ga-upd-head">Updater <span class="pill ${ok ? "live" : "wait"}">${ok ? "Approved" : "Waiting for your approval"}</span>`
    + `<span class="muted">${esc(when)}</span></div>`
    + `<dl class="ga-upd-grid"><dt>Folder</dt><dd><code class="mono">${esc(u.folder)}</code></dd>`
    + `<dt>Command</dt><dd><code class="mono">${esc(u.shown)}</code></dd>`
    + `<dt>Timeout</dt><dd>${esc(u.timeout_minutes)} minutes</dd>`
    + (chans.length ? `<dt>Channels</dt><dd>${esc(chans.map(chanWord).join(", "))}</dd>` : "") + `</dl>`
    + `<div class="ga-upd-acts">${ok
      ? `<button class="ghost sm" data-ga-revoke="${esc(g.key)}">Revoke</button>`
      : `<button class="act sm" data-ga-approve="${esc(g.key)}">Approve</button>`}`
    + `<span class="muted">An approved updater runs with full Docker access.</span>${noDeployer}</div>`
    + (ok && chans.length > 1 && DEPLOY && DEPLOY.available ? gaChanBox(g, u) : "")
    + `</div></td></tr>`;
}

// ---- a paired game's update channel (docs/installs.md, "Update channels"), owner only ----
// The switch runs the approved updater with --channel <name> as a deployer job; the panel stores
// the channel only once that job ended ok. A dev access code typed here goes to the deployer,
// which hands it to the updater in its environment; the field is cleared as soon as it is sent.
const GA_SW = {};          // key -> {job, want, state, summary}: a switch started from this page
let GA_CHAN_FORM = null;   // {key, want}: the switch form that is open
const chanWord = (c) => c.charAt(0).toUpperCase() + c.slice(1);

function gaChanBox(g, u) {
  const key = g.key, on = u.channel || "stable", sw = GA_SW[key];
  const busy = sw && !JOB_DONE.has(sw.state);
  const segs = u.channels.map((c) => `<button type="button" data-ga-chan="${esc(key)}" data-to="${esc(c)}"`
    + `${c === on ? ' class="on" aria-pressed="true"' : ' aria-pressed="false"'}${busy ? " disabled" : ""}>${esc(chanWord(c))}</button>`).join("");
  const when = u.channel_at ? `Switched by ${u.channel_by || "the owner"}, ${ago(u.channel_at * 1000)}`
    : "It has not been switched from this panel.";
  let live = "";
  if (busy) {
    live = `<div class="upd-live"><span class="spin" aria-hidden="true"></span>`
      + `<span>Switching ${esc(g.title)} to ${esc(chanWord(sw.want))}: its updater is running.</span>`
      + ` <button class="ghost sm" data-ga-chanlog="${esc(sw.job)}">Follow the log</button></div>`;
  } else if (sw && sw.state !== "ok") {
    live = `<div class="upd-why bad">Switch to ${esc(chanWord(sw.want))} failed: ${esc(String(sw.summary || "").replace(/^Update failed:\s*/i, "") || "the updater stopped with an error")}.`
      + ` ${esc(g.title)} stays on ${esc(chanWord(on))}. <button class="ghost sm" data-ga-chanlog="${esc(sw.job)}">Show the log</button></div>`;
  }
  const f = GA_CHAN_FORM && GA_CHAN_FORM.key === key ? GA_CHAN_FORM.want : null;
  const form = f ? `<div class="ga-box ga-chan-form"><h3>Switch ${esc(g.title)} to ${esc(chanWord(f))}</h3>`
    + `<p class="hint" style="margin-top:0">The deployer runs the approved updater with <code class="mono">--channel ${esc(f)}</code>. `
    + `${esc(g.title)}'s own setup program switches its images and looks after its database, and refuses a switch it cannot `
    + `do safely. Everyone playing it may be disconnected.</p>`
    + `<div class="row"><div style="flex:1 1 260px"><label for="gaChanCode">Access code, if the developers gave you one</label>`
    + `<input type="password" id="gaChanCode" class="mono" maxlength="2048" autocomplete="off" spellcheck="false" placeholder="ABCD-EFGH-JKMN or olx1..."></div></div>`
    + `<div class="hint">It goes to the deployer and the updater only; it is not stored here and never shown again.</div>`
    + `<div class="ga-result bad" id="gaChanResult" role="alert" hidden></div>`
    + `<div class="toolbar" style="margin-top:12px"><button class="act sm" data-ga-chango="${esc(key)}">Switch to ${esc(chanWord(f))}</button>`
    + `<button class="ghost sm" data-ga-chancancel>Cancel</button></div></div>` : "";
  return `<div class="ga-chan"><span class="ga-chan-lab">Update channel</span>`
    + `<div class="seg" role="group" aria-label="Update channel of ${esc(g.title)}">${segs}</div>`
    + `<span class="muted">${esc(when)}</span></div>${live}${form}`;
}

async function gaChanSwitch(key, want, code) {
  const g = ADDED.find((x) => x.key === key) || { title: key };
  const res = $("#gaChanResult");
  try {
    const r = await api("/api/updates/channel", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(code ? { key, channel: want, code } : { key, channel: want }) });
    GA_CHAN_FORM = null;
    GA_SW[key] = { job: r.job, want, state: "running" };
    toast(`Switching ${g.title} to ${chanWord(want)}: started`);
    renderAdded();
    gaChanFollow(key);
  } catch (e) {
    if (res) { res.hidden = false; res.textContent = e.message; } else toast(e.message, true);
  }
}

function gaChanFollow(key) {
  const sw = GA_SW[key];
  if (!sw || JOB_DONE.has(sw.state)) return;
  setTimeout(async () => {
    let j = null;
    try { j = await api("/api/deploy/job?id=" + encodeURIComponent(sw.job)); } catch (e) { /* restarting: ask again */ }
    if (j && JOB_DONE.has(j.state)) {
      sw.state = j.state; sw.summary = j.summary || "";
      if (j.state === "ok") toast(`${(ADDED.find((x) => x.key === key) || { title: key }).title} is on ${chanWord(sw.want)}`);
      // the panel stores the channel once it has seen the job end: give it a moment
      setTimeout(() => { if (CUR_TAB === "modules") loadModules(); }, 1200);
      if (CUR_TAB === "modules") renderAdded();
      return;
    }
    gaChanFollow(key);
  }, 2000);
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
  $("#dcWhy").innerHTML = kinds.map((k) => `<p${k.key && !k.issuing ? ' class="dc-problem"' : ""}>${esc(k.why || "")}</p>`).join("")
    + (d.problems || []).map((p) => `<p class="dc-problem">${esc(p)}</p>`).join("");
  const sel = $("#dcKind"), was = sel.value;
  const ready = kinds.filter((k) => k.issuing);
  sel.innerHTML = ready.map((k) => `<option value="${esc(k.key)}">${esc(k.label || k.key)}</option>`).join("");
  if (ready.some((k) => k.key === was)) sel.value = was;
  $("#dcKindBox").hidden = ready.length < 2;
  $("#dcMake").disabled = !d.issuing;
  const kindCol = named || codes.some((c) => !c.kind_ok);
  box.querySelectorAll(".dc-kindcol").forEach((el) => { el.hidden = !kindCol; });
  $("#dcBody").innerHTML = codes.map((c) => {
    const [cls, word] = DC_WORD[c.state] || DC_WORD.unused;
    const used = c.redeemed_at ? `${ago(c.redeemed_at * 1000)}<div class="muted mono">${esc(c.install || "")}</div>` : '<span class="muted">-</span>';
    const act = c.state !== "revoked" || c.token_left
      ? `<button class="${c.token_left ? "ghost" : "danger"} sm" data-dc-revoke="${esc(c.code)}">${c.token_left ? "Delete its token again" : "Revoke"}</button>` : "";
    const kind = c.kind_ok ? esc(c.kind_label || c.kind || "")
      : `${esc(c.kind_label || c.kind || "Unnamed")}<div class="muted">kind not configured</div>`;
    return `<tr><td class="strong" data-label="Code"><code class="mono dc-code">${esc(c.code)}</code></td>`
      + (kindCol ? `<td data-label="Kind">${kind}</td>` : "")
      + `<td data-label="For">${esc(c.note || "")}<div class="muted">${esc([c.made_by, c.created_at ? ago(c.created_at * 1000) : ""].filter(Boolean).join(", "))}</div></td>`
      + `<td data-label="State"><span class="pill ${cls}">${esc(word)}</span>${c.token ? ' <span class="muted">own token</span>' : ""}</td>`
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
    $("#dcNew").hidden = false;
    $("#dcNewList").textContent = r.codes.join("\n");
    $("#dcNewHead").textContent = `${r.codes.length} new ${r.kind_label ? r.kind_label + " " : ""}code${r.codes.length === 1 ? "" : "s"} for ${r.note}`;
    loadDevCodes();
  } catch (e) { res.hidden = false; res.textContent = e.message; }
}

async function dcRevoke(code) {
  const c = ((DEVCODES || {}).codes || []).find((x) => x.code === code) || { code };
  const sure = c.token_left ? true : confirm(`Revoke ${code}${c.note ? ` (for ${c.note})` : ""}?\n\n`
    + (c.state === "used" ? "The server that redeemed it can no longer pull the dev builds"
      + (c.token ? "; its registry token is deleted." : " once its access code is changed; with the shared code it keeps working.")
      : "Nobody can redeem it any more."));
  if (!sure) return;
  try {
    const r = await api("/api/devcodes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "revoke", code }) });
    if (r.warning) toast(r.warning, true);
    else toast(`${code} revoked`);
  } catch (e) { toast(e.message, true); }
  loadDevCodes();
}

document.addEventListener("click", (e) => {
  const t = e.target;
  const ch = t.closest("[data-ga-chan]");
  if (ch && !ch.disabled) {
    const key = ch.dataset.gaChan, want = ch.dataset.to;
    const g = ADDED.find((x) => x.key === key);
    const on = ((g || {}).updater || {}).channel || "stable";
    if (want === on) { GA_CHAN_FORM = null; renderAdded(); return; }
    if (want === "stable") {
      if (!confirm(`Switch ${g.title} back to Stable?\n\nThe deployer runs its updater with --channel stable. `
        + `${g.title}'s setup program decides what that means for its data; what changed on another channel may be lost.`)) return;
      return gaChanSwitch(key, want, "");
    }
    GA_CHAN_FORM = { key, want };
    renderAdded();
    const inp = $("#gaChanCode");
    if (inp) inp.focus();
    return;
  }
  if (t.closest("[data-ga-chancancel]")) { GA_CHAN_FORM = null; renderAdded(); return; }
  const go = t.closest("[data-ga-chango]");
  if (go) {
    const inp = $("#gaChanCode");
    const code = inp ? inp.value.trim() : "";
    if (inp) inp.value = "";
    const was = go.textContent;
    go.disabled = true;
    go.textContent = "Starting...";
    return gaChanSwitch(go.dataset.gaChango, GA_CHAN_FORM ? GA_CHAN_FORM.want : "", code).finally(() => {
      go.disabled = false; go.textContent = was;
    });
  }
  const lg = t.closest("[data-ga-chanlog]");
  if (lg) { showTab("deploy"); followJob(lg.dataset.gaChanlog); return; }
  if (t.closest("#dcMake")) return dcMake();
  if (t.closest("#dcCopy")) return copyText($("#dcNewList").textContent, "the codes");
  if (t.closest("#dcNewClose")) { $("#dcNew").hidden = true; return; }
  const rv = t.closest("[data-dc-revoke]");
  if (rv) return dcRevoke(rv.dataset.dcRevoke);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "gaChanCode") {
    e.preventDefault();
    const b = document.querySelector("[data-ga-chango]");
    if (b) b.click();
  }
});

async function gaUpdater(key, approve) {
  const g = ADDED.find((x) => x.key === key);
  const u = g && g.updater;
  if (!u) return;
  const sure = approve
    ? confirm(`This runs ${u.shown} in ${u.folder} with full Docker access when you press Update. Approve?`)
    : confirm(`Revoke the updater of ${g.title}?\n\nThe Update button stops running it until you approve it again.`);
  if (!sure) return;
  try {
    await api("/api/modules/game", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(approve ? { action: "approve_updater", key, id: u.id } : { action: "revoke_updater", key }) });
    toast(approve ? `The updater of ${g.title} is approved` : `The updater of ${g.title} is revoked`);
  } catch (e) { toast(e.message, true); }
  loadModules();
}

function renderAdded() {
  $("#addedBody").innerHTML = ADDED.map((g) => {
    const how = g.how === "paired" && g.paired ? "Paired by its setup program" : "Added by hand";
    const when = g.added_at ? ago(g.added_at * 1000) : "";
    return `<tr${g.updater ? ' class="has-upd"' : ""}><td class="strong">${esc(g.title)}<div class="muted mono">${esc(g.key)}</div></td>`
      + `<td><span class="mono">${esc(g.url)}</span>`
      + `${g.env ? `<div class="patch-why">The environment also sets POL_ADMIN_GAME_${esc(g.key.toUpperCase())}_URL, and it wins.</div>` : ""}</td>`
      + `<td>${esc(how)}<div class="muted">${esc([g.added_by, when].filter(Boolean).join(", "))}</div></td>`
      + `<td>${g.token_set ? "set" : '<span class="muted">none</span>'}</td>`
      + `<td><div class="deploy-acts"><button class="ghost sm" data-ga-edit="${esc(g.key)}">Change</button>`
      + `${g.paired ? `<button class="ghost sm" data-ga-unpair="${esc(g.key)}">Unpair</button>` : ""}`
      + `<button class="danger sm" data-ga-remove="${esc(g.key)}">Remove</button></div></td></tr>`
      + updaterRow(g);
  }).join("") || `<tr><td colspan="5" class="muted">None yet. Games set up in the environment are listed below.</td></tr>`;
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
  $("#gaToken").placeholder = g && g.token_set ? "set: leave empty to keep it" : "";
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
    gaShow(true, `<b>${esc(g.title)}</b> answered${g.platform ? ` (${esc(g.platform)})` : ""}.`
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
    toast(r.added ? `${r.title} added` : `${r.title} saved`);
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
    ? confirm(`Remove ${g.title} from the panel?\n\nIts pages and actions leave the panel. The game itself keeps running`
      + (g.paired ? ", and its setup program can no longer change its token here." : "."))
    : confirm(`Unpair ${g.title}?\n\nIts setup program can no longer tell the panel a new token. The game stays; `
      + "if its token changes, change it here by hand.");
  if (!sure) return;
  try {
    await api("/api/modules/game", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ action, key }) });
    toast(action === "remove" ? `${g.title} removed` : `${g.title} unpaired`);
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
  if (!left) { pairSay("This code has expired. Make a new one."); $("#pairCode").classList.add("muted"); stopPairPoll(); return; }
  $("#pairCode").classList.remove("muted");
  pairSay(`Good for one pairing, for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more.`);
  if (++PAIR.polled % 3) return;          // ask the panel every 3 s whether it was used
  let s;
  try { s = await api("/api/pair/code?code=" + encodeURIComponent(PAIR.code)); } catch (e) { return; }
  if (s.state === "used") {
    stopPairPoll();
    pairSay(`Paired with ${s.title}. It is in the list below.`, true);
    $("#pairCode").classList.add("muted");
    loadModules();
  } else if (s.state === "expired") {
    stopPairPoll();
    pairSay("This code has expired. Make a new one.");
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
  if (!(DEPLOY && DEPLOY.available)) return false;
  return key === "core" || (DEPLOY.games || []).some((g) => g.key === key && g.installed);
}

function updItem(it) {
  const [cls, word] = UPD_WORD[it.update] || UPD_WORD.unknown;
  const run = it.running || {}, lat = it.latest || {};
  const behind = typeof it.behind === "number" && it.update === "available"
    ? `${it.behind} change${it.behind === 1 ? "" : "s"} behind` : "";
  const vers = [
    run.commit ? `Running <code class="mono">${esc(short12(run.commit))}</code>${run.built_at ? ` built ${esc(updDate(run.built_at))}` : ""}` : "",
    lat.commit ? `Newest <code class="mono">${esc(short12(lat.commit))}</code>${lat.date ? ` from ${esc(updDate(lat.date))}` : ""}` : ""]
    .filter(Boolean).map((s) => `<span>${s}</span>`).join("");
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
      : it.via === "game" ? btn("data-upd-apply", "The game updates itself.")
      : it.via === "updater" && DEPLOY && DEPLOY.available ? btn("data-upd-run", "The deployer runs its approved updater.")
      : (it.how ? `<div class="upd-how">${esc(it.how)}</div>` : "");
  } else if (it.update === "unknown" && it.why && it.state !== "failed") {
    tail = `<div class="upd-why">${esc(it.why)}</div>`;
  }
  if (it.state === "failed" && !running) tail = `<div class="upd-why bad">Update failed: ${esc(it.why || "")}</div>` + tail;
  const lastRun = UPD_RUN[it.key];
  if (lastRun && lastRun.state === "failed" && !running) {
    // the deployer ran the game's updater and it failed: the game's last line, and its whole log
    const line = String(lastRun.summary || "").replace(/^Update failed:\s*/i, "");
    tail = `<div class="upd-why bad">Update failed: ${esc(line || "the updater stopped with an error")}`
      + ` <button class="ghost sm" data-upd-log="${esc(lastRun.job)}">Show the log</button></div>` + tail;
  }
  const ch = it.changes || [];
  const li = (c) => `<li><span class="s">${esc(c.subject)}</span><span class="d">${esc(updDate(c.date))}</span></li>`;
  const news = ch.length ? `<details${ch.length <= 3 ? " open" : ""}><summary>What's new (${ch.length})</summary>`
    + `<ul class="upd-changes">${ch.slice(0, 10).map(li).join("")}</ul>`
    + (ch.length > 10 ? `<button class="ghost sm" data-upd-all="${esc(it.key)}" style="margin-top:6px">Show all ${ch.length}</button>` : "")
    + `</details>` : "";
  return `<div class="upd-item" data-upd="${esc(it.key)}"><div class="upd-head"><span class="nm">${esc(it.name || it.key)}</span>`
    + `<span class="pill ${cls}">${esc(word)}</span>${it.channel === "dev" ? '<span class="pill dev">dev build</span>' : ""}`
    + `${behind ? `<span class="muted">${esc(behind)}</span>` : ""}</div>`
    + `${vers ? `<div class="upd-vers">${vers}</div>` : ""}${tail}${news}</div>`;
}

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
    const r = o.running || {};
    const who = r.by ? ` Asked by ${r.by}${r.at ? ", " + ago(r.at * 1000) : ""}.` : "";
    html += `<div class="upd-live"><span class="spin" aria-hidden="true"></span>`
      + `<span>${esc(`${name} is updating.${who} The result shows here when it ends.`)}</span></div>`;
  } else if (it.update !== "current" || o.auto) {
    const label = it.update === "available" ? "Update" : "Update now";
    const how = o.auto && it.update !== "available" ? "Runs its update now instead of at the next check."
      : "Runs its update on the server now.";
    html += `<div class="upd-act"><button class="act sm" data-upd-ops="${esc(it.key)}">${label}</button>`
      + `<span class="muted">${esc(how)}</span></div>`;
  }
  if (o.on && o.off) {
    const checking = o.service && o.service.state === "activating";
    const state = o.auto ? (checking ? "Automatic, checking now" : o.next ? `Automatic, next check ${inMin(o.next)}` : "Automatic")
      : "When you press Update";
    const asked = OPS_ASKED[it.key] && OPS_ASKED[it.key].what !== "update";
    html += `<div class="upd-auto"><span>Updates:</span>`
      + `<div class="seg" role="group" aria-label="When ${esc(name)} updates">`
      + `<button data-upd-auto="${esc(it.key)}" data-on="1" class="${o.auto ? "on" : ""}" aria-pressed="${o.auto}"${asked ? " disabled" : ""}>Automatic (deploys within minutes of a push)</button>`
      + `<button data-upd-auto="${esc(it.key)}" data-on="0" class="${o.auto ? "" : "on"}" aria-pressed="${!o.auto}"${asked ? " disabled" : ""}>When I press Update</button></div>`
      + `<span class="muted">${esc(asked ? "Changing..." : state)}</span></div>`;
  }
  if (o.last && !busy) {
    const l = o.last;
    html += `<div class="upd-why${l.ok ? "" : " bad"}">${esc(`Last update from the panel${l.by ? " (" + l.by + ")" : ""}`
      + `${l.at ? " " + ago(l.at * 1000) : ""}: ${l.ok ? "finished" : "failed"}. ${l.result || ""}`)}</div>`
      + (l.log && l.log.length ? `<details class="upd-log"><summary>Show the log</summary><pre>${esc(l.log.join("\n"))}</pre></details>` : "");
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
      toast(`${it.name}: update ${o.last.ok ? "finished" : "failed"}${o.last.result ? ". " + o.last.result : ""}`, !o.last.ok);
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
  if (what === "update" && !confirm(`Update ${name} now?\n\nIts update runs on the server. When there is a new version `
    + `it goes live and ${name} restarts, and everyone playing it is disconnected.`)) return;
  if (what === "auto-on" && !confirm(`Update ${name} automatically?\n\nPushes to main go live within minutes and restart the game.`)) return;
  try {
    const r = await api("/api/updates/ops", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key, what }) });
    toast(r.message || "Asked.");
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
      if (r.done) { toast(`${st.name} is up to date`); return applyEnded(key); }
      if (r.state === "failed") { toast(`${st.name}: update failed: ${r.why || ""}`, true); return applyEnded(key); }
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
  if (!confirm(`Everyone playing ${name} is disconnected while it updates.`)) return;
  let r;
  try {
    r = await api("/api/updates/apply", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ key }) });
  } catch (e) { toast(e.message, true); return; }
  toast(r.msg || `${name} is updating`);
  APPLY[key] = { name, message: "" };
  applyFollow(key, name);
  if (UPD && (CUR_TAB === "deploy" || CUR_TAB === "modules")) renderUpdates();
  if (CUR_TAB === "games") renderGame(key);
}

async function updaterStart(key) {
  const it = ((UPD || {}).items || []).find((x) => x.key === key) || { name: key };
  const name = it.name || key;
  if (!confirm(`Update ${name}?\n\nThe deployer runs the updater you approved for ${name}, with full Docker access. `
    + "Everyone playing it may be disconnected.")) return;
  try {
    const r = await api("/api/updates/run", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ key }) });
    toast(`Update ${name}: started`);
    UPD_RUN[key] = { job: r.job, state: "running" };
    await probeDeploy(); renderDeploy();
    followJob(r.job);
    const panel = $("#deployJobPanel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
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
    const n = typeof u.behind === "number" ? `${u.behind} change${u.behind === 1 ? "" : "s"} behind. ` : "";
    body = `<div class="upd-act"><button class="act sm" data-upd-apply="${esc(g.key)}">Update</button>`
      + `<span class="muted">${esc(n)}The game updates itself.</span></div>`;
  } else {
    body = `<p class="muted">${u.update === "current" ? "Up to date." : esc(u.why || "It cannot tell whether it is up to date.")}</p>`;
  }
  if (u.state === "failed" && !running) body = `<div class="upd-why bad">Update failed: ${esc(u.why || "")}</div>` + body;
  return `<section class="panel gx-wide gx-upd"><h3>Update</h3>${body}</section>`;
}

function renderUpdates() {
  const box = DEPLOY && DEPLOY.available ? $("#updDeploy") : $("#updModules");
  const other = box.id === "updDeploy" ? $("#updModules") : $("#updDeploy");
  other.hidden = true;
  const r = UPD || {};
  const items = r.items || [];
  box.hidden = false;
  const when = r.checking ? "checking now..." : (r.checked_at ? "checked " + ago(Date.parse(r.checked_at)) : "");
  box.innerHTML = `<h2>Updates <span class="h2sub">${esc(when)}</span>`
    + `${r.channel === "dev" ? ' <span class="pill dev">dev channel</span>' : ""}</h2>`
    + `<div class="toolbar"><button class="ghost sm" data-upd-check${r.checking ? " disabled" : ""}>Check now</button></div>`
    + `<div class="upd-list">${items.map(updItem).join("") || '<p class="hint">Nothing reports its version yet.</p>'}</div>`
    // no deployer: say how to get it, since Install games (and the dev access code box) is hidden
    + (box.id === "updModules" ? `<p class="hint">Installing games from here, the Update buttons and the `
      + `developers' builds (with an access code) need the deployer, which this install does not run. `
      + `To add it, open OpenLobby Setup and pick Add the deployer; it asks for an access code if you `
      + `have one. Installed with the setup script instead: run it again from the OpenLobby folder with `
      + `<span class="mono">-Deployer</span> (<span class="mono">--deployer</span>). Then reload this page: `
      + `Settings gets an Install games tab.</p>` : "");
  // an update a game applies itself, started here or before a reload: follow it
  for (const it of items) if ((it.state === "updating" || it.applying) && !APPLY[it.key]) applyFollow(it.key, it.name);
}

async function loadUpdates(refresh) {
  if (!isOwner()) return;
  clearTimeout(UPD_TIMER);
  try { UPD = await api("/api/updates" + (refresh ? "?refresh=1" : "")); }
  catch (e) { toast(e.message, true); return; }
  const opsBusy = opsFollow(UPD.items || []);
  if (CUR_TAB === "deploy" || CUR_TAB === "modules") renderUpdates();
  // while a check runs, ask again every 3 s; while the ops runner works on a game, every 5 s
  if (UPD.checking && ["deploy", "modules", "overview"].includes(CUR_TAB))
    UPD_TIMER = setTimeout(() => loadUpdates(false), 3000);
  else if (opsBusy && ["deploy", "modules"].includes(CUR_TAB))
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
  const go = DEPLOY && DEPLOY.available ? "deploy" : "modules";
  ul.querySelectorAll("li.empty").forEach((li) => li.remove());
  ul.insertAdjacentHTML("afterbegin", `<li class="upd-note" data-go="${go}"><span class="t1"><b>Updates available:</b> `
    + `${esc(avail.map((it) => it.name || it.key).join(", "))}</span><span class="t2">${go === "deploy" ? "Install games" : "Settings, Modules"}</span></li>`);
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
    const li = (c) => `<li><span class="s">${esc(c.subject)}</span><span class="d">${esc(updDate(c.date))}</span></li>`;
    all.previousElementSibling.innerHTML = (it.changes || []).map(li).join("");
    all.remove();
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
  const it = ((UPD || {}).items || []).find((x) => x.key === key) || { name: key };
  const name = it.name || key;
  const sure = key === "core"
    ? confirm(`Update ${name}?\n\nThe deployer fetches the new version and restarts the core services, this panel among them. `
      + "The panel goes away for a minute or two and comes back by itself; reload the page if it does not.")
    : confirm(`Update ${name}?\n\nThe deployer fetches the new version and restarts ${name}. Everyone playing it is disconnected.`);
  if (!sure) return;
  try {
    const r = await api("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ action: "update", game: key }) });
    toast(`Update ${name}: started`);
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
  $("#deployTab").hidden = !(DEPLOY && DEPLOY.available);
}

function deployState(g) {
  if (!g.installed) return ["off", "Not installed", ["install"]];
  if (!g.enabled) return ["off", "Installed, off", ["enable", "update", "check"]];
  if (g.running === false) return ["bad", "On, not running", ["restart", "update", "disable", "check"]];
  return ["good", g.running ? "Running" : "On", ["restart", "update", "disable", "check"]];
}
const ACT_WORD = { install: "Install", enable: "Turn on", disable: "Turn off", update: "Update",
                   restart: "Restart", check: "Check", updater: "Update" };

function renderDeploy() {
  const d = DEPLOY || {};
  $("#deployOff").hidden = !!d.available;
  $("#deployPanel").hidden = !d.available;
  $("#deployJobsPanel").hidden = !d.available;
  if (!d.available) {
    $("#deployWhy").textContent = "No deployer is answering (" + (d.why || "not set up") + "). It is an "
      + "opt-in service: see docs/deployer.md to run it, then set POL_DEPLOYER_URL and POL_DEPLOYER_TOKEN "
      + "for the admin service.";
    return;
  }
  // the deployer found that Docker sees the checkouts elsewhere: it runs no compose command
  const problem = (d.options || {}).problem || "";
  $("#deployProblem").hidden = !problem;
  $("#deployProblem").textContent = problem;
  const busy = (d.jobs || []).some((j) => !JOB_DONE.has(j.state));
  const rows = [{ key: "core", name: "OpenLobby (this server)", installed: true, enabled: true,
                  running: (d.core || {}).running, missing: [], admin_env: [], core: true }]
    .concat(d.games || []);
  $("#deployBody").innerHTML = rows.map((g) => {
    const [cls, word, acts] = g.core ? ["good", "Running", ["update", "restart", "check"]] : deployState(g);
    const needs = (g.missing || []).map((m) => `<div><span class="mono">${esc(m.path)}</span>`
      + (m.required === false ? ' <span class="muted">(optional)</span>' : "")
      + `${m.hint ? ` <span class="muted">${esc(m.hint)}</span>` : ""}</div>`).join("")
      + (g.installed && g.enabled && (g.admin_env || []).length
        ? `<div class="muted">For its admin page set ${(g.admin_env || []).map((e) => `<span class="mono">${esc(e)}</span>`).join(" and ")}.</div>` : "")
      + (g.notes ? `<div class="muted">${esc(g.notes)}</div>` : "")
      + [...new Set((g.missing || []).filter((m) => m.import && m.item).map((m) => m.item))]
        .map((it) => `<div><button class="ghost sm" data-import="${esc(g.key)}" data-item="${esc(it)}"`
          + `${busy ? " disabled" : ""}>Import ${esc(it)} from a client...</button></div>`).join("");
    return `<tr><td class="strong">${esc(g.name || g.key)}<div class="muted mono">${esc(g.key)}</div></td>`
      + `<td><span class="pill ${cls === "good" ? "live" : cls === "bad" ? "off" : "env"}">${esc(word)}</span></td>`
      + `<td>${needs || '<span class="muted">-</span>'}</td>`
      + `<td><div class="deploy-acts">${acts.map((a) => `<button class="${a === "disable" ? "danger" : "ghost"} sm" `
        + `data-deploy="${esc(a)}" data-game="${esc(g.key)}"${busy ? " disabled" : ""}>${esc(ACT_WORD[a])}</button>`).join("")}</div></td></tr>`;
  }).join("");
  $("#deployJobs").innerHTML = (d.jobs || []).map((j) =>
    `<li data-job="${esc(j.id)}"><span class="t1">${j.action === "channel" ? (j.key ? `Update channel: ${esc(j.key)} to ${j.to === "dev" ? "Dev" : "Stable"}` : "Update channel")
      : `${esc(ACT_WORD[j.action] || j.action)} ${esc(j.title || j.game)}`}: `
    + `${esc(JOB_WORD[j.state] || j.state)}${j.summary ? " - " + esc(j.summary) : ""}</span>`
    + `<span class="t2">${esc(jobWhen(j))}</span></li>`).join("")
    || '<li class="empty">No jobs yet.</li>';
  if (CHAN) renderChannel();
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
  $("#chanFormHead").textContent = again ? "Enter a new access code" : "Unlock the dev builds";
  if (open) $("#chanCode").focus();
}

// every row of the list: the deployer's keys, then the paired games
function chanRows(c) {
  const imgs = c.images || [];
  const rows = (c.keys || []).map((k) => ({
    kind: "key", key: k.key, name: k.key === "core" ? "OpenLobby (this server)" : k.name, sub: k.key === "core" ? k.name : "",
    on: k.on, since: k.since, enabled: k.enabled, applies: k.applies, note: k.note, problem: k.problem,
    would: k.would || [], back: k.back, services: k.services || [],
    images: imgs.filter((r) => r.key === k.key && r.dev_name)
  }));
  for (const p of c.paired || []) {
    rows.push({ kind: "paired", key: p.key, name: p.title, sub: "Paired game", on: p.on === "dev" ? "dev" : "stable",
                other: p.on !== "dev" && p.on !== "stable" ? p.on : null, note: p.note, problem: null,
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
    $("#chanHint").textContent = old ? "This deployer switches everything at once; update the core to switch each server on its own."
      : (c.why || "The deployer did not say what it follows.");
    ["#chanBar", "#chanWarn", "#chanForm", "#chanGaps", "#chanSource", "#chanAdv"].forEach((s) => { $(s).hidden = true; });
    $("#chanBody").innerHTML = "";
    return;
  }
  const rows = chanRows(c);
  const onDev = rows.filter((r) => r.on === "dev");
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
  const since = c.since ? updDate(c.since) : "";
  $("#chanHint").textContent = c.unlocked
    ? `The dev builds are unlocked${since ? " since " + since : ""}. Pick Dev for each server that should follow them; `
      + "anything left on Stable keeps the published images."
    : "Everything follows the published releases. With an access code from the developers you can let each server "
      + "follow their preview builds on its own.";
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
    if (r.kind === "paired") build = '<span class="muted">Its own setup program switches it.</span>';
    else {
      build = r.images.map((x) => (x.applies ? img(x.dev_name) : `${img(x.dev_name)}<span class="chan-what">${esc(x.note || "")}</span>`)
        + (x.what ? `<span class="chan-what">${esc(x.what)}</span>` : "")).join("<br>");
      if (!r.enabled) build += '<span class="chan-what">Not turned on: it follows its switch once it is.</span>';
    }
    const lockedOut = r.kind === "key" && !c.unlocked && !dev;
    const seg = ["stable", "dev"].map((to) => {
      const here = r.on === to;
      const off = busy || (to === "dev" && lockedOut && !here);
      return `<button type="button" data-ck="${esc(r.key)}" data-kind="${r.kind}" data-to="${to}"`
        + `${here ? ' class="on" aria-pressed="true"' : ' aria-pressed="false"'}${off ? " disabled" : ""}`
        + `${to === "dev" && lockedOut ? ' title="Enter an access code first"' : ""}>${to === "dev" ? "Dev" : "Stable"}</button>`;
    }).join("");
    const notes = (r.problem ? `<span class="chan-note bad">${esc(r.problem)}</span>` : "")
      + (r.note ? `<span class="chan-note warn">${esc(r.note)}</span>` : "")
      + (r.other ? `<span class="chan-note">On its ${esc(r.other)} channel (Settings, Modules).</span>` : "");
    return `<tr><td class="strong" data-label="Server"><div>${esc(r.name)}${r.sub ? `<div class="muted">${esc(r.sub)}</div>` : ""}${notes}</div></td>`
      + `<td data-label="Dev build"><div>${build || '<span class="muted">-</span>'}</div></td>`
      + `<td data-label="Follows"><div><div class="seg" role="group" aria-label="${esc(r.name)} follows">${seg}</div>`
      + `${dev && r.since ? `<span class="chan-when">On Dev since ${esc(updDate(r.since))}</span>` : ""}</div></td>`
      + (anyDev ? `<td data-label="Source"><div>${dev && srcLinks(r) ? srcLinks(r) : '<span class="muted">-</span>'}</div></td>` : "")
      + "</tr>";
  }).join("") || `<tr><td colspan="${anyDev ? 4 : 3}" class="muted">Nothing installed here has a dev build yet.</td></tr>`;
  const gapNames = [...new Set((c.images || []).filter((r) => !r.dev_name).map((r) => r.stable.split("/").pop()))];
  $("#chanGaps").hidden = !gapNames.length;
  $("#chanGaps").innerHTML = "No dev build yet, so these stay on the published images: "
    + gapNames.map((n) => `<code class="mono">${esc(n)}</code>`).join(", ") + ".";
  const anySrc = rows.some((r) => r.on === "dev" && r.images.some((x) => x.has_source));
  $("#chanSource").hidden = !anySrc;
  $("#chanSource").textContent = "Each dev build's source code is at its link, under the licence the link names.";
  renderIssuer(c);
}

const ISSUER_FROM = {
  env: "Set by POL_DEV_ISSUER in this server's environment, so it cannot be changed here.",
  state: "Set here.",
  default: "The project's own issuer, the default."
};

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
  $("#chanIssuerHint").textContent = `${ISSUER_FROM[iss.from] || ""} Short codes (ABCD-EFGH-JKMN) are redeemed there for `
    + `this server; type off to turn them off. ${iss.value === "off" ? "Short codes are off." : ""}`.trim();
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
    await chanPost({ unlock: code }, "Checking the access code: started");
    chanForm(false);
    return true;
  } catch (err) {
    $("#chanResult").hidden = false;
    $("#chanResult").textContent = err.message;
    return false;
  }
}

function chanConfirm(r, to) {
  const word = to === "dev" ? "Dev" : "Stable";
  let text = `Switch ${r.name} to ${word}?\n\n`;
  if (r.kind === "paired") text += `The deployer runs its approved updater with --channel ${to}; its own setup program `
    + "switches its images and looks after its data. Everyone playing it may be disconnected.";
  else if (!r.enabled) text += "It is not turned on, so nothing is restarted now; it follows this once it is.";
  else text += `The deployer pulls its ${to === "dev" ? "dev builds" : "published images"} and restarts only its services `
    + `(${r.services.join(", ")}). Everyone playing it may be disconnected.`;
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
        const j = await api("/api/updates/channel", { method: "POST", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify({ key: r.key, channel: to }) });
        toast(`Switching ${r.name} to ${to === "dev" ? "Dev" : "Stable"}: started`);
        await probeDeploy(); renderDeploy(); followJob(j.job);
      } else {
        await chanPost({ key: r.key, to }, `Switching ${r.name} to ${to === "dev" ? "Dev" : "Stable"}: started`);
      }
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.closest("#chanUnlock")) return chanForm($("#chanForm").hidden, false);
  if (t.closest("#chanNewCode")) return chanForm($("#chanForm").hidden, true);
  if (t.closest("#chanCancel")) return chanForm(false);
  if (t.closest("#chanLock")) {
    if (!confirm("Forget the access code?\n\nThe deployer signs out of the dev builds and forgets the code; "
      + "entering one again unlocks them again.")) return;
    try { await chanPost({ lock: true }, "Forgetting the access code: started"); } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.closest("#chanIssuerSave") || t.closest("#chanIssuerDefault")) {
    const value = t.closest("#chanIssuerDefault") ? "" : $("#chanIssuer").value.trim();
    const res = $("#chanIssuerResult");
    res.hidden = true;
    try {
      await chanPost({ issuer: value }, value ? "Issuer of short codes saved" : "Issuer of short codes: the default");
      loadChannel();
    } catch (err) { res.hidden = false; res.textContent = err.message; }
    return;
  }
  if (t.closest("#chanGo")) {
    const code = $("#chanCode").value.trim();
    $("#chanCode").value = "";
    if (!code) { $("#chanResult").hidden = false; $("#chanResult").textContent = "Type the access code."; return; }
    const btn = t.closest("#chanGo");
    btn.disabled = true;
    $("#chanResult").hidden = true;
    btn.textContent = "Checking the code...";
    await chanUnlock(code);
    btn.textContent = "Unlock";
    btn.disabled = false;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "chanCode") {
    e.preventDefault();
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
                     patch_remove: "Remove bundle", patch_check: "Check bundles", patch_fetch: "Get from the developers" };
Object.assign(ACT_WORD, PATCH_WORD);

async function loadPatches(recheck) {
  if (!DEPLOY || !DEPLOY.available) { $("#patchPanel").hidden = true; return; }
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
    return `<tr><td class="strong">${esc(b.title || key)}<div class="muted mono">${esc(key)}${(b.aliases || []).length ? " (also " + esc(b.aliases.join(", ")) + ")" : ""}</div>`
      + `${b.error ? `<div class="patch-why">${esc(b.error)}</div>` : ""}</td>`
      + `<td><span class="mono">${esc(b.latest || "-")}</span><div class="muted"><a href="#" data-prov="${esc(key)}">${esc((b.versions || []).length)} version${(b.versions || []).length === 1 ? "" : "s"}</a></div></td>`
      + `<td>${b.served ? '<span class="pill live">Yes</span>' : '<span class="pill off">No</span>'}</td>`
      + `<td>${sc}${b.selfcheck_at ? `<div class="muted">${esc(jobWhen({ started: b.selfcheck_at }))}</div>` : ""}`
      + `${b.selfcheck_why ? `<div class="patch-why">${esc(b.selfcheck_why)}</div>` : ""}</td>`
      + `<td>${esc(impBytes(b.size))}</td>`
      + `<td><div class="deploy-acts">${older.length ? `<select data-rollto="${esc(key)}"><option value="">Roll back to...</option>`
        + older.slice().reverse().map((v) => `<option>${esc(v)}</option>`).join("") + `</select>` : ""}`
      + `<button class="danger sm" data-patchrm="${esc(key)}"${busy ? " disabled" : ""}>Remove</button></div></td></tr>`
      + `<tr class="prov-row" data-provrow="${esc(key)}" hidden><td colspan="6"></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">No bundles in the mirrors folder yet.</td></tr>';
  PROV = null;
}

// ---- where each version of a bundle comes from (GET /api/deploy/patches/provenance) ----
let PROV = null;
const PROV_WORD = { retail: ["Retail", "live"], ours: ["Ours", "wait"], "our-label": ["Retail files, our number", "env"],
                    unknown: ["Unknown", "off"], disc: ["Disc version", "env"], missing: ["Not here", "off"] };
// newest first: the date, then the suffix, shorter first (a same-day _9 before _10)
const provKey = (v) => [v.slice(0, 8), String(v.length - 9).padStart(2, "0"), v.slice(9)].join("|");

function provHtml(p) {
  if (p.error) return `<div class="patch-why">${esc(p.error)}</div>`;
  const row = (r) => {
    const [word, pill] = PROV_WORD[r.origin] || [r.origin, "env"];
    return `<tr><td class="mono">${esc(r.version)}</td><td><span class="pill ${pill}">${esc(word)}</span></td>`
      + `<td class="muted">${esc(r.why || (r.origin === "ours" ? "published here; not a Square Enix release" : r.origin === "unknown" ? "nothing on record says where it came from" : ""))}</td></tr>`;
  };
  const held = (p.versions || []).slice().reverse();
  const lost = (p.missing || []).slice().reverse();
  return `<div class="muted" style="margin:2px 0 6px">${p.final_list ? "Square Enix's final list for this is known." : "Square Enix's full list for this is not known; the missing list may be short."}`
    + `${p.note ? " " + esc(p.note) : ""}</div>`
    + `<table class="cards"><thead><tr><th>Version</th><th>What it is</th><th>Known from</th></tr></thead><tbody>`
    + [].concat(held, lost.map((r) => Object.assign({}, r, { origin: "missing" })), p.discs || [])
      .sort((x, y) => (provKey(y.version) < provKey(x.version) ? -1 : provKey(y.version) > provKey(x.version) ? 1 : 0))
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
  if (GAME_SHOWN === key && $("#gamesMain .gx-layout")) renderGame(key);
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
    if (b.error) return `<tr><td colspan="4"><b>${esc(id)}</b> <span class="patch-why">${esc(b.error)}</span></td></tr>`;
    const n = {};
    (b.versions || []).forEach((r) => { n[r.origin] = (n[r.origin] || 0) + 1; });
    const here = Object.entries(n).map(([o, k]) => `${k} ${word(o)[0].toLowerCase()}`).join(", ");
    const top = (b.versions || []).find((r) => r.version === b.latest);
    const where = many ? b.title : (String(b.title || "").match(/\(([^)]*)\)\s*$/) || [, b.title])[1];
    const lost = (b.missing || []).length;
    return `<tr class="gp-row" data-gp="${esc(g.key + ":" + id)}" tabindex="0" aria-expanded="${open}">`
      + `<td><span class="gp-caret" aria-hidden="true">${open ? "▾" : "▸"}</span> <b>${esc(where)}</b> <span class="muted mono">${esc(id)}</span></td>`
      + `<td>${b.latest ? `<span class="mono">${esc(b.latest)}</span> ${top ? `<span class="pill ${word(top.origin)[1]}">${esc(word(top.origin)[0])}</span>` : ""}` : '<span class="muted">nothing</span>'}</td>`
      + `<td>${(b.versions || []).length} <span class="muted">(${esc(here || "none")})</span></td>`
      + `<td>${lost ? `<span class="pill off">${lost} not here</span>` : `<span class="muted">none known</span>`}`
      + `${b.final_list ? "" : ' <span class="muted">· full list unknown</span>'}</td></tr>`
      + `<tr data-gpbody="${esc(g.key + ":" + id)}"${open ? "" : " hidden"}><td colspan="4">${open ? provHtml(b) : ""}</td></tr>`;
  }).join("");
  const from = c.data.source === "deployer" ? "the deployer" : "the patch versions service";
  return `<section class="panel gx-wide gx-patches"><h3>Patches <span class="muted">${bs.length} bundle${bs.length === 1 ? "" : "s"}</span></h3>`
    + `<p class="muted">What the patch service hands out for ${esc(g.title)}, by region. "Serving" is the version it`
    + ` gives as newest. Open a row for every version: retail (Square Enix's), ours, or unknown, and the retail`
    + ` versions known to exist that are not here. Read from ${from}${c.data.at ? ", " + esc(c.data.at) : ""}.</p>`
    + `<div class="tablewrap"><table class="cards"><thead><tr><th>Bundle</th><th>Serving</th><th>Versions here</th><th>Retail not here</th></tr></thead>`
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
    tr.firstElementChild.innerHTML = p ? provHtml(p) : '<span class="muted">Nothing on record for this bundle.</span>';
  } catch (x) { tr.firstElementChild.innerHTML = `<span class="patch-why">${esc(x.message)}</span>`; }
  tr.hidden = false;
});

// ---- Install games > From the developers (the dev channel's patch feed) ----
let FEED = null;
const FEED_STATE = { new: ["Not here", "env"], behind: ["Behind", "wait"], current: ["Up to date", "live"],
                     ahead: ["Newer here", "live"], custom: ["Own versions", "off"], unavailable: ["Cannot get", "off"] };

async function loadFeed(refresh) {
  const panel = $("#feedPanel");
  if (!DEPLOY || !DEPLOY.available) { panel.hidden = true; return; }
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
  panel.hidden = false;
  $("#feedPill").textContent = r.feed ? "On" : "Off";
  $("#feedPill").className = "pill " + (r.feed ? "live" : "env");
  $("#feedOff").hidden = !!r.feed;
  $("#feedOnBlock").hidden = !r.feed;
  // a kept code (or the dev channel's, when it opens the patches) needs nothing typed
  $("#feedCode").hidden = !!r.can_enable;
  $("#feedHint").hidden = !!r.feed || !r.from_channel;
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
  }).join("") || '<tr><td colspan="5" class="muted">The developers have no patches up yet.</td></tr>';
}

async function patchAct(body, sure) {
  if (sure === false) return;
  try {
    const r = await api("/api/deploy/patch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${PATCH_WORD[body.action]}: started`);
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
    const body = { enabled: on };
    if (on && !$("#feedCode").hidden) body.code = $("#feedCode").value.trim();
    return (async () => {
      try {
        await api("/api/deploy/patches/feed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        $("#feedCode").value = "";
        toast(on ? "Patches from the developers are on" : "Patches from the developers are off");
      } catch (x) { toast(x.message, true); }
      loadFeed();
    })();
  }
  const get = e.target.closest("[data-feedget]");
  if (get) {
    const b = (FEED.bundles || []).find((x) => `${x.region}-${x.product}` === get.dataset.feedget);
    return patchAct({ action: "patch_fetch", region: b.region, product: b.product },
      confirm(`${b.status === "new" ? "Get" : "Update"} ${b.title || get.dataset.feedget} from the developers?\n\n`
        + `This downloads ${impBytes(b.size)} and brings it to ${b.latest}. Players who are updating `
        + `are let finish before the patch service restarts.`));
  }
  const rm = e.target.closest("[data-patchrm]");
  if (rm) {
    const b = (PATCHES.bundles || []).find((x) => `${x.region}-${x.product}` === rm.dataset.patchrm);
    const name = b.title || rm.dataset.patchrm;
    const typed = prompt(`Remove ${name} from the patch service? Its files go to the deployer's trash folder, `
      + `and clients can no longer update it. Type ${rm.dataset.patchrm} to confirm.`);
    return patchAct({ action: "patch_remove", region: b.region, product: b.product }, typed === rm.dataset.patchrm ? true : (typed !== null && toast("Not removed: that did not match.", true), false));
  }
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-rollto]");
  if (!sel || !sel.value) return;
  const b = (PATCHES.bundles || []).find((x) => `${x.region}-${x.product}` === sel.dataset.rollto);
  const to = sel.value;
  sel.value = "";
  const gone = (b.versions || []).slice((b.versions || []).indexOf(to) + 1);
  patchAct({ action: "patch_rollback", region: b.region, product: b.product, to },
    confirm(`Roll ${b.title || sel.dataset.rollto} back to ${to}?\n\nThis takes out ${gone.length === 1 ? "version" : "versions"} `
      + `${gone.join(", ")}. Clients on ${gone.length === 1 ? "it" : "them"} are told to update to ${to}. `
      + `The files go to the deployer's trash folder.`));
});

async function loadDeploy() {
  await probeDeploy();
  renderDeploy();
  loadPatches();
  loadFeed();
  if (!DEPLOY || !DEPLOY.available) { renderChannel(); return; }
  loadChannel();
  loadUpdates(false);
  const live = (DEPLOY.jobs || []).find((j) => !JOB_DONE.has(j.state));
  if (live && live.id !== DEPLOY_JOB) followJob(live.id);
  else if (!live && !DEPLOY_JOB && (DEPLOY.jobs || [])[0]) showJob(DEPLOY.jobs[0].id);
}

async function showJob(id) {
  let j;
  try { j = await api("/api/deploy/job?id=" + encodeURIComponent(id)); } catch (e) { return; }
  DEPLOY_JOB = id;
  $("#deployJobPanel").hidden = false;
  $("#deployJobTitle").textContent = `${ACT_WORD[j.action] || j.action} ${j.title || j.game}`;
  $("#deployJobState").textContent = JOB_WORD[j.state] || j.state;
  $("#deployJobSummary").textContent = j.summary || "";
  // new client data reaches a game that is already on only when it is turned on again
  const jg = ((DEPLOY || {}).games || []).find((g) => g.key === j.game);
  if (j.action === "import" && j.state === "ok" && jg && jg.enabled) {
    $("#deployJobSummary").insertAdjacentHTML("beforeend", ` <span>${esc(jg.name || jg.key)} is on: turn it on again to use the new data.</span>`
      + ` <button class="act sm" data-deploy="enable" data-game="${esc(jg.key)}">Turn on again</button>`);
  }
  $("#deployLog").textContent = j.log || "";
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
      pre.textContent += r.text;
      pre.scrollTop = pre.scrollHeight;
    }
    // the deployer says where the text it sent started; the next read follows on from it
    DEPLOY_OFFSET = typeof r.next === "number" ? r.next
      : DEPLOY_OFFSET + new TextEncoder().encode(r.text || "").length;
    if (JOB_DONE.has(r.state)) {
      stopDeployPoll();
      const done = await showJob(id);
      await probeDeploy();
      renderDeploy();
      if (j.action === "channel") { loadChannel(); loadUpdates(false); }
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
      + `<span class="t1">Use this folder</span><span class="t2">${(r.files || []).length === 1 ? "1 file" : esc((r.files || []).length) + " files"} here</span></li>`)
    + (r.dirs || []).map((d) => `<li data-dir="${esc((IMP.path ? IMP.path + "/" : "") + d)}"><span class="t1">${esc(d)}/</span></li>`).join("")
    + (IMP.mode === "patch_import" ? (r.files || []).filter((f) => /\.polpatch$/i.test(f.name)).map((f) => {
        const rel = (IMP.path ? IMP.path + "/" : "") + f.name, pp = f.polpatch || {};
        return `<li data-key="${esc(JSON.stringify({ path: rel }))}" data-usefile="${esc(rel)}" class="${pp.error ? "" : "match"}">`
          + `<span class="t1">${esc(f.name)}<span class="muted"> ${esc(pp.error || ((pp.title || "") + " " + (pp.versions || []).join(", ")))}</span></span>`
          + `<span class="t2">${esc(impBytes(f.size))}</span></li>`;
      }).join("") : "")
    + (r.truncated ? '<li class="empty">More folders than shown.</li>' : "");
  if (!impPatchMode() && impMatches(r.matches)) impChoose({ path: IMP.path }, "/" + IMP.path);
}

// the same dialog imports a .polpatch or builds one from an install
async function openPatchDialog(mode) {
  IMP = { mode, game: null, item: null, path: "", source: null };
  $("#impVerb").textContent = mode === "patch_import" ? "Import" : "Build";
  $("#impWhat").textContent = mode === "patch_import" ? "a patch bundle (.polpatch)" : "a patch bundle from a client install";
  $("#impHint").textContent = mode === "patch_import"
    ? "Upload a .polpatch, or pick one in the clients folder. It is checked in full before anything changes."
    : "Upload an archive of the install, or pick its folder. The region and product say which title it is.";
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
  $("#impHint").textContent = (m.import || {}).hint ? `Point it at ${m.import.hint}.` : "";
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
    $("#impPct").textContent = pct < 100 ? `${pct}% of ${impBytes(e.total)}` : "Unpacking on the server...";
  };
  x.onload = async () => {
    let j = {}; try { j = JSON.parse(x.responseText || "{}"); } catch (e) { /* not JSON */ }
    if (x.status !== 201) { $("#impPct").textContent = j.error || "The upload failed (" + x.status + ")."; return; }
    $("#impPct").textContent = `Uploaded: ${j.files} files.` + (impMatches(j.matches) ? " It looks right." : "");
    await impUploads();
    impChoose(impUploadSource(j.upload, j.matches), file.name);
  };
  x.onerror = () => { $("#impPct").textContent = "The upload stopped. Check the connection and try again."; };
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
    $("#impMissing").innerHTML = esc(j.error || "The deployer refused the import.") + miss;
    $("#impMissing").hidden = false;
    $("#impGo").disabled = false;
    return;
  }
  $("#impModal").classList.remove("show");
  toast(impPatchMode() ? "Started" : `Import ${IMP.item}: started`);
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
    if (!confirm("Remove this upload from the server?")) return;
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
  const b = e.target.closest("[data-deploy]");
  if (b) {
    const act = b.dataset.deploy, game = b.dataset.game;
    const what = `${ACT_WORD[act]} ${game === "core" ? "OpenLobby" : game}`;
    if (act !== "check" && !confirm(`${what}?\n\nThis changes what runs on the server.`)) return;
    try {
      const r = await api("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ action: act, game }) });
      toast(`${what}: started`);
      await probeDeploy(); renderDeploy();
      followJob(r.job);
    } catch (err) { toast(err.message, true); }
    return;
  }
  const li = e.target.closest("#deployJobs [data-job]");
  if (li) followJob(li.dataset.job);
});
