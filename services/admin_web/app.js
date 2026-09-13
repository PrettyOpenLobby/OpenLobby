// app.js -- admin dashboard logic (codes, accounts, PML editor wiring).
"use strict";

const $ = (s) => document.querySelector(s);
// Handles and notes come from the CLIENT, so they can contain markup-significant
// symbols (SE's own handle rule is "alphanumeric characters and symbols").
// Escape anything that reaches innerHTML.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const t = await r.text();
  let j; try { j = t ? JSON.parse(t) : {}; } catch { j = { raw: t }; }
  // The session expired (or was revoked by a password change elsewhere). A
  // reload lands on the login page instead of leaving a dead panel toasting
  // "not signed in" at every click.
  if (r.status === 401 && j.auth) { location.reload(); throw new Error("signed out"); }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

let toastTimer;
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", !!err);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

// ---- tabs, and where you were ----
// The open tab -- and on the PML tab, the open file -- live in the URL hash:
//
//     #accounts
//     #pml/wh000.pol.com/pml/help/login/index.pml
//
// so a refresh, a bookmark and the browser's back button all land where you
// left off. This is a single page, so without it every reload dropped you on
// Codes with an empty editor, which costs a click and a search every time the
// server restarts underneath you.
const TABS = [...document.querySelectorAll("nav button")].map((b) => b.dataset.tab);

// A path is the rest of the hash. It must be encoded -- the URL-cache pages are
// named `index.pml%3Fcnt%3D2...` and an unencoded `%3F` would decode back to a
// `?` and split the path -- but `/` is left alone so the hash stays readable.
const encPath = (p) => encodeURIComponent(p).replace(/%2F/g, "/");

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const slash = raw.indexOf("/");
  const tab = slash < 0 ? raw : raw.slice(0, slash);
  let path = "";
  try { path = slash < 0 ? "" : decodeURIComponent(raw.slice(slash + 1)); }
  catch (e) { path = ""; }              // a hand-mangled hash is not an error
  return { tab, path };
}

// replaceState, not `location.hash =`: opening a file is a selection within the
// tab you are already on, and a history entry per click would make Back mean
// "the file before this one" for as long as you kept browsing.
function writeHash(tab, path) {
  const h = "#" + tab + (path ? "/" + encPath(path) : "");
  if (location.hash !== h) history.replaceState(null, "", h);
}

function showTab(name) {
  if (!TABS.includes(name)) name = TABS[0];
  document.querySelectorAll("nav button").forEach(
    (x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(
    (x) => x.classList.toggle("active", x.id === "tab-" + name));
  if (name === "accounts") loadAccounts();
  if (name === "reports") loadReports();
  if (name === "issues") loadIssues();
  if (name === "gmcalls") { loadGmCalls(); startGmDesk(); } else stopGmDesk();
  if (name === "news") loadNews();
  if (name === "security") loadSession();
  return name;
}

async function applyHash() {
  const { tab, path } = parseHash();
  if (showTab(tab) !== "pml" || !path || path === ACTIVE_FILE) return;
  await PML_LIST_READY;   // openPmlFile reads SHAPES to decide the Show filter
  openPmlFile(path);
}

document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    // Carry the open file with you, so leaving PML and coming back -- or
    // refreshing while away -- does not lose it.
    const tab = b.dataset.tab;
    const before = location.hash;
    location.hash = "#" + tab
      + (tab === "pml" && ACTIVE_FILE ? "/" + encPath(ACTIVE_FILE) : "");
    // Clicking the tab you are already on fires no hashchange, so apply it
    // here -- but only then, or every tab click would load its data twice.
    if (location.hash === before) applyHash();
  };
});
window.addEventListener("hashchange", applyHash);

// ---- session / credentials ----
// The panel is OPEN when no credential is set (first run, loopback bind), so
// every control here has to work in both states: with auth on we ask for the
// current password, with auth off there isn't one to ask for.
let SESSION = {};
async function loadSession() {
  try { SESSION = await api("/api/session"); } catch { SESSION = {}; }
  const who = $("#who");
  if (SESSION.authenticated) {
    who.innerHTML = `<span>signed in as <b style="color:var(--text)">${SESSION.user}</b></span>`;
    const out = document.createElement("button");
    out.textContent = "Sign out";
    out.onclick = async () => {
      try { await api("/api/logout", { method: "POST" }); location.reload(); }
      catch (e) { toast(e.message, true); }
    };
    who.appendChild(out);
  } else {
    who.innerHTML = `<span title="No operator password is set - anyone who can reach this port has full access.">unauthenticated · panel is open</span>`;
  }

  const authOn = !!SESSION.auth_required;
  $("#secCurrentWrap").style.display = authOn ? "" : "none";
  $("#secUser").value = SESSION.user || "admin";
  const st = $("#secState");
  if (!authOn) {
    st.className = "note warn";
    st.innerHTML = `<b>No password is set.</b> Anyone who can reach this port can
      mint registration codes and accounts. That is fine while the panel is bound to
      127.0.0.1 - set one before changing <code class="mono">POL_ADMIN_BIND</code>
      to a reachable address.`;
  } else if (SESSION.credential_set) {
    const when = (SESSION.updated_at || "").slice(0, 19).replace("T", " ");
    st.className = "note";
    st.innerHTML = `<b>Password set${when ? " " + when + " UTC" : ""}.</b> Stored
      hashed in accounts.db.` + (SESSION.env_override
        ? ` <b style="color:var(--warn)">POL_ADMIN_PASSWORD is also set</b> in the
          environment and will keep working as a break-glass login until you clear
          it from .env and restart the service.` : "");
  } else {
    st.className = "note warn";
    st.innerHTML = `<b>Signed in with the POL_ADMIN_PASSWORD environment
      override.</b> Set a password here to move the credential into the database,
      where it can be changed without editing .env and restarting.`;
  }
}

$("#secSave").onclick = async () => {
  const username = $("#secUser").value.trim();
  const password = $("#secNew").value, confirm = $("#secNew2").value;
  if (!username) return toast("Username is required", true);
  if (password !== confirm) return toast("The two new passwords do not match", true);
  if (password.length < 8) return toast("Password must be at least 8 characters", true);
  try {
    const r = await api("/api/credentials", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: $("#secCurrent").value, username, password })
    });
    $("#secCurrent").value = ""; $("#secNew").value = ""; $("#secNew2").value = "";
    toast(`Saved - signed in as ${r.user}, other sessions signed out`);
    loadSession();
  } catch (e) { toast(e.message, true); }
};

// ---- content chips / legend ----
let CONTENT = {};
async function loadContentNames() {
  CONTENT = await api("/api/content-names");
  const chips = $("#contentChips");
  const grantChips = $("#grantChips");
  const newChips = $("#newChips");
  chips.innerHTML = "";
  grantChips.innerHTML = "";
  newChips.innerHTML = "";
  Object.entries(CONTENT).forEach(([code, name]) => {
    const lab = document.createElement("label");
    lab.className = "chip";
    lab.innerHTML = `<input type="checkbox" value="${code}" ${code === "1" ? "checked" : ""}> ${name}`;
    chips.appendChild(lab);
    // The same chips on the accounts tab: granting somebody the four titles
    // they actually bought should be one click, not four round trips.
    const g = document.createElement("label");
    g.className = "chip";
    g.innerHTML = `<input type="checkbox" value="${code}"> ${name}`;
    grantChips.appendChild(g);
    // And again on the create form. Ticked for content 1 to match the
    // in-client sign-up's default, which grants PlayOnline itself.
    const n = document.createElement("label");
    n.className = "chip";
    n.innerHTML = `<input type="checkbox" value="${code}" ${code === "1" ? "checked" : ""}> ${name}`;
    newChips.appendChild(n);
  });
}

// ---- registration code entry ----
//
// Codes are five groups of four (see admin._rand_code), and SE's own screen says
// they are case-sensitive -- but every code we issue is upper-case, so folding
// the input is safe and stops "play-nine-..." being created as a code nobody can
// then redeem from the client. Dashes are structure, not characters: they are
// re-derived on every keystroke, so pasting `PLAYNINEREVAGAMEFFXI` or
// `play nine reva game ffxi` both land on the canonical form.
const CODE_GROUPS = 5, CODE_GLEN = 4;
function maskCode(raw) {
  const body = String(raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "")
                                .slice(0, CODE_GROUPS * CODE_GLEN);
  return (body.match(new RegExp(`.{1,${CODE_GLEN}}`, "g")) || []).join("-");
}
$("#code").addEventListener("input", (e) => {
  const el = e.target;
  // Keep the caret where the typist left it: masking rewrites the whole value,
  // and without this every edit in the middle of a code jumps to the end.
  const before = el.value.slice(0, el.selectionStart).replace(/[^A-Za-z0-9]/g, "").length;
  el.value = maskCode(el.value);
  let pos = 0, seen = 0;
  while (pos < el.value.length && seen < before) { if (el.value[pos] !== "-") seen++; pos++; }
  el.setSelectionRange(pos, pos);
  const short = el.value.replace(/-/g, "").length;
  $("#codeHint").textContent = !short
    ? "Five groups of four - dashes are inserted as you type."
    : short < CODE_GROUPS * CODE_GLEN
      ? `${CODE_GROUPS * CODE_GLEN - short} more character(s) to go.`
      : "Complete.";
});

// ---- codes ----
async function loadCodes() {
  const body = $("#codesBody");
  try {
    const rows = await api("/api/codes");
    body.innerHTML = "";
    if (!rows.length) { body.innerHTML = `<tr><td colspan="4" style="color:var(--muted)">No codes yet.</td></tr>`; return; }
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      // Spent = either field. Deleting an account clears redeemed_by (it is a
      // foreign key into the row that just went) but leaves redeemed_at, so a
      // code judged on redeemed_by alone would read as unused again.
      const used = !!(r.redeemed_by || r.redeemed_at);
      const by = r.redeemed_by ? "redeemed by " + esc(r.redeemed_by)
                               : "redeemed (account deleted)";
      tr.innerHTML =
        `<td><code class="mono">${esc(r.code)}</code></td>` +
        `<td>${esc(r.contents_label || r.contents || "")}</td>` +
        `<td style="color:var(--muted)">${esc(r.note || "")}</td>` +
        `<td><span class="pill ${used ? "used" : "open"}">${used ? by : "unused"}</span></td>`;
      body.appendChild(tr);
    });
  } catch (e) { toast(e.message, true); }
}

