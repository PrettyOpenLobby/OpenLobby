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
    await fetchGames(false);
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
function renderGamesSide() {
  const cur = CUR_TAB === "pml" ? "viewer" : CUR_TAB === "games" ? (GAME_TOOL || "").split("/")[0] : null;
  $("#gamesList").innerHTML =
    `<button class="gx-item${cur === "" ? " on" : ""}" data-game="">All games</button>`
    + GAMES.map((g) => {
      const st = gameState(g);
      const on = g.live && typeof g.live.online === "number" ? g.live.online : "";
      return `<button class="gx-item${cur === g.key ? " on" : ""}" data-game="${esc(g.key)}">`
        + `<span class="dot ${st.cls}" aria-hidden="true"></span>${esc(g.short || g.title)}`
        + `<span class="n">${esc(on)}</span></button>`;
    }).join("");
}

// ---- every game ----
function renderGameGrid() {
  const total = GAMES.reduce((n, g) => n + (g.live && typeof g.live.online === "number" ? g.live.online : 0), 0);
  const cards = GAMES.map((g) => {
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
  }).join("");
  $("#gamesMain").innerHTML =
    `<div class="gx-top"><h2 class="gx-title">Games</h2>`
    + `<span class="muted">${GAMES.length} on this server, ${total} players online</span></div>`
    + (cards ? `<div class="gx-grid">${cards}</div>`
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

  const actHtml = g.actions.length ? `<section class="panel gx-wide"><h3>Actions</h3><div class="gx-acts">`
    + g.actions.map((a) => `<form class="gx-act" data-action="${esc(a.id)}">`
      + `<div class="strong">${esc(a.label)}</div>${a.about ? `<div class="muted">${esc(a.about)}</div>` : ""}`
      + (a.fields || []).map((f) => `<label>${esc(f.label || f.id)}<input type="text" name="${esc(f.id)}" autocomplete="off"></label>`).join("")
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

  $("#gamesMain").innerHTML = head + `<div class="gx-stats">${statHtml}</div>`
    + `<div class="gx-layout">${playersHtml}${actHtml}${roomsHtml}${svcHtml}${about}</div>`;
}

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
  if (GAMES_LOADED) renderGamesSide();
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
    const btn = form.querySelector("button");
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

async function loadModules() {
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
      + `<td>${m.shown ? "yes" : "no"}</td><td>${esc(tools)}${m.actions ? `, ${m.actions} action${m.actions === 1 ? "" : "s"}` : ""}</td>`
      + `<td class="${st && st.error ? "bad" : "muted"}">${esc(liveText)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No modules found.</td></tr>`;
  const probs = r.problems || [];
  $("#modulesProblems").hidden = !probs.length;
  $("#modulesProblemList").innerHTML = probs.map((p) => `<li>${esc(p)}</li>`).join("");
}

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

async function loadDeploy() {
  await probeDeploy();
  renderDeploy();
  if (!DEPLOY || !DEPLOY.available) return;
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
const impMatches = (ms) => (ms || []).some((m) => m.game === IMP.game && m.item === IMP.item);

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
    const key = JSON.stringify({ upload: u.id });
    return `<li data-key="${esc(key)}" data-upload="${esc(u.id)}" data-name="${esc(u.name)}" class="${impMatches(u.matches) ? "match" : ""}">`
      + `<span class="t1">${esc(u.name || u.id)}</span><span class="t2">${esc(impBytes(u.size))}, ${esc(u.files)} files`
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
  $("#impDirs").innerHTML = `<li data-key="${esc(here)}" data-use="${esc(IMP.path)}" class="${impMatches(r.matches) ? "match" : ""}">`
      + `<span class="t1">Use this folder</span><span class="t2">${(r.files || []).length === 1 ? "1 file" : esc((r.files || []).length) + " files"} here</span></li>`
    + (r.dirs || []).map((d) => `<li data-dir="${esc((IMP.path ? IMP.path + "/" : "") + d)}"><span class="t1">${esc(d)}/</span></li>`).join("")
    + (r.truncated ? '<li class="empty">More folders than shown.</li>' : "");
  if (impMatches(r.matches)) impChoose({ path: IMP.path }, "/" + IMP.path);
}

async function openImport(game, item) {
  const g = ((DEPLOY || {}).games || []).find((x) => x.key === game) || {};
  const m = (g.missing || []).find((x) => x.item === item && x.import) || {};
  IMP = { game, item, path: "", source: null };
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
    impChoose({ upload: j.upload }, file.name);
  };
  x.onerror = () => { $("#impPct").textContent = "The upload stopped. Check the connection and try again."; };
  x.send(file);
}

async function impGo() {
  if (!IMP || !IMP.source) return;
  $("#impGo").disabled = true;
  const r = await fetch("/api/deploy/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "import", game: IMP.game, item: IMP.item, source: IMP.source }) });
  let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status !== 202) {
    const miss = (j.missing || []).map((m) => `<div><span class="mono">${esc(m.path)}</span>${m.hint ? ` <span class="muted">${esc(m.hint)}</span>` : ""}</div>`).join("");
    $("#impMissing").innerHTML = esc(j.error || "The deployer refused the import.") + miss;
    $("#impMissing").hidden = false;
    $("#impGo").disabled = false;
    return;
  }
  $("#impModal").classList.remove("show");
  toast(`Import ${IMP.item}: started`);
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
  if (li && li.dataset.upload) return impChoose({ upload: li.dataset.upload }, li.dataset.name || li.dataset.upload);
  if (li && li.dataset.use !== undefined) return impChoose({ path: li.dataset.use }, "/" + li.dataset.use);
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
