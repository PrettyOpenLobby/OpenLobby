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
  else if (tool && !g.tools.some((t) => t.id === tool && !t.panel)) tool = "";
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
      + `${m.hint ? ` <span class="muted">${esc(m.hint)}</span>` : ""}</div>`).join("")
      + (g.installed && g.enabled && (g.admin_env || []).length
        ? `<div class="muted">For its admin page set ${(g.admin_env || []).map((e) => `<span class="mono">${esc(e)}</span>`).join(" and ")}.</div>` : "")
      + (g.notes ? `<div class="muted">${esc(g.notes)}</div>` : "");
    return `<tr><td class="strong">${esc(g.name || g.key)}<div class="muted mono">${esc(g.key)}</div></td>`
      + `<td><span class="pill ${cls === "good" ? "live" : cls === "bad" ? "off" : "env"}">${esc(word)}</span></td>`
      + `<td>${needs || '<span class="muted">-</span>'}</td>`
      + `<td><div class="deploy-acts">${acts.map((a) => `<button class="${a === "disable" ? "danger" : "ghost"} sm" `
        + `data-deploy="${esc(a)}" data-game="${esc(g.key)}"${busy ? " disabled" : ""}>${esc(ACT_WORD[a])}</button>`).join("")}</div></td></tr>`;
  }).join("");
  $("#deployJobs").innerHTML = (d.jobs || []).map((j) =>
    `<li data-job="${esc(j.id)}"><span class="t1">${esc(ACT_WORD[j.action] || j.action)} ${esc(j.game)}: `
    + `${esc(JOB_WORD[j.state] || j.state)}${j.summary ? " - " + esc(j.summary) : ""}</span>`
    + `<span class="t2">${j.started ? esc(ago(j.started * 1000)) : ""}</span></li>`).join("")
    || '<li class="empty">No jobs yet.</li>';
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
      DEPLOY_OFFSET += new TextEncoder().encode(r.text).length;
      pre.scrollTop = pre.scrollHeight;
    }
    if (JOB_DONE.has(r.state)) {
      stopDeployPoll();
      await showJob(id);
      await probeDeploy();
      renderDeploy();
    }
  }, 1500);
}

function stopDeployPoll() { clearInterval(DEPLOY_TIMER); DEPLOY_TIMER = null; }

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