$("#randBtn").onclick = async () => {
  try { $("#code").value = (await api("/api/codes/random", { method: "POST" })).code; }
  catch (e) { toast(e.message, true); }
};

$("#createBtn").onclick = async () => {
  const contents = [...document.querySelectorAll("#contentChips input:checked")].map((c) => +c.value);
  if (!contents.length) return toast("Pick at least one content", true);
  try {
    const r = await api("/api/codes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: $("#code").value.trim(), contents, note: $("#note").value.trim() })
    });
    toast("Created " + r.code);
    $("#code").value = ""; $("#note").value = "";
    loadCodes();
  } catch (e) { toast(e.message, true); }
};

// ---- accounts ----
async function loadAccounts() {
  const body = $("#accountsBody");
  try {
    const rows = await api("/api/accounts");
    if (rows.error) { toast(rows.error, true); return; }
    body.innerHTML = "";
    if (!rows.length) { body.innerHTML = `<tr><td colspan="6" style="color:var(--muted)">No accounts yet.</td></tr>`; return; }
    // Fill the ID picker on the grant form from the same fetch.
    $("#polidList").innerHTML = rows.map((r) => `<option value="${esc(r.polid)}">`).join("");
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      // PLAYABLE is not the same as OWNED. The launcher reads the per-handle
      // links (lobby 1:3), so a grant that was never linked shows as owned here
      // and as "You have no Content ID" on the client.
      const owned = (r.contents || []).length;
      const missing = (r.unlinked || []).length;
      const play = !owned ? `<span style="color:var(--muted)"> - </span>`
        : missing ? `<span class="pill used">${owned - missing}/${owned} - ${missing} not linked</span>`
                  : `<span class="pill open">all ${owned}</span>`;
      tr.innerHTML =
        `<td><code class="mono">${esc(r.polid)}</code></td>` +
        `<td>${esc(r.handle) || " - "}</td>` +
        `<td>${esc(r.contents_label) || " - "}</td>` +
        `<td>${play}</td>` +
        `<td style="color:var(--muted)">${esc((r.created_at || "").slice(0, 19).replace("T", " "))}</td>` +
        `<td style="text-align:right"><button class="ghost">Password</button> ` +
        `<button class="danger">Delete</button></td>`;
      const [pwBtn, delBtn] = tr.querySelectorAll("button");
      pwBtn.onclick = () => askPassword(r.polid);
      delBtn.onclick = () => askDelete(r.polid);
      body.appendChild(tr);
    });
  } catch (e) { toast(e.message, true); }
}

// ---- tester issue reports ----
// Filed by the shim's report chord; landed (and correlated) by
// services/issuereport.py. NOT the abuse reports below -- see the note on the
// section in index.html.
//
// Bundle files are fetched as TEXT and written with textContent, never
// innerHTML: every byte in a bundle came off a client machine, and a log line
// can contain anything at all. The screenshot is the one exception and it goes
// through an <img src>, where bytes cannot become markup.
async function loadIssues() {
  const body = $("#issuesBody");
  try {
    const rows = await api("/api/issues");
    if (rows.error) { toast(rows.error, true); return; }
    body.innerHTML = "";
    $("#issueDetail").style.display = "none";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="5" style="color:var(--muted)">` +
        `No issue reports filed.</td></tr>`;
      return;
    }
    rows.forEach((r) => {
      const w = r.window || {};
      const first = String(r.description || "").split("\n")[0];
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.innerHTML =
        `<td style="color:var(--muted)">${esc((r.received_at || "").slice(0, 19).replace("T", " "))}</td>` +
        `<td><b>${esc(r.handle) || " - "}</b><br>` +
          `<span style="color:var(--muted)">${esc(r.host) || ""}</span></td>` +
        `<td>${esc(r.title) || " - "}</td>` +
        `<td>${esc(first) || "<i>(no description)</i>"}</td>` +
        // The honest-negative column. A bundle whose lines could not be tied to
        // this client is the case you must not read as "the server was quiet".
        `<td>${w.correlation_warning
          ? `<span title="${esc(w.correlation_warning)}">WARNING: ${esc(w.correlated_lines || 0)}</span>`
          : esc(w.correlated_lines ?? " - ")}</td>`;
      tr.onclick = () => showIssue(r);
      body.appendChild(tr);
    });
  } catch (e) { toast(e.message, true); }
}

function showIssue(r) {
  const w = r.window || {};
  $("#issueId").textContent = r.id || "";
  $("#issueDesc").textContent = r.description || "(no description)";
  const warn = $("#issueWarn");
  warn.style.display = w.correlation_warning ? "" : "none";
  warn.textContent = w.correlation_warning || "";

  // The screenshot, inline, because for a rendering bug it IS the report.
  const shot = (r.client_files || []).find((f) => /\.(png|jpe?g)$/i.test(f.name));
  $("#issueShot").innerHTML = shot
    ? `<img src="/api/issue-file?id=${encodeURIComponent(r.id)}` +
      `&f=client/${encodeURIComponent(shot.name)}" ` +
      `style="max-width:100%; border:1px solid var(--line,#444); border-radius:4px">`
    : "";

  // Every file, client and server, as one clickable list. The per-channel byte
  // counts come from the manifest rather than being re-measured here.
  const cuts = {};
  (w.cuts || []).forEach((c) => { cuts[c.channel] = c; });
  const link = (rel, label, note) =>
    `<a href="#" data-f="${esc(rel)}">${esc(label)}</a>` +
    (note ? ` <span style="color:var(--muted)">${esc(note)}</span>` : "") + "<br>";
  let html = "<b>client</b><br>";
  (r.client_files || []).forEach((f) => {
    html += link("client/" + f.name, f.name, `${f.bytes} B`);
  });
  html += "<br><b>server</b><br>";
  (r.server_files || []).forEach((n) => {
    const c = cuts[n];
    html += link("server/" + n, n,
      c ? `${c.lines} lines${c.truncated ? " - TRUNCATED" : ""}` : "");
  });
  const box = $("#issueFiles");
  box.innerHTML = html;
  box.querySelectorAll("a[data-f]").forEach((a) => {
    a.onclick = (ev) => { ev.preventDefault(); openIssueFile(r.id, a.dataset.f); };
  });

  $("#issueView").textContent = "";
  $("#issueViewName").style.display = "none";
  $("#issueDetail").style.display = "";
  // correlated.log is what you actually want first, so open it unasked.
  if ((r.server_files || []).includes("correlated.log")) {
    openIssueFile(r.id, "server/correlated.log");
  }
}

async function openIssueFile(id, rel) {
  try {
    const url = `/api/issue-file?id=${encodeURIComponent(id)}` +
                `&f=${rel.split("/").map(encodeURIComponent).join("/")}`;
    if (/\.(png|jpe?g)$/i.test(rel)) { window.open(url, "_blank"); return; }
    const r = await fetch(url);
    const text = await r.text();
    $("#issueViewName").textContent = rel;
    $("#issueViewName").style.display = "";
    // textContent, NOT innerHTML -- see the note at the top of this section.
    $("#issueView").textContent = text || "(empty)";
  } catch (e) { toast(e.message, true); }
}

// ---- abuse reports ----
// The Viewer's "Report User" dialog really does send SMTP. Its body is a set of
// pseudo-XML <harassment_form_*> tags, parsed server-side and filed as JSON; this
// only renders them. The attached transcript is the client's own, so it is shown
// verbatim rather than reformatted.
async function loadReports() {
  const body = $("#reportsBody");
  try {
    const rows = await api("/api/reports");
    if (rows.error) { toast(rows.error, true); return; }
    body.innerHTML = "";
    $("#reportLogPanel").style.display = "none";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="5" style="color:var(--muted)">No reports filed.</td></tr>`;
      return;
    }
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td style="color:var(--muted)">${esc((r.received_at || "").slice(0, 19).replace("T", " "))}</td>` +
        `<td><b>${esc(r.suspect) || " - "}</b></td>` +
        `<td>${esc(r.application) || " - "}</td>` +
        `<td><code class="mono">${esc(r.from) || " - "}</code></td>` +
        `<td>${esc(r.explanation) || " - "}</td>`;
      if (r.log) {
        tr.style.cursor = "pointer";
        tr.title = "Show the attached transcript";
        tr.onclick = () => {
          $("#reportLogWho").textContent = r.suspect || "(unnamed)";
          $("#reportLog").textContent = r.log;
          $("#reportLogPanel").style.display = "";
        };
      }
      body.appendChild(tr);
    });
  } catch (e) { toast(e.message, true); }
}

// ---- the GM desk: queue control, and the room ----
//
// Two halves with very different footing, and the UI is written to keep them
// apart rather than let one borrow the other's credibility:
//
//   * the DESK is fully measured -- queue is 0x801 body +0x02, Join is flag
//     0x40, Start is 0x20, all confirmed against a live client -- so what this
//     sets is what a caller is told;
//   * the CONSOLE delivers, and that is all anyone has ever observed. Nothing
//     relayed has been seen to render, because a 'T' takes its speaker from the
//     client's member table and no relayed speaker has been admitted to it. So
//     the panel says "delivered", never "sent", and keeps the raw/nick probes
//     in reach.
let GM_TIMER = null, GM_ROOM = "", GM_SEEN = 0;

function stopGmDesk() { if (GM_TIMER) { clearInterval(GM_TIMER); GM_TIMER = null; } }
function startGmDesk() {
  stopGmDesk();
  loadGmDesk();
  // 4s: fast enough that a room reads as a conversation, slow enough that a
  // panel left open all day is not a load. The renew below rides this tick, so
  // an on-duty claim only expires by the tab actually going away.
  GM_TIMER = setInterval(loadGmDesk, 4000);
}

