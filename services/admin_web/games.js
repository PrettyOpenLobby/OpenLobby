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
    + `<div class="gx-layout">${playersHtml}${actHtml}${roomsHtml}${svcHtml}${about}</div>`;
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
}

// ---- Settings > Modules: games added here, by hand or by their setup program (adminpair.py) ----
function renderAdded() {
  $("#addedBody").innerHTML = ADDED.map((g) => {
    const how = g.how === "paired" && g.paired ? "Paired by its setup program" : "Added by hand";
    const when = g.added_at ? ago(g.added_at * 1000) : "";
    return `<tr><td class="strong">${esc(g.title)}<div class="muted mono">${esc(g.key)}</div></td>`
      + `<td><span class="mono">${esc(g.url)}</span>`
      + `${g.env ? `<div class="patch-why">The environment also sets POL_ADMIN_GAME_${esc(g.key.toUpperCase())}_URL, and it wins.</div>` : ""}</td>`
      + `<td>${esc(how)}<div class="muted">${esc([g.added_by, when].filter(Boolean).join(", "))}</div></td>`
      + `<td>${g.token_set ? "set" : '<span class="muted">none</span>'}</td>`
      + `<td><div class="deploy-acts"><button class="ghost sm" data-ga-edit="${esc(g.key)}">Change</button>`
      + `${g.paired ? `<button class="ghost sm" data-ga-unpair="${esc(g.key)}">Unpair</button>` : ""}`
      + `<button class="danger sm" data-ga-remove="${esc(g.key)}">Remove</button></div></td></tr>`;
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
});

// ---- Updates: the core and each game (adminupdates.py), owner only ----
// In Install games when a deployer answers (with its Update buttons), else in Settings > Modules.
let UPD = null, UPD_TIMER = null;
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
  if (it.update === "available") {
    tail = updByDeployer(it.key)
      ? `<div class="upd-act"><button class="act sm" data-upd-go="${esc(it.key)}">Update</button></div>`
      : (it.how ? `<div class="upd-how">${esc(it.how)}</div>` : "");
  } else if (it.update === "unknown" && it.why) {
    tail = `<div class="upd-why">${esc(it.why)}</div>`;
  }
  const ch = it.changes || [];
  const li = (c) => `<li><span class="s">${esc(c.subject)}</span><span class="d">${esc(updDate(c.date))}</span></li>`;
  const news = ch.length ? `<details${ch.length <= 3 ? " open" : ""}><summary>What's new (${ch.length})</summary>`
    + `<ul class="upd-changes">${ch.slice(0, 10).map(li).join("")}</ul>`
    + (ch.length > 10 ? `<button class="ghost sm" data-upd-all="${esc(it.key)}" style="margin-top:6px">Show all ${ch.length}</button>` : "")
    + `</details>` : "";
  return `<div class="upd-item" data-upd="${esc(it.key)}"><div class="upd-head"><span class="nm">${esc(it.name || it.key)}</span>`
    + `<span class="pill ${cls}">${esc(word)}</span>${behind ? `<span class="muted">${esc(behind)}</span>` : ""}</div>`
    + `${vers ? `<div class="upd-vers">${vers}</div>` : ""}${tail}${news}</div>`;
}

function renderUpdates() {
  const box = DEPLOY && DEPLOY.available ? $("#updDeploy") : $("#updModules");
  const other = box.id === "updDeploy" ? $("#updModules") : $("#updDeploy");
  other.hidden = true;
  const r = UPD || {};
  const items = r.items || [];
  box.hidden = false;
  const when = r.checking ? "checking now..." : (r.checked_at ? "checked " + ago(Date.parse(r.checked_at)) : "");
  box.innerHTML = `<h2>Updates <span class="h2sub">${esc(when)}</span></h2>`
    + `<div class="toolbar"><button class="ghost sm" data-upd-check${r.checking ? " disabled" : ""}>Check now</button></div>`
    + `<div class="upd-list">${items.map(updItem).join("") || '<p class="hint">Nothing reports its version yet.</p>'}</div>`;
}

async function loadUpdates(refresh) {
  if (!isOwner()) return;
  clearTimeout(UPD_TIMER);
  try { UPD = await api("/api/updates" + (refresh ? "?refresh=1" : "")); }
  catch (e) { toast(e.message, true); return; }
  if (CUR_TAB === "deploy" || CUR_TAB === "modules") renderUpdates();
  // while a check runs, ask again every 3 s
  if (UPD.checking && ["deploy", "modules", "overview"].includes(CUR_TAB))
    UPD_TIMER = setTimeout(() => loadUpdates(false), 3000);
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
  const all = t.closest("[data-upd-all]");
  if (all) {
    const it = ((UPD || {}).items || []).find((x) => x.key === all.dataset.updAll);
    if (!it) return;
    const li = (c) => `<li><span class="s">${esc(c.subject)}</span><span class="d">${esc(updDate(c.date))}</span></li>`;
    all.previousElementSibling.innerHTML = (it.changes || []).map(li).join("");
    all.remove();
    return;
  }
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
                   restart: "Restart", check: "Check" };

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
    `<li data-job="${esc(j.id)}"><span class="t1">${esc(ACT_WORD[j.action] || j.action)} ${esc(j.game)}: `
    + `${esc(JOB_WORD[j.state] || j.state)}${j.summary ? " - " + esc(j.summary) : ""}</span>`
    + `<span class="t2">${esc(jobWhen(j))}</span></li>`).join("")
    || '<li class="empty">No jobs yet.</li>';
}

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
                     patch_remove: "Remove bundle", patch_check: "Check bundles" };
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
      + `<td><span class="mono">${esc(b.latest || "-")}</span><div class="muted">${esc((b.versions || []).length)} version${(b.versions || []).length === 1 ? "" : "s"}</div></td>`
      + `<td>${b.served ? '<span class="pill live">Yes</span>' : '<span class="pill off">No</span>'}</td>`
      + `<td>${sc}${b.selfcheck_at ? `<div class="muted">${esc(jobWhen({ started: b.selfcheck_at }))}</div>` : ""}`
      + `${b.selfcheck_why ? `<div class="patch-why">${esc(b.selfcheck_why)}</div>` : ""}</td>`
      + `<td>${esc(impBytes(b.size))}</td>`
      + `<td><div class="deploy-acts">${older.length ? `<select data-rollto="${esc(key)}"><option value="">Roll back to...</option>`
        + older.slice().reverse().map((v) => `<option>${esc(v)}</option>`).join("") + `</select>` : ""}`
      + `<button class="danger sm" data-patchrm="${esc(key)}"${busy ? " disabled" : ""}>Remove</button></div></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">No bundles in the mirrors folder yet.</td></tr>';
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
  if (!DEPLOY || !DEPLOY.available) return;
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
  $("#deployJobTitle").textContent = `${ACT_WORD[j.action] || j.action} ${j.game}`;
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
      await showJob(id);
      await probeDeploy();
      renderDeploy();
      if (String(j.action || "").startsWith("patch_") || PATCHES) loadPatches();
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