const gmTime = (t) => new Date((t || 0) * 1000).toLocaleTimeString();

async function loadGmDesk() {
  let d;
  try {
    d = await api("/api/gm-desk" + (GM_ROOM ? "?room=" + encodeURIComponent(GM_ROOM) : ""));
  } catch (e) { return; }          // a poll that fails must not toast every 4s
  GM_ROOM = d.room || GM_ROOM;
  $("#gmRoomName").textContent = GM_ROOM || "(no room - is gmd running?)";

  // The desk state, in the words the server used. `duty === null` is a THIRD
  // state and saying "off duty" for it would be a claim nobody made.
  const state = d.on_duty ? "a GM is on duty"
    : d.duty === null ? "nobody has claimed the desk"
    : "off duty";
  $("#gmState").textContent = state;
  $("#gmWhy").textContent = d.flags_why || "";
  $("#gmWhy").className = "pill " + (d.join ? "open" : "used");
  if (d.on_duty) $("#gmClearDuty").disabled = false;

  const line = (k, v, hot) =>
    `<div class="${hot ? "hot" : ""}"><span>${k}</span><span>${esc(v)}</span></div>`;
  const hex = (n) => "0x" + (n >>> 0).toString(16);
  // What gmd LAST TOLD a caller, beside what it would say now. They differ until
  // the next poll, and an operator who flips a switch and sees the client not
  // move needs that difference on screen rather than assuming it failed.
  const sv = d.serving || {};
  let html =
    line("Flags now", `${hex(d.flags)} - ${d.join ? "Join" : "no Join"}, ${d.start ? "Start" : "no Start"}`) +
    line("Last served to a caller",
         sv.flags === undefined ? "nothing polled yet"
           : `${hex(sv.flags)} at ${gmTime(sv.at)}`,
         sv.flags !== undefined && sv.flags !== d.flags) +
    line("Queue", d.pinned_queue !== null && d.pinned_queue !== undefined
         ? `${d.pinned_queue} (pinned)`
         : `${d.waiting} waiting (live)` + (d.env_queue ? ` - POL_GMD_QUEUE=${d.env_queue}` : ""));
  if (d.pinned_flags !== null && d.pinned_flags !== undefined)
    html += line("Flags pinned to", hex(d.pinned_flags), true);
  if (d.by) html += line("Last changed by", d.by);
  html += line("Undelivered in the spool", d.pending,
               d.pending > 0);
  (d.callers || []).forEach((c) => html += line(
    `Caller ${c.peer}`,
    (c.request_no ? `request #${c.request_no}` : "no ticket yet")
    + `, idle ${c.idle}s` + (c.live ? "" : " - aged out")));
  $("#gmDesk").innerHTML = html;

  // The transcript. Re-rendered only when it grew, so the operator's scroll
  // position survives a poll that changed nothing.
  const rows = d.transcript || [];
  if (rows.length !== GM_SEEN) {
    GM_SEEN = rows.length;
    const log = $("#gmLog");
    const wasBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    log.innerHTML = rows.map((r) =>
      `<div class="${r.dir === "out" ? "out" : "in"}">` +
      `<span class="t">${esc(gmTime(r.at))}</span>` +
      `<span class="w">${esc(r.nick || "?")}</span>` +
      `<span class="m">${esc(r.text)}` +
      // The raw bytes stay on screen. They are the CAPTURE -- the encoders here
      // were corrected once already by reading a client's own record out of a
      // log by hand, and hiding the hex would put that back the way it was.
      `<br><span class="hexy">${esc(r.raw)}</span></span></div>`).join("")
      || `<div><span class="m" style="color:var(--muted)">Nothing in this room yet. The client's own records land here too - its presence heartbeat should appear within a minute of it joining.</span></div>`;
    if (wasBottom) log.scrollTop = log.scrollHeight;
  }

  // Hold the on-duty claim open while this tab is. It EXTENDS and never creates,
  // so a forgotten tab cannot re-open a desk somebody deliberately closed.
  if (d.on_duty) {
    try { await api("/api/gm-control", gmPost({ renew: true })); } catch (e) {}
  }
}

const gmPost = (body) => ({
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body)
});

async function gmControl(body, msg) {
  try {
    const r = await api("/api/gm-control", gmPost(body));
    if (msg) toast(msg + (r.note ? " - " + r.note : ""));
    GM_SEEN = -1;                 // force the next poll to re-render
    loadGmDesk();
  } catch (e) { toast(e.message, true); }
}

$("#gmOnDuty").onclick = () => gmControl({ on_duty: true }, "On duty");
$("#gmOffDuty").onclick = () => gmControl({ on_duty: false }, "Signed off");
$("#gmClearDuty").onclick = () =>
  gmControl({ on_duty: null }, "Duty handed back to POL_GMD_STATUS_FLAGS");
$("#gmPin").onclick = () => {
  const q = $("#gmQueue").value.trim(), f = $("#gmFlags").value.trim();
  gmControl({ queue: q === "" ? null : q, flags: f === "" ? null : f }, "Applied");
};
$("#gmUnpin").onclick = () => {
  $("#gmQueue").value = ""; $("#gmFlags").value = "";
  gmControl({ queue: null, flags: null }, "Pins cleared");
};

async function gmSay(body, clear) {
  if (!GM_ROOM) return toast("No room - is gmd running?", true);
  try {
    const r = await api("/api/gm-say", gmPost({ ...body, room: GM_ROOM,
                                                nick: $("#gmNick").value.trim() }));
    // DELIVERED, not sent, and pending is the honest read: it only falls when a
    // session that is actually in that room comes round the relay loop.
    toast(r.pending > 1 ? `Spooled - ${r.pending} waiting, is anyone in the room?`
                        : "Spooled - delivered on the next relay pass");
    if (clear) clear.forEach((sel) => { $(sel).value = ""; });
    GM_SEEN = -1;
    loadGmDesk();
  } catch (e) { toast(e.message, true); }
}

$("#gmSend").onclick = () => {
  const t = $("#gmSay").value.trim();
  if (!t) return toast("Nothing to say", true);
  gmSay({ say: t }, ["#gmSay"]);
};
$("#gmSay").onkeydown = (e) => { if (e.key === "Enter") $("#gmSend").click(); };
$("#gmSendEvent").onclick = () => {
  const ev = $("#gmEvent").value;
  if (!ev) return toast("Pick an event", true);
  gmSay({ event: ev, who: $("#gmEventWho").value.trim() || "GM" });
};
$("#gmSendRaw").onclick = () => {
  const r = $("#gmRaw").value;
  if (!r) return toast("Nothing to send", true);
  gmSay({ raw: r });
};

// ---- GM call requests ----
// Filed by the `gmd` service from the 0x102 the client sends on Submit. It used
// to be gmserver.exe on the host, because the cipher was reached by mapping
// polcore.dll; gmcrypt.py replaced that, so the GM band is now an ordinary
// compose service writing into the shared data dir, and this just renders it.
async function loadGmCalls() {
  const body = $("#gmBody");
  try {
    const rows = await api("/api/gm-calls");
    if (rows.error) { toast(rows.error, true); return; }
    body.innerHTML = "";
    $("#gmDetail").style.display = "none";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="6" style="color:var(--muted)">No GM calls filed. Check that the <code>gmd</code> service is up, then submit one from the client's GM Call screen.</td></tr>`;
      return;
    }
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td style="color:var(--muted)">${esc((r.received_at || "").slice(0, 19).replace("T", " "))}</td>` +
        `<td><code class="mono">${esc(String(r.request_no ?? " - "))}</code></td>` +
        `<td><b>${esc(r.handle) || " - "}</b></td>` +
        `<td>${esc(r.content_label) || esc(String(r.content_id ?? " - "))}</td>` +
        `<td>${esc(String(r.issue ?? " - "))}</td>` +
        `<td>${esc(r.subject) || " - "}</td>`;
      tr.style.cursor = "pointer";
      tr.title = "Show the full request";
      tr.onclick = () => {
        $("#gmWho").textContent =
          `${r.request_no ?? "?"} - ${r.handle || "(unknown)"}: ${r.subject || ""}`;
        $("#gmText").textContent = r.body || "(no body text)";
        $("#gmDetail").style.display = "";
      };
      body.appendChild(tr);
    });
  } catch (e) { toast(e.message, true); }
}

// ---- delete an account ----
// Two-step on purpose: the server is asked what the deletion would destroy, the
// operator sees that inventory, and only a POL ID typed back arms the button.
// Nothing in accounts.db can be undone and the panel takes no backup.
let DEL_POLID = null;

async function askDelete(polid) {
  let fp;
  try { fp = await api("/api/account-footprint?polid=" + encodeURIComponent(polid)); }
  catch (e) { return toast(e.message, true); }
  DEL_POLID = polid;
  $("#delPolid").textContent = polid;
  $("#delEcho").textContent = polid;
  $("#delConfirm").value = "";
  $("#delRelease").checked = false;
  $("#delGo").disabled = true;
  $("#delRelease").parentElement.style.display = fp.regcodes.length ? "" : "none";

  const line = (k, v, hot) =>
    `<div class="${hot ? "hot" : ""}"><span>${k}</span><span>${esc(v)}</span></div>`;
  const members = fp.members.map((m) => m.login_name).join(", ") || " - ";
  let html =
    line("Members", members) +
    line("Handles", fp.handles.join(", ") || " - ") +
    line("Content", fp.contents_label || " - ") +
    line("Friend entries", fp.friends) +
    line("Groups", fp.groups) +
    line("On other people's lists", fp.referenced_by, fp.referenced_by > 0) +
    line("Mail messages", fp.mail, fp.mail > 0) +
    line("Open sessions", fp.sessions);
  if (fp.online) html += line("Signed in", "yes - they will be dropped", true);
  if (fp.regcodes.length) html += line("Redeemed code(s)", fp.regcodes.join(", "));
  $("#delInv").innerHTML = html;
  $("#delModal").classList.add("show");
  $("#delConfirm").focus();
}

function closeDelete() {
  $("#delModal").classList.remove("show");
  DEL_POLID = null;
}

$("#delConfirm").oninput = () => {
  $("#delGo").disabled = $("#delConfirm").value.trim() !== DEL_POLID;
};
$("#delConfirm").onkeydown = (e) => {
  if (e.key === "Enter" && !$("#delGo").disabled) $("#delGo").click();
};
$("#delCancel").onclick = closeDelete;
$("#delModal").onclick = (e) => { if (e.target === $("#delModal")) closeDelete(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#delModal").classList.contains("show")) closeDelete();
});

$("#delGo").onclick = async () => {
  const polid = DEL_POLID, btn = $("#delGo");
  btn.disabled = true;
  try {
    await api("/api/account-delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, confirm: $("#delConfirm").value.trim(),
                             release_codes: $("#delRelease").checked })
    });
    closeDelete();
    toast("Deleted " + polid);
    loadAccounts();
    loadCodes();          // a released code becomes unused again
  } catch (e) { btn.disabled = false; toast(e.message, true); }
};

// ---- create an account ----
// POSTs to the same accounts.register_account the in-client sign-up calls, so an
// operator-made account is indistinguishable from a player-made one. The
// response carries the password ONCE -- only the PBKDF2 hash is stored -- so it
// is rendered and left on screen rather than toasted away after three seconds.
// The client's login screen can only TYPE letters and digits into the
// password field (observed on a real Viewer; the server itself accepts any
// printable ASCII). An admin-set password outside that set would lock the
// player out at the keyboard, so warn before creating one.
function viewerTypable(pw) {
  return !pw || /^[A-Za-z0-9]*$/.test(pw) ||
    window.confirm("This password contains characters the client's login " +
      "screen cannot type (only letters and digits work there). Use it anyway?");
}

$("#newBtn").onclick = async () => {
  const btn = $("#newBtn");
  const codes = [...document.querySelectorAll("#newChips input:checked")].map((c) => +c.value);
  if (!viewerTypable($("#newPw").value.trim())) return;
  btn.disabled = true;
  try {
    const r = await api("/api/account-create", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        handle: $("#newHandle").value.trim(),
        password: $("#newPw").value.trim(),
        code: maskCode($("#newCode").value),
        content_codes: codes
      })
    });
    const line = (k, v, hot) =>
      `<div class="${hot ? "hot" : ""}"><span>${k}</span><span class="mono">${esc(v)}</span></div>`;
    // The ID and the NICK are DIFFERENT strings and the client sends the NICK.
    // Handing over only the PlayOnline ID has stranded people before, so both
    // are shown, labelled by what they are for.
    $("#newResult").innerHTML =
      line("PlayOnline ID", r.polid) +
      (r.nick ? line("Signs in as", r.nick) : "") +
      line("Handle", r.handle) +
      line("Password", r.password, true) +
      line("Content", r.contents_label || " - ") +
      line("Mail", r.mail) +
      `<div class="hot"><span>Write the password down</span>` +
      `<span>it is not stored and cannot be shown again</span></div>`;
    $("#newResult").style.display = "";
    $("#newHandle").value = ""; $("#newPw").value = ""; $("#newCode").value = "";
    toast("Created " + r.polid);
    loadAccounts();
    loadCodes();          // a code spent here is no longer unused
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
};

// ---- change a password ----
// TWO KEYS, and the dialog says so. `password` is the PBKDF2 hash the ucs-cgi
// account screens verify; the LOBBY verifies member.login_token, the 11-char
// token off the NICK line, trust-on-first-use -- a mismatch is SE reject 0xCA.
// Changing one does not change the other, which is why the token reset is its
// own checkbox and not something a password change does quietly.
let PW_POLID = null;
function askPassword(polid) {
  PW_POLID = polid;
  $("#pwPolid").textContent = polid;
  $("#pwNew").value = "";
  $("#pwResetToken").checked = false;
  $("#pwModal").classList.add("show");
  $("#pwNew").focus();
}
function closePassword() {
  $("#pwModal").classList.remove("show");
  PW_POLID = null;
}
$("#pwCancel").onclick = closePassword;
$("#pwModal").onclick = (e) => { if (e.target === $("#pwModal")) closePassword(); };
$("#pwNew").onkeydown = (e) => { if (e.key === "Enter") $("#pwGo").click(); };
$("#pwGo").onclick = async () => {
  const polid = PW_POLID, btn = $("#pwGo");
  const pw = $("#pwNew").value.trim(), reset = $("#pwResetToken").checked;
  // Ticking the box alone is a legitimate action -- unlocking somebody out of
  // the lobby without touching a password they still know.
  if (!pw && !reset) return toast("Type a password, or tick the token reset", true);
  if (!viewerTypable(pw)) return;
  btn.disabled = true;
  try {
    const r = await api("/api/account-password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, password: pw, reset_token: reset })
    });
    closePassword();
    toast((r.changed ? `Password changed for ${r.polid}` : `${r.polid}`)
          + (r.token_reset ? " - login token reset, next sign-in re-seeds it" : ""));
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
};

function grantChecks() {
  return [...document.querySelectorAll("#grantChips input")];
}
$("#grantAllBtn").onclick = () => grantChecks().forEach((c) => { c.checked = true; });
$("#grantNoneBtn").onclick = () => grantChecks().forEach((c) => { c.checked = false; });

$("#grantBtn").onclick = async () => {
  const polid = $("#grantPol").value.trim();
  const codes = grantChecks().filter((c) => c.checked).map((c) => +c.value);
  if (!polid) return toast("Which account?", true);
  if (!codes.length) return toast("Pick at least one content", true);
  try {
    const r = await api("/api/grant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, content_codes: codes })
    });
    // `linked` is the count of contents now attached to the handle -- the thing
    // the client's launch gate reads. A grant that links nothing means the
    // account has no handle yet, and the titles will not be playable until it
    // does, so say so rather than reporting a flat success.
    toast(r.linked ? `Granted - ${r.linked} content(s) linked to the handle`
                   : "Granted, but this account has no handle yet, so nothing is playable",
          !r.linked);
    loadAccounts();
  } catch (e) { toast(e.message, true); }
};

$("#revokeBtn").onclick = async () => {
  const polid = $("#grantPol").value.trim();
  const codes = grantChecks().filter((c) => c.checked).map((c) => +c.value);
  if (!polid) return toast("Which account?", true);
  if (!codes.length) return toast("Pick at least one content to revoke", true);
  const names = codes.map((c) => CONTENT[c] || c).join(", ");
  if (!confirm(`Revoke ${names} from ${polid}?\n\nThe character(s) leave the launcher and the client returns to "Create character". A later Grant restores the same Content ID.`)) return;
  try {
    const r = await api("/api/revoke", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, content_codes: codes })
    });
    toast(`Revoked - content ${r.content_codes.join(", ")} cancelled on ${r.polid}`);
    loadAccounts();
  } catch (e) { toast(e.message, true); }
};

// ---- PML editor ----
const stage = $("#stage");
// The page being previewed. Both the server-side expander and the art loader
// need it: a PML `src` is relative to the file that wrote it, so without this
// every include and every image resolves against the wrong tree.
let ACTIVE_FILE = null;
// Pages built from SE's template layer (define/array/for/if/include, &var=) carry
// no positioned markup until it's run, so those go through the server-side
// evaluator first; self-contained pages (the wizard steps) render directly.
// `="...$x..."` is in here because a bare expression in an attribute --
// how SE writes nearly every src, pos and size -- needs the evaluator just
// as much as a <for> does, and a page can carry those and nothing else.
const TEMPLATED = /<(for|if|include|array|define)\b|&var=|&calc=|="[^"]*\$[A-Za-z_]/i;
// Which `show="0"` panel to draw, if any. null = what the client shows on
// arrival, which is the honest default; a name, or "all", to look at the
// panels a page keeps hidden until something reveals them.
let REVEAL = null;
let renderSeq = 0;
// What the server-side expander could not work out for this page: the
// variables it read but nothing defined, and the includes it could not find.
// For a fragment those ARE the explanation of an empty stage.
let EXPAND_REPORT = {};
async function renderPreview() {
  const text = $("#pml").value;
  const seq = ++renderSeq;
  let toRender = text;
  EXPAND_REPORT = {};
  if (TEMPLATED.test(text)) {
    try {
      const r = await fetch("/api/pml-expand", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, path: ACTIVE_FILE || "" }),
      });
      if (r.ok) {
        const j = await r.json();
        if (j && j.text) toRender = j.text;
        EXPAND_REPORT = j || {};
      }
    } catch (e) { /* fall back to rendering the raw text */ }
    if (seq !== renderSeq) return;   // a newer keystroke superseded this one
  }
  try {
    describeRender(PML.render(toRender, stage, ACTIVE_FILE || "",
                              { reveal: REVEAL }));
  }
  catch (e) {
    stage.innerHTML = `<div style="color:#ff8a8a;padding:12px;font:13px monospace">render error: ${e.message}</div>`;
    $("#stageNote").textContent = "";
  }
}

// Which pages pull this file in, and which it pulls in. For a fragment the
// first line is the whole answer to "what is this for" -- it cannot draw itself,
// but it can name the pages it belongs to, and they are one click away.
// Edges are a best-effort index (see services/pmlrefs.py), so this is "probably
// these", not a proof; the wrong link costs one click.
const REF_SHOWN = 8;
async function loadRefs(path) {
  const box = $("#stageRefs");
  box.innerHTML = "";
  if (!path) return;
  let j;
  try { j = await api("/api/pml-refs?path=" + encodeURIComponent(path)); }
  catch (e) { return; }
  if (ACTIVE_FILE !== path) return;      // the operator moved on while we fetched
  const line = (label, rows) => {
    if (!rows.length) return;
    const d = document.createElement("div");
    const l = document.createElement("span");
    l.className = "rl";
    l.textContent = `${label} (${rows.length})`;
    d.appendChild(l);
    const add = (from, to) => rows.slice(from, to).forEach((r) => {
      const a = document.createElement("a");
      a.textContent = r.path.replace(/^_(lang|eras)\/[^/]+\//, "\u2026/");
      a.title = r.path + (r.shape ? `  (${r.shape})` : "");
      a.onclick = () => openPmlFile(r.path);
      d.appendChild(a);
    });
    add(0, REF_SHOWN);
    if (rows.length > REF_SHOWN) {
      const more = document.createElement("span");
      more.className = "more";
      more.textContent = `+${rows.length - REF_SHOWN} more`;
      more.onclick = () => { more.remove(); add(REF_SHOWN, rows.length); };
      d.appendChild(more);
    }
    box.appendChild(d);
  };
  // Composition and navigation are different questions: "built from" is what
  // this page IS, "links to" is where it can send you.
  line("Built from", j.built_from || []);
  line("Included by", j.included_by || []);
  line("Links to", j.links_to || []);
  line("Linked from", j.linked_from || []);
}

// A page stacks its alternate panels in the same 640x480 and hides all but
// one, so the preview shows what the client shows on arrival and offers the
// rest as buttons. Without this `ev11/evpm01.pml` drew sixteen sheets at once.
function renderLayerSwitch(layers) {
  const box = $("#stageLayers");
  box.innerHTML = "";
  if (!layers.length) return;
  const lbl = document.createElement("span");
  lbl.className = "lbl";
  lbl.textContent = `${layers.length} hidden panel${layers.length === 1 ? "" : "s"}:`;
  box.appendChild(lbl);
  const btn = (value, text) => {
    const b = document.createElement("button");
    b.textContent = text;
    if (REVEAL === value) b.className = "on";
    b.onclick = () => { REVEAL = value; renderPreview(); };
    box.appendChild(b);
  };
  btn(null, "on load");
  layers.forEach((name) => btn(name, name));
  btn("all", "all at once");
}

// An empty stage has to say WHY. Most files in the mirror have no layout of
// their own, and a blank 640x480 with no explanation is indistinguishable
// from a renderer that failed -- which is exactly how it was read.
function describeRender(r) {
  const note = $("#stageNote");
  if (!r) { note.textContent = ""; return; }
  renderLayerSwitch(r.layers || []);
  const bits = [];
  if (r.title) bits.push(`&ldquo;${esc(r.title)}&rdquo;`);
  if (r.mode === "page" || r.mode === "layout") {
    bits.push(`<b>${r.placed}</b> element${r.placed === 1 ? "" : "s"} placed`);
    const parts = PARTS[ACTIVE_FILE] || 0;
    if (ACTIVE_FILE) {
      bits.push(parts
        ? `assembled from <b>${parts}</b> other file${parts === 1 ? "" : "s"}`
        : "<b>wholly its own markup</b> - it includes nothing");
    }
    if ((r.layers || []).length && !r.reveal) {
      bits.push(`<b>${r.layers.length}</b> hidden panel`
        + `${r.layers.length === 1 ? "" : "s"} not drawn `
        + " - the client reveals these one at a time");
    }
    if (!r.hasBody) bits.push("no &lt;body&gt; - a fragment a page includes");
  } else if (r.mode === "document") {
    bits.push(`<b>${r.records}</b> text record${r.records === 1 ? "" : "s"}, `
      + "no layout of its own - shown as the page that includes it would");
  } else {
    bits.push("<b>nothing to draw.</b> This file has no markup of its own - "
      + "it holds variables and arrays another page reads (or its markup is "
      + "commented out)");
  }
  // A fragment's <for>/<if> guards read variables its HOST defines, so on
  // its own every branch is skipped. Name them: that turns a blank stage
  // into "open the page that includes this".
  const miss = (EXPAND_REPORT.missing || []).slice(0, 6);
  if (miss.length && r.mode !== "page") {
    bits.push("reads <b>" + miss.map((v) => "$" + esc(v)).join(", ") + "</b>"
      + ((EXPAND_REPORT.missing.length > miss.length) ? " and more" : "")
      + ", which nothing here defines - the page that includes this does");
  }
  const unres = (EXPAND_REPORT.unresolved || []).slice(0, 3);
  if (unres.length) {
    bits.push("include not found: <b>" + unres.map(esc).join(", ") + "</b>");
  }
  note.innerHTML = bits.join(" &middot; ");
}
let renderTimer;
$("#pml").addEventListener("input", () => {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(renderPreview, 120);
});
$("#renderBtn").onclick = renderPreview;

// ---- PML file browser (faceted: location / game / locale) ----
let ALLFILES = [];              // [{path, host, game, locale, dir, name}]
let PML_LIST_READY = Promise.resolve();

const GAME_BY_SEG = {
  ff11: "FFXI", ffxi: "FFXI", tetra: "Tetra Master", fmo: "Front Mission Online",
  fe: "Fantasy Earth", fantasyearth: "Fantasy Earth", dc: "Dirge of Cerberus",
  eq2: "EverQuest II", ff14: "FINAL FANTASY XIV", ffxiv: "FINAL FANTASY XIV",
  jan: "Janhourou", janhourou: "Janhourou", ambrosia: "Ambrosia Odyssey"
};
const LOCALE_RE = /^(en-US|en-GB|ja-JP|fr-FR|de-DE|ja|fr|de)$/i;

// What each file IS, from the server index. Only about one .pml in twenty
// is a whole page -- the rest are the pieces pages are built from -- so
// without this the browser is 3,400 entries that look identical and mostly
// preview as nothing, which reads as a broken renderer.
let SHAPES = {};
// How many <include>s each file writes. A page with none is wholly its own
// markup; one with six is assembled at request time out of six other files,
// and that is the first thing worth knowing about it.
let PARTS = {};
const SHAPE_LABEL = {
  page: "page", layout: "layout", content: "text", data: "data",
};
const SHAPE_NOTE = {
  page: "A whole screen.",
  layout: "Positioned markup with no <body> -- a page includes this.",
  content: "Body copy a page pulls into a <textbox>. Shown as its host would.",
  data: "Variables and arrays only. Nothing here draws -- another page reads it.",
};

function classify(path) {
  const segs = path.split("/");
  let game = null, locale = null;
  for (const s of segs) {
    const g = GAME_BY_SEG[s.toLowerCase()];
    if (g && !game) game = g;
    if (!locale && LOCALE_RE.test(s)) locale = s;
  }
  const slash = path.lastIndexOf("/");
  return {
    path, host: segs[0], game, locale, shape: SHAPES[path] || "",
    parts: PARTS[path] || 0,
    dir: slash < 0 ? "" : path.slice(0, slash),
    name: slash < 0 ? path : path.slice(slash + 1)
  };
}

function fillFacet(sel, values, label) {
  const cur = sel.value;
  sel.innerHTML = "";
  const all = document.createElement("option");
  all.value = ""; all.textContent = `All ${label}`;
  sel.appendChild(all);
  [...values.entries()].sort((a, b) => a[0].localeCompare(b[0])).forEach(([v, n]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = `${v} (${n})`;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

async function loadPmlFileList() {
  try {
    const j = await api("/api/pml-list");
    SHAPES = j.shapes || {};
    PARTS = j.parts || {};
    ALLFILES = j.files.map(classify);
    // build facet counts
    const hosts = new Map(), games = new Map(), locales = new Map();
    const bump = (m, k) => { if (k) m.set(k, (m.get(k) || 0) + 1); };
    ALLFILES.forEach((f) => { bump(hosts, f.host); bump(games, f.game); bump(locales, f.locale); });
    fillFacet($("#fHost"), hosts, "locations");
    fillFacet($("#fGame"), games, "games");
    fillFacet($("#fLocale"), locales, "locales");
    renderFileList();
  } catch (e) { $("#pmlCount").textContent = "unavailable"; }
}

const kindMatches = (kind, shape) =>
  !kind || (kind === "draws" ? shape !== "data" : shape === kind);
const visibleUnderFilter = (path) =>
  kindMatches($("#fKind").value, SHAPES[path] || "");

function renderFileList() {
  const host = $("#fHost").value, game = $("#fGame").value,
    locale = $("#fLocale").value, q = $("#fSearch").value.toLowerCase().trim(),
    sort = $("#fSort").value, kind = $("#fKind").value;
  // The default is "page" -- 180 of 3,499 files, and the only ones that are a
  // whole screen. Everything else in this list is a piece of one, and burying
  // the pages among them is what made the browser feel like it was broken.
  const kindOk = (f) => kindMatches(kind, f.shape);
  let rows = ALLFILES.filter((f) =>
    (!host || f.host === host) && (!game || f.game === game) &&
    (!locale || f.locale === locale) && kindOk(f) &&
    (!q || f.path.toLowerCase().includes(q)));
  rows.sort((a, b) => sort === "name"
    ? a.name.localeCompare(b.name) || a.path.localeCompare(b.path)
    : a.path.localeCompare(b.path));
  // Never a bare filtered count: 3,499 files and 180 pages is the single
  // most surprising fact about this mirror, so the filter says so.
  $("#pmlCount").textContent = kind === "page"
    ? ` - ${rows.length.toLocaleString()} pages of ${ALLFILES.length.toLocaleString()} files; the rest are the pieces they are built from`
    : ` - ${rows.length.toLocaleString()} of ${ALLFILES.length.toLocaleString()}`;

  const list = $("#fileList");
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<div class="empty">No files match those filters.</div>`; return; }
  const CAP = 1200, shown = rows.slice(0, CAP);
  let curDir = null, group = null;
  const frag = document.createDocumentFragment();
  shown.forEach((f) => {
    if (f.dir !== curDir) {
      curDir = f.dir;
      group = document.createElement("div"); group.className = "filegroup";
      const d = document.createElement("div"); d.className = "dir";
      d.textContent = f.dir || "(root)";
      group.appendChild(d); frag.appendChild(group);
    }
    const it = document.createElement("div");
    it.className = "fileitem" + (f.path === ACTIVE_FILE ? " active" : "");
    // A page says how it was made; a fragment says what kind of piece it is.
    const made = f.shape !== "page" ? null
      : f.parts ? { cls: "built", text: `built from ${f.parts}` }
                : { cls: "original", text: "original" };
    const badges =
      (made ? `<span class="badge ${made.cls}">${made.text}</span>` : "")
      + [f.shape === "page" ? null : SHAPE_LABEL[f.shape], f.game, f.locale]
        .filter(Boolean).map((b) => `<span class="badge">${b}</span>`).join("");
    it.innerHTML = `<span>${f.name}</span><span class="badges">${badges}</span>`;
    it.onclick = () => openPmlFile(f.path);
    group.appendChild(it);
  });
  list.appendChild(frag);
  if (rows.length > CAP) {
    const more = document.createElement("div"); more.className = "empty";
    more.textContent = `…and ${rows.length - CAP} more - refine the filters to narrow.`;
    list.appendChild(more);
  }
}

async function openPmlFile(path) {
  if (!path) return;
  try {
    const r = await fetch("/api/pml-load?path=" + encodeURIComponent(path));
    const txt = await r.text();
    if (!r.ok) throw new Error(txt);
    $("#pml").value = txt;
    $("#pmlKind").textContent = "decoded: " + (r.headers.get("X-PML-Kind") || "?");
    ACTIVE_FILE = path;
    REVEAL = null;              // a new page starts as the client shows it
    writeHash("pml", path);
    // Opening a page's part from the "Built from" links lands on a file the
    // default Pages filter hides. Widen it rather than showing a list with no
    // selection in it -- following a link should never look like nothing moved.
    // ...but not before the index arrives: with SHAPES empty every file
    // looks invisible, and restoring from a hash would clear the filter.
    if (Object.keys(SHAPES).length && !visibleUnderFilter(path)) {
      $("#fKind").value = "";
    }
    renderFileList();
    renderPreview();
    loadRefs(path);
    toast("Opened " + path);
  } catch (e) { toast("open failed: " + e.message, true); }
}

["fHost", "fGame", "fLocale", "fSort", "fKind"].forEach((id) => $("#" + id).addEventListener("change", renderFileList));
let searchTimer;
$("#fSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderFileList, 140); });

$("#loadBtn").onclick = async () => {
  try {
    const kinou = $("#liveKinou").value, step = $("#liveStep").value;
    const r = await fetch(`/api/page?kinou_id=${kinou}&step=${step}`);
    const txt = await r.text();
    if (!r.ok) throw new Error(txt);
    $("#pml").value = txt;
    ACTIVE_FILE = null;         // generated, not a file -- art falls back to ART_ROOT
    $("#stageRefs").innerHTML = "";
    renderFileList();
    renderPreview();
    toast(`Loaded kinou ${kinou} step ${step}`);
  } catch (e) { toast("load failed: " + e.message, true); }
};

// ---- news ----
// One store, four output shapes. The panel edits the store and previews what a
// publish would write; nothing here touches the served tree until Publish, so
// a half-typed announcement can never reach a client.
let NEWS = [];            // the store, newest first -- the order players see
let NEWS_SEL = 0;         // index of the entry the form is bound to
let NEWS_META = null;     // kinds/contents/categories, straight from newsgen.py
let NEWS_ART = null;      // SE's own picker art as data URIs, from /api/news/art
let NEWS_DIRTY = false;
let NEWS_DRAFT_KEY = null;   // per-store, so a dev draft cannot land on prod
let NEWS_DRAFT_TIMER = null;

// --- draft autosave ---------------------------------------------------------
// Everything above lives in memory until Save, so a refresh, a crash or a
// closed tab lost the lot. Drafts go to localStorage rather than to the server
// on a timer, deliberately: the store IS what Publish reads, so autosaving
// there would put half-typed headlines one click away from a client. This keeps
// the Save/Publish gate exactly as it was and only protects the typing.
function newsDraftSave() {
  if (!NEWS_DRAFT_KEY) return;
  try {
    localStorage.setItem(NEWS_DRAFT_KEY, JSON.stringify({
      at: Date.now(), sel: NEWS_SEL, items: NEWS,
      // what the store held when this draft started, so a draft written against
      // a since-changed store can say so instead of silently reverting it
      base: NEWS_BASE,
    }));
    const t = new Date();
    $("#newsDraftHint").textContent = "draft saved "
      + String(t.getHours()).padStart(2, "0") + ":"
      + String(t.getMinutes()).padStart(2, "0");
  } catch (e) { /* private mode, or full: drafts are a bonus, not a dependency */ }
}

function newsDraftClear() {
  if (!NEWS_DRAFT_KEY) return;
  try { localStorage.removeItem(NEWS_DRAFT_KEY); } catch (e) { /* as above */ }
  $("#newsDraftHint").textContent = "";
  $("#newsDraftNote").style.display = "none";
}

function newsDraftRead() {
  if (!NEWS_DRAFT_KEY) return null;
  try {
    const d = JSON.parse(localStorage.getItem(NEWS_DRAFT_KEY) || "null");
    return d && Array.isArray(d.items) ? d : null;
  } catch (e) { return null; }
}

let NEWS_BASE = "";          // JSON of the store as last loaded or saved

function newsMarkDirty() {
  NEWS_DIRTY = true;
  $("#newsSaveBtn").classList.add("act");
  // Debounced: this fires on every keystroke in the body textarea.
  clearTimeout(NEWS_DRAFT_TIMER);
  NEWS_DRAFT_TIMER = setTimeout(newsDraftSave, 400);
}

async function loadNews() {
  try {
    const j = await api("/api/news");
    NEWS = j.items || [];
    NEWS_META = j;
    NEWS_DIRTY = false;
    $("#newsSaveBtn").classList.remove("act");

    // SE's own art for the two pickers. Fetched once, and failure is not fatal
    // -- the strips fall back to text, which is what the old <select>s were.
    if (NEWS_ART === null) {
      try { NEWS_ART = (await api("/api/news/art")).art || {}; }
      catch (e) { NEWS_ART = {}; }
    }
    buildNewsPickers();

    const st = $("#newsState");
    if (!j.writable) {
      st.style.display = "";
      st.className = "note warn";
      st.innerHTML = `<b>${esc(j.www)} is mounted read-only</b>, so Publish will
        refuse. Drop <code class="mono">:ro</code> from the admin service&rsquo;s
        <code class="mono">www</code> volume and recreate the container.`;
    } else {
      st.style.display = "none";
    }
    $("#newsStoreHint").textContent = j.using_store
      ? `store: ${j.store}`
      : `no store yet - showing the shipped seed (${j.seed}); the first save forks it to ${j.store}`;

    // Keyed by the store this panel writes, so a draft typed against a dev
    // store is never offered on prod.
    NEWS_DRAFT_KEY = "pol-admin:news-draft:" + (j.store || "?");
    NEWS_BASE = JSON.stringify(NEWS);
    newsRestoreDraft();

    if (NEWS_SEL >= NEWS.length) NEWS_SEL = Math.max(0, NEWS.length - 1);
    renderNewsList();
    bindNewsForm();
    loadNewsOutputs();
  } catch (e) { toast("news: " + e.message, true); }
}

// Put a draft back on screen, if there is one and it still says something the
// store does not. Restoring is silent-but-announced rather than a modal: the
// work is already yours, and a prompt on every load would train you to dismiss
// it. Discard is one click away in the banner.
function newsRestoreDraft() {
  const d = newsDraftRead();
  const note = $("#newsDraftNote");
  if (!d) { note.style.display = "none"; return; }
  if (JSON.stringify(d.items) === NEWS_BASE) {
    newsDraftClear();                 // already saved; nothing to restore
    return;
  }
  NEWS = d.items;
  NEWS_SEL = Math.min(d.sel || 0, Math.max(0, NEWS.length - 1));
  NEWS_DIRTY = true;
  $("#newsSaveBtn").classList.add("act");

  const when = new Date(d.at || Date.now());
  const moved = d.base !== undefined && d.base !== NEWS_BASE;
  note.style.display = "";
  note.className = moved ? "note warn" : "note";
  note.innerHTML =
    `<b>Unsaved draft restored</b> from ${esc(when.toLocaleString())} - it was never saved to the store, so nothing has reached a client.
     ${moved ? `<b>The stored announcements have changed since you typed it</b>,
       so saving will replace what is there now. ` : ""}
     <button class="ghost" id="newsDraftDiscard"
             style="padding:3px 10px; font-size:12px; margin-left:6px">Discard
       draft</button>`;
  $("#newsDraftDiscard").onclick = () => {
    newsDraftClear();
    loadNews();                       // straight back to the stored version
  };
}

function renderNewsList() {
  const box = $("#newsList");
  if (!NEWS.length) {
    box.innerHTML = `<div class="empty">No announcements. “New announcement” starts one.</div>`;
    return;
  }
  box.innerHTML = NEWS.map((it, i) => {
    const cat = NEWS_META ? (NEWS_META.categories[NEWS_META.kinds[it.kind].category] || "") : "";
    const badges = [
      `<span class="badge">${esc(cat)}</span>`,
      it.status ? `<span class="badge built">status</span>` : "",
      (it.body || "").trim() ? "" : `<span class="badge original">no body</span>`,
    ].join("");
    return `<div class="fileitem${i === NEWS_SEL ? " active" : ""}" data-i="${i}">
      <span>${esc(it.title)}</span>
      <span class="badges">${badges}<span class="badge">${esc(it.date)}</span></span>
    </div>`;
  }).join("");
  box.querySelectorAll(".fileitem").forEach((el) => {
    el.onclick = () => { readNewsForm(); NEWS_SEL = +el.dataset.i; renderNewsList(); bindNewsForm(); };
  });
}

// --- the two pickers -------------------------------------------------------
// Kind and Service each choose a piece of SE's own art and a place the story
// lands, and a <select> showed neither. These draw the actual icon the client
// will render, which is the only way an operator finds out BEFORE publishing
// that "Extras" wears a Front Mission Online badge.
function newsArt(key) {
  const src = (NEWS_ART || {})[key];
  return src ? `<img src="${src}" alt="">` : "";
}

function buildNewsPickers() {
  if (!NEWS_META) return;
  const kinds = $("#nKindStrip");
  kinds.innerHTML = Object.entries(NEWS_META.kinds).map(([k, v]) => {
    const cat = (NEWS_META.categories || [])[v.category] || "?";
    const art = newsArt("marker:" + v.marker);
    return `<button type="button" class="pick" data-kind="${esc(k)}"
              title="${esc(v.note || "")}">
      <span class="art">${art || `<span class="sub">&lt;${esc(v.marker)}&gt;</span>`}</span>
      <span class="nm">${esc(k)}</span>
      <span class="sub">${esc(cat)}</span>
    </button>`;
  }).join("");
  kinds.querySelectorAll(".pick").forEach((el) => {
    el.onclick = () => pickNews("kind", el.dataset.kind);
  });

  const conts = $("#nContentStrip");
  // Ordered by content id, not by however the JSON happened to arrive: the
  // tiles read as a numbered list ("id 8", "id 9") and dict order silently put
  // them out of sequence once two services swapped ids.
  const byId = Object.entries(NEWS_META.contents).sort(
    (a, b) => (NEWS_META.content_ids[a[0]] || 0) - (NEWS_META.content_ids[b[0]] || 0));
  conts.innerHTML = byId.map(([k, label]) => {
    const info = (NEWS_META.content_icons || {})[k] || {};
    const id = (NEWS_META.content_ids || {})[k];
    const art = newsArt("ticker:" + k);
    return `<button type="button" class="pick" data-content="${esc(k)}"
              title="content id ${id} - ticker badge and news${id}.pml">
      <span class="art">${art ? `<span class="plate">${art}</span>`
                              : `<span class="sub">id ${id}</span>`}</span>
      <span class="nm">${esc(label)}</span>
      <span class="sub"${info.differs ? ' style="color:var(--warn)"' : ""}>${
        info.differs ? "badge says " + esc(info.short) : "id " + id}</span>
    </button>`;
  }).join("");
  conts.querySelectorAll(".pick").forEach((el) => {
    el.onclick = () => pickNews("content", el.dataset.content);
  });
}

// The strips write straight to the model -- there is no hidden <input> to keep
// in step, so readNewsForm() deliberately does not touch kind/content.
function pickNews(field, value) {
  const it = NEWS[NEWS_SEL];
  if (!it || it[field] === value) return;
  it[field] = value;
  newsMarkDirty();
  renderNewsList();
  markNewsPicks();
  showIconHint();
  renderNewsEffect();
}

function markNewsPicks() {
  const it = NEWS[NEWS_SEL] || {};
  $("#nKindStrip").querySelectorAll(".pick").forEach((el) =>
    el.classList.toggle("on", el.dataset.kind === (it.kind || "info")));
  $("#nContentStrip").querySelectorAll(".pick").forEach((el) =>
    el.classList.toggle("on", el.dataset.content === (it.content || "playonline")));
}

function bindNewsForm() {
  const it = NEWS[NEWS_SEL];
  $("#newsForm").style.display = it ? "" : "none";
  if (!it) return;
  $("#nDate").value = it.date || "";
  $("#nSerial").value = it.serial || "(on save)";
  $("#nTitle").value = it.title || "";
  $("#nLink").value = it.link || "";
  $("#nStatus").checked = !!it.status;
  $("#nBody").value = it.body || "";
  markNewsPicks();
  showIconHint();
  showDateHint();
  renderNewsEffect();
}

// The service choice is also the ticker icon: main/index.pml uses the content
// id verbatim as the sprite sequence. Spell out which icon that lands on,
// because for one value the two SE tables disagree -- id 5 is "Extras" in the
// Information section's switcher and FMO in the icon strip.
function showIconHint() {
  const el = $("#nIconHint");
  if (!el || !NEWS_META) return;
  const key = (NEWS[NEWS_SEL] || {}).content || "playonline";
  const info = (NEWS_META.content_icons || {})[key];
  const label = (NEWS_META.contents || {})[key];
  if (!info) { el.textContent = ""; return; }
  // Counted, never written down: this hint went stale the first time the
  // service list grew, still claiming "only these five exist" next to seven.
  const n = Object.keys(NEWS_META.contents || {}).length;
  el.innerHTML = info.differs
    ? `SE reuses this content id for two different things: the Information
       section calls it &ldquo;${esc(label)}&rdquo;, but the login ticker draws
       <b>${esc(info.icon)}</b>. Both are SE&rsquo;s own tables; neither is
       ours to fix.`
    : `${n} services. The id on each tile is a <b>sequence</b> number in
       <code class="mono">ma_i/maic06i.ang</code>, and a sequence points at an
       image - it is not a frame index, which is why the ids are not
       contiguous. 6 and 8 are missing because SE&rsquo;s sequences there draw a
       blank badge, and 5 deliberately reuses the PlayOnline one. Ids 1-10
       are SE&rsquo;s own; 11 and 12 were appended by
       <code class="mono">tools/make_news_badges.py</code> from the
       client&rsquo;s <code class="mono">data/icon/cicn_*</code> badge art.`;
}

// --- "where this shows up" -------------------------------------------------
// Every surface a single entry reaches, restated from the CURRENT form values.
// The numbers behind each line are newsgen's, so this cannot drift from what
// Publish actually writes.
function renderNewsEffect() {
  const box = $("#nEffect");
  const it = NEWS[NEWS_SEL];
  if (!box || !it || !NEWS_META) return;
  const k = NEWS_META.kinds[it.kind] || {};
  const cat = (NEWS_META.categories || [])[k.category] || "?";
  const label = (NEWS_META.contents || {})[it.content] || it.content;
  const cid = (NEWS_META.content_ids || {})[it.content];
  const link = (it.link || "").trim();
  const body = (it.body || "").trim();
  const badge = newsArt("ticker:" + it.content);
  const marker = newsArt("marker:" + k.marker);
  const title = esc(it.title || "(no headline yet)");

  // $aMORE, straight out of newsgen.feed(): 2 = the raw link, 1 = our detail
  // page, 0 = nothing, which is a headline the player cannot click through.
  const target = link
    ? `opens <span class="mono">${esc(link)}</span>`
    : (body ? `opens the detail page <span class="mono">${it.serial || "…"}.pml</span>`
            : `<b style="color:var(--warn)">clicks through to nothing</b> - give it a
               body or a link, or the headline is inert`);

  const pages = cid === 1
    ? "every Information page - a PlayOnline story is carried by all of them"
    : `the <b>${esc(label)}</b> page and <b>View All</b>`;

  const rows = [
    ["Login ticker", `<div class="tickerline">${
        badge ? `<span class="plate">${badge}</span>` : ""
      }<span class="hd">${title}</span></div>
      <div class="sub" style="margin-top:5px; color:var(--muted); font-size:11.5px">${target}</div>`, true],
    ["Information section",
      `${marker ? `<span class="plate" style="vertical-align:middle; margin-right:6px">${marker}</span>` : ""}
       filed under <b>${esc(cat)}</b>, on ${pages}.`, true],
    ["Detail page",
      body ? `<span class="mono">${it.serial || "…"}.pml</span> is written with the body below.`
           : (link ? "not written - the external link takes its place."
                   : "not written - the body is empty."),
      !!body],
    ["Status / Maintenance",
      it.status ? `listed in the &ldquo;current or recently resolved&rdquo; panel
                   as <b>${k.maint === 2 ? "trouble" : k.maint === 3 ? "maintenance" : "other"}</b>.`
                : "not listed.", !!it.status],
  ];

  box.innerHTML = `<h4>Where this shows up</h4>` + rows.map(([w, v, on]) =>
    `<div class="er${on ? "" : " off"}"><span class="w">${w}</span>
       <span class="v">${v}</span></div>`).join("")
    + `<div class="er off"><span class="w">Not used</span><span class="v">Kind also
        sets the ticker&rsquo;s <span class="mono">$aSTAMP</span> field, which
        <b>no page reads</b> - <span class="mono">pml/main/index.pml</span> is the only
        consumer of <span class="mono">$LATESTNEWS</span> and it never touches field 6.
        The date is not read there either; it shows on the Information and detail
        pages only.</span></div>`;
}

// --- the date --------------------------------------------------------------
// SE's own format, measured off the captured en-US news1.pml: "Sep. 16, 2025
// 20:35 [PDT]" -- abbreviated month WITH a period except May, day NOT zero
// padded, 24-hour clock, zone in brackets. The field stays free text because
// the client only ever echoes it, but nobody should have to retype that shape.
const NEWS_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
                     "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."];
const NEWS_DATE_RE =
  /^(Jan\.|Feb\.|Mar\.|Apr\.|May|Jun\.|Jul\.|Aug\.|Sep\.|Oct\.|Nov\.|Dec\.) \d{1,2}, \d{4} \d{2}:\d{2} \[[A-Z]{2,5}\]$/;

function newsDateString(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${NEWS_MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} [UTC]`;
}

function setNewsDate(str) {
  $("#nDate").value = str;
  readNewsForm();
  newsMarkDirty();
  renderNewsList();
  showDateHint();
}

// Floors to the hour rather than rounding to the nearest one: a story stamped
// 20:00 while it is 19:47 is announcing itself in the future.
$("#nDateNow").onclick = () => {
  const d = new Date();
  d.setUTCMinutes(0, 0, 0);
  setNewsDate(newsDateString(d));
  $("#nDatePick").value = "";
};
$("#nDatePick").addEventListener("change", (e) => {
  if (!e.target.value) return;
  // A datetime-local reads as local wall-clock; the operator picked the time
  // they mean to publish, so take those digits as the UTC stamp verbatim
  // rather than shifting them by the browser's offset.
  setNewsDate(newsDateString(new Date(e.target.value + ":00Z")));
});

function showDateHint() {
  const el = $("#nDateHint");
  if (!el) return;
  const v = $("#nDate").value.trim();
  if (!v) { el.innerHTML = "Shown as typed on the Information and detail pages."; return; }
  el.innerHTML = NEWS_DATE_RE.test(v)
    ? `Matches SE&rsquo;s own format.`
    : `Not SE&rsquo;s format - theirs reads
       <code class="mono">${esc(newsDateString(new Date()))}</code>. Yours is
       served as typed, so this is a house-style warning, not an error.`;
}

// Pull the form back into the model. Called before anything that changes which
// entry is selected, so switching rows never silently drops an edit.
// kind/content are absent on purpose: the pickers set them directly.
function readNewsForm() {
  const it = NEWS[NEWS_SEL];
  if (!it) return;
  it.date = $("#nDate").value;
  it.title = $("#nTitle").value;
  it.link = $("#nLink").value.trim();
  it.status = $("#nStatus").checked;
  it.body = $("#nBody").value;
}

["nDate", "nTitle", "nLink", "nBody"].forEach((id) =>
  $("#" + id).addEventListener("input", () => {
    readNewsForm(); newsMarkDirty(); renderNewsList(); renderNewsEffect();
    if (id === "nDate") showDateHint();
  }));
$("#nStatus").addEventListener("change", () => {
  readNewsForm(); newsMarkDirty(); renderNewsList(); renderNewsEffect();
});

// The debounce above means the last few keystrokes before a refresh would not
// be in the draft yet. `pagehide` fires on reload, navigation and tab close
// (unlike `beforeunload`, it is reliable on mobile Safari), so flush there --
// the whole point of this feature is the accidental refresh.
window.addEventListener("pagehide", () => {
  if (!NEWS_DIRTY || !NEWS_DRAFT_KEY) return;
  clearTimeout(NEWS_DRAFT_TIMER);
  readNewsForm();
  newsDraftSave();
});

$("#newsAddBtn").onclick = () => {
  readNewsForm();
  // Newest first is the order the ticker shows, and the generator does not
  // sort -- "newest" is not recoverable from a date string SE never parsed.
  NEWS.unshift({ date: "", title: "New announcement", kind: "info",
                 content: "playonline", body: "", status: false, link: "" });
  NEWS_SEL = 0;
  newsMarkDirty();
  renderNewsList();
  bindNewsForm();
  $("#nTitle").focus();
  $("#nTitle").select();
};

$("#nDelBtn").onclick = () => {
  const it = NEWS[NEWS_SEL];
  if (!it) return;
  if (!confirm(`Delete “${it.title}”?\n\nIts detail page is removed on the next publish.`)) return;
  NEWS.splice(NEWS_SEL, 1);
  NEWS_SEL = Math.min(NEWS_SEL, NEWS.length - 1);
  newsMarkDirty();
  renderNewsList();
  bindNewsForm();
};

const newsMove = (d) => () => {
  readNewsForm();
  const j = NEWS_SEL + d;
  if (j < 0 || j >= NEWS.length) return;
  [NEWS[NEWS_SEL], NEWS[j]] = [NEWS[j], NEWS[NEWS_SEL]];
  NEWS_SEL = j;
  newsMarkDirty();
  renderNewsList();
};
$("#nUpBtn").onclick = newsMove(-1);
$("#nDownBtn").onclick = newsMove(1);

async function saveNews(quiet) {
  readNewsForm();
  const j = await api("/api/news", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: NEWS }),
  });
  NEWS = j.items || [];          // comes back serial-stamped
  NEWS_DIRTY = false;
  $("#newsSaveBtn").classList.remove("act");
  // The store now holds this, so the draft has nothing left to protect.
  NEWS_BASE = JSON.stringify(NEWS);
  clearTimeout(NEWS_DRAFT_TIMER);
  newsDraftClear();
  renderNewsList();
  bindNewsForm();
  loadNewsOutputs();
  if (!quiet) toast(`Saved ${NEWS.length} announcement(s)`);
  return j;
}

$("#newsSaveBtn").onclick = async () => {
  try { await saveNews(); } catch (e) { toast("save failed: " + e.message, true); }
};

async function newsPublish(dry) {
  try {
    // Publish reads the STORE, not the form, so an unsaved edit would publish
    // the previous text. Save first rather than publishing something the
    // operator can see on screen but did not commit.
    if (NEWS_DIRTY) await saveNews(true);
    const r = await api("/api/news/publish", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: !!dry }),
    });
    const bits = [`${r.written.length} file(s)`];
    if (r.pruned.length) bits.push(`${r.pruned.length} removed`);
    if (r.backed_up.length) bits.push(`${r.backed_up.length} kept as .se-orig`);
    toast((dry ? "Would write " : "Published ") + bits.join(", "));
    if (dry) {
      $("#newsViewRaw").click();
      $("#newsRaw").textContent =
        [`${r.written.length} of ${r.total} file(s) would change:`,
         ...r.written.map((p) => "  " + p),
         r.pruned.length ? "\nwould remove (announcement deleted):" : "",
         ...r.pruned.map((p) => "  " + p),
         r.backed_up.length ? "\nwould keep a .se-orig copy of:" : "",
         ...r.backed_up.map((p) => "  " + p),
        ].filter(Boolean).join("\n");
    } else {
      loadNewsOutputs();
    }
  } catch (e) { toast("publish failed: " + e.message, true); }
}
$("#newsPublishBtn").onclick = () => newsPublish(false);
$("#newsDryBtn").onclick = () => newsPublish(true);

async function loadNewsOutputs() {
  try {
    const j = await api("/api/news/outputs");
    const sel = $("#newsOutput");
    const keep = sel.value;
    sel.innerHTML = "";
    // Only en-US is offered: every other locale gets a byte-identical copy of
    // the same text, so listing all five would be forty near-duplicate rows.
    for (const f of j.files) {
      if (!/\/(en-US)\//.test(f.path) && !/snews\/en-US\//.test(f.path)
          && !f.path.endsWith("pml/info/news0.pml")) continue;
      sel.add(new Option(`${f.path}  (${f.bytes} B)`, f.path));
    }
    if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
    else {
      // Default to the page that actually draws something.
      const page = [...sel.options].find((o) => o.value.includes("snews/"));
      if (page) sel.value = page.value;
    }
    showNewsOutput();
  } catch (e) { /* the tab is still usable without the preview */ }
}

let NEWS_VIEW = "render";
async function showNewsOutput() {
  const path = $("#newsOutput").value;
  if (!path) return;
  let text = "";
  try {
    const r = await fetch("/api/news/preview?path=" + encodeURIComponent(path));
    text = await r.text();
    if (!r.ok) throw new Error(text);
  } catch (e) { text = "preview failed: " + e.message; }
  $("#newsRaw").textContent = text;
  // Only the substitute Information page is a page. The rest are data arrays
  // SE's own pages read, so there is nothing for the renderer to draw and
  // pretending otherwise would show a convincing blank stage.
  const drawable = /snews\//.test(path);
  $("#newsViewRender").disabled = !drawable;
  const view = drawable ? NEWS_VIEW : "raw";
  $("#newsPreviewWrap").style.display = view === "render" ? "" : "none";
  $("#newsRaw").style.display = view === "render" ? "none" : "";
  $("#newsViewRender").classList.toggle("on", view === "render");
  $("#newsViewRaw").classList.toggle("on", view !== "render");
  if (view === "render") {
    try {
      describeRenderInto(PML.render(text, $("#newsStage"), path, {}), "#newsNote");
    } catch (e) {
      $("#newsStage").innerHTML =
        `<div style="color:#ff8a8a;padding:12px;font:13px monospace">render error: ${esc(e.message)}</div>`;
    }
  }
}
$("#newsOutput").addEventListener("change", showNewsOutput);
$("#newsViewRender").onclick = () => { NEWS_VIEW = "render"; showNewsOutput(); };
$("#newsViewRaw").onclick = () => { NEWS_VIEW = "raw"; showNewsOutput(); };

// A one-line "what did the renderer actually find" note. The PML tab's own
// describeRender() writes into #stageNote and reads ACTIVE_FILE, so it cannot
// be reused here; this is the same answer in the same field names PML.render
// returns (mode / placed / records / title).
function describeRenderInto(r, sel) {
  const el = $(sel);
  if (!el) return;
  if (!r) { el.textContent = ""; return; }
  const bits = [];
  if (r.title) bits.push(`&ldquo;${esc(r.title)}&rdquo;`);
  if (r.mode === "page" || r.mode === "layout") {
    bits.push(`<b>${r.placed}</b> element${r.placed === 1 ? "" : "s"} placed`);
  } else if (r.mode === "document") {
    bits.push(`<b>${r.records}</b> text record${r.records === 1 ? "" : "s"}`);
  } else {
    bits.push("<b>nothing to draw</b> - this file is data another page reads");
  }
  el.innerHTML = bits.join(" &middot; ");
}

// ---- boot ----
(async function () {
  loadSession();
  await loadContentNames();
  loadCodes();
  PML_LIST_READY = loadPmlFileList();
  // Seed the editor with a tiny sample so the preview isn't blank -- but not
  // when the hash names a file, or the restore would flash the sample first.
  if (!parseHash().path) $("#pml").value =
    `<pml><head>\n  <style name="hdr" face="6" size="18" color="#ffffffff">\n</head>\n<body>\n  <text pos="30,30" size="400,24" style="hdr">PlayOnline PML preview</text>\n  <scrollarea pos="30,70" size="200,24" skin="0" bgcolor="#efe9dcff" skincolor="#00000000"></scrollarea>\n  <input name="demo" type="text" pos="30,70" size="200,24" style="hdr" skin="1" skincolor="#efe9dcff" value="type here">\n</body></pml>`;
  if (!parseHash().path) renderPreview();
  applyHash();          // restore the tab, and the file if the hash names one
})();
