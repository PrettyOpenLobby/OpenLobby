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

// ---- who is signed in, and what they may do ----
// The SERVER enforces all of this (admin._permit); hiding things here only
// keeps a moderator from clicking into a 403.
const isOwner = () => SESSION.role !== "mod";
const can = (p) => isOwner() || (SESSION.perms || []).includes(p);
const TAB_PERM = { accounts: "accounts_view", codes: "codes", reports: "reports",
                   issues: "reports", gmcalls: "gm", news: "owner", pml: "owner" };
const tabAllowed = (t) => !TAB_PERM[t] || (TAB_PERM[t] === "owner" ? isOwner() : can(TAB_PERM[t]));
const visibleTabs = () => TABS.filter(tabAllowed);

function applyRole() {
  document.querySelectorAll("nav button").forEach((b) => { b.hidden = !tabAllowed(b.dataset.tab); });
  document.querySelectorAll("[data-owner]").forEach((el) => { el.hidden = !isOwner(); });
  document.querySelectorAll("[data-perm]").forEach((el) => { el.hidden = !can(el.dataset.perm); });
  $("#mePanel").hidden = isOwner();
  const sec = document.querySelector('nav button[data-tab="security"]');
  sec.textContent = isOwner() ? "Security" : "My account";
}

let CUR_TAB = "";
function showTab(name) {
  if (!TABS.includes(name) || !tabAllowed(name)) name = visibleTabs()[0] || "overview";
  CUR_TAB = name;
  document.querySelectorAll("nav button").forEach(
    (x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(
    (x) => x.classList.toggle("active", x.id === "tab-" + name));
  if (name !== "gmcalls") stopGmDesk();
  document.body.classList.toggle("wide", name === "pml");
  if (name === "pml") setTimeout(fitStage, 0);
  loadTab(name);
  return name;
}

// What each tab fetches when it is opened -- and again on Refresh / R.
function loadTab(name, refresh) {
  if (name === "overview") loadOverview();
  if (name === "codes") loadCodes();
  if (name === "accounts") loadAccounts();
  if (name === "reports") loadReports().then(() => markSeen("reports"));
  if (name === "issues") loadIssues().then(() => markSeen("issues"));
  if (name === "gmcalls") { loadGmCalls(); startGmDesk(); renderAlertBtn(); }
  if (name === "news") loadNews();
  // The PML index is big and cached server-side; fetched at boot, and again
  // only when asked.
  if (name === "pml" && refresh) PML_LIST_READY = loadPmlFileList();
  if (name === "security") {
    loadSession(); loadSessions();
    if (isOwner()) { loadMods(); loadAudit(); loadAlerts(); }
  }
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
    who.innerHTML = `<span title="${SESSION.remember
        ? "Remembered on this device: stays signed in across restarts"
        : "Signed in until this browser closes"}">signed in as ` +
      `<b style="color:var(--text)">${esc(SESSION.user)}</b>` +
      (SESSION.role === "mod" ? ` <span class="pill open">moderator</span>` : "") + `</span>`;
    const out = document.createElement("button");
    out.textContent = "Sign out";
    out.onclick = async () => {
      try { await api("/api/logout", { method: "POST" }); location.reload(); }
      catch (e) { toast(e.message, true); }
    };
    who.appendChild(out);
  } else {
    who.innerHTML = `<span title="Anyone who can reach this address has full access.">no password set</span>`;
  }

  const authOn = !!SESSION.auth_required;
  $("#secCurrentWrap").style.display = authOn ? "" : "none";
  $("#secUser").value = SESSION.user || "admin";
  const st = $("#secState");
  if (!authOn) {
    st.className = "note warn";
    st.innerHTML = `<b>No password is set.</b> Anyone who can reach this address has
      full access. Set one below.`;
  } else if (SESSION.credential_set) {
    const when = (SESSION.updated_at || "").slice(0, 19).replace("T", " ");
    st.className = "note";
    st.innerHTML = `Password last changed ${when ? when + " UTC" : "(unknown)"}.`
      + (SESSION.env_override ? ` An override password is also set in the server
        environment (<code class="mono">POL_ADMIN_PASSWORD</code>).` : "");
  } else {
    st.className = "note warn";
    st.innerHTML = `Signed in with the environment override password. Set a password
      here to manage it from the panel.`;
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
    toast("Saved. Your other browsers were signed out.");
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
    ? "Format XXXX-XXXX-XXXX-XXXX-XXXX. Leave blank for a random code."
    : short < CODE_GROUPS * CODE_GLEN
      ? `${CODE_GROUPS * CODE_GLEN - short} more character(s).`
      : "Complete.";
});

// ---- small shared helpers ----
const fmtWhen = (iso) => String(iso || "").slice(0, 16).replace("T", " ");
const isoMs = (iso) => Date.parse(iso || "") || 0;
function ago(ms) {
  if (!ms) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  if (s < 30 * 86400) return Math.round(s / 86400) + " d ago";
  return new Date(ms).toLocaleDateString();
}
const store = {
  get(k, d) { try { const v = localStorage.getItem("pol-admin:" + k); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("pol-admin:" + k, v); } catch (e) {} },
};

// The panel is plain http on a tailnet IP, which is not a "secure context", so
// navigator.clipboard is often simply absent. Fall back to a selected textarea.
async function copyText(text, what) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {}
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e) {}
    ta.remove();
  }
  toast(ok ? `Copied ${what}` : "Copy failed. Select the text and copy it manually.", !ok);
}

function downloadCsv(name, header, rows) {
  const cell = (v) => {
    const t = String(v ?? "");
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const text = [header, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// A segmented control: `.seg` with data-f buttons. Remembers its choice.
function segControl(sel, key, def, onChange) {
  const box = $(sel);
  let cur = store.get(key, def);
  const paint = () => box.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("on", b.dataset.f === cur));
  box.onclick = (e) => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    cur = b.dataset.f; store.set(key, cur); paint(); onChange();
  };
  paint();
  return () => cur;
}

function flash(el) {
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}

// ---- codes ----
let CODES = [];
const codeUsed = (r) => !!(r.redeemed_by || r.redeemed_at);
const codeFilter = segControl("#codeFilter", "codeFilter", "unused", () => renderCodes());

async function loadCodes() {
  try { CODES = await api("/api/codes"); renderCodes(); }
  catch (e) { toast(e.message, true); }
}

function codesShown() {
  const f = codeFilter(), q = $("#codeSearch").value.trim().toLowerCase();
  return CODES.filter((r) =>
    (f === "all" || (f === "used") === codeUsed(r)) &&
    (!q || [r.code, r.note, r.contents_label, r.redeemed_by]
      .some((v) => String(v || "").toLowerCase().includes(q))));
}

function renderCodes() {
  const body = $("#codesBody");
  const rows = codesShown();
  const unused = CODES.filter((r) => !codeUsed(r)).length;
  $("#codeCountLbl").textContent =
    `${rows.length} shown · ${unused} unused of ${CODES.length}`;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="muted">${CODES.length
      ? "No codes match." : "No codes yet. Make one above."}</td></tr>`;
    renderCodeLimit();
    return;
  }
  renderCodeLimit();
  body.innerHTML = rows.map((r) => {
    // Spent = either field. Deleting an account clears redeemed_by (it is a
    // foreign key into the row that just went) but leaves redeemed_at, so a
    // code judged on redeemed_by alone would read as unused again.
    const used = codeUsed(r);
    const by = r.redeemed_by ? "redeemed by " + esc(r.redeemed_by)
                             : "redeemed (account deleted)";
    return `<tr>` +
      `<td style="white-space:nowrap"><code class="mono">${esc(r.code)}</code>` +
        `<button class="copy" data-copy="${esc(r.code)}" title="Copy">copy</button></td>` +
      `<td>${esc(r.contents_label || r.contents || "")}</td>` +
      `<td class="muted">${esc(r.note || "")}</td>` +
      `<td class="muted" style="white-space:nowrap" title="${esc(r.created_at || "")}">${esc(fmtWhen(r.created_at))}</td>` +
      `<td class="muted">${esc(r.created_by || "")}</td>` +
      `<td class="muted" style="white-space:nowrap">${r.expires_at && !used
        ? esc(new Date(r.expires_at * 1000).toLocaleDateString()) : ""}</td>` +
      `<td><span class="pill ${used ? "used" : "open"}" title="${esc(r.redeemed_at || "")}">${used ? by : "unused"}</span></td>` +
      `<td style="text-align:right">${used || !isOwner() ? "" :
        `<button class="danger sm" data-void="${esc(r.code)}" title="Delete this unused code">Void</button>`}</td>` +
      `</tr>`;
  }).join("");
}

// A moderator with a daily limit sees how much of it is left.
function renderCodeLimit() {
  const el = $("#codeLimitHint");
  const lim = SESSION.code_limit;
  if (isOwner() || lim === null || lim === undefined) { el.hidden = true; return; }
  const day = Date.now() - 86400e3;
  const used = CODES.filter((r) => isoMs(r.created_at) > day).length;
  el.hidden = false;
  el.textContent = `You can make ${lim} code${lim === 1 ? "" : "s"} per 24 hours: `
    + `${Math.max(0, lim - used)} left right now.`;
}

$("#codesBody").onclick = async (e) => {
  const c = e.target.closest("[data-copy]");
  if (c) return copyText(c.dataset.copy, c.dataset.copy);
  const v = e.target.closest("[data-void]");
  if (!v) return;
  const code = v.dataset.void;
  if (!confirm(`Void ${code}?\n\nIt is deleted and can no longer be redeemed.`)) return;
  v.disabled = true;
  try {
    await api("/api/codes/void", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code })
    });
    toast("Voided " + code);
    loadCodes();
  } catch (err) { v.disabled = false; toast(err.message, true); }
};
let codeSearchTimer;
$("#codeSearch").addEventListener("input", () => {
  clearTimeout(codeSearchTimer); codeSearchTimer = setTimeout(renderCodes, 100);
});
$("#codeCopyShown").onclick = () => {
  const rows = codesShown();
  if (!rows.length) return toast("Nothing to copy", true);
  copyText(rows.map((r) => r.code).join("\n"), `${rows.length} code(s)`);
};
$("#codeVoidShown").onclick = async () => {
  const rows = codesShown().filter((r) => !codeUsed(r));
  if (!rows.length) return toast("No unused codes shown", true);
  if (!confirm(`Void ${rows.length} unused code${rows.length === 1 ? "" : "s"}? They can no longer be redeemed.`)) return;
  let n = 0;
  for (const r of rows) {
    try { await post("/api/codes/void", { code: r.code }); n++; } catch (e) { toast(e.message, true); break; }
  }
  toast(`Voided ${n} code${n === 1 ? "" : "s"}`);
  loadCodes();
};

$("#codeCsv").onclick = () => downloadCsv("registration-codes.csv",
  ["code", "grants", "note", "created_at", "made_by", "expires", "redeemed_at", "redeemed_by"],
  codesShown().map((r) => [r.code, r.contents_label || r.contents, r.note, r.created_at,
                           r.created_by, r.expires_at ? new Date(r.expires_at * 1000).toISOString() : "",
                           r.redeemed_at, r.redeemed_by]));

$("#randBtn").onclick = async () => {
  try {
    $("#code").value = (await api("/api/codes/random", { method: "POST" })).code;
    $("#codeCount").value = "1";
  } catch (e) { toast(e.message, true); }
};

$("#createBtn").onclick = async () => {
  const contents = [...document.querySelectorAll("#contentChips input:checked")].map((c) => +c.value);
  if (!contents.length) return toast("Pick at least one content", true);
  const n = parseInt($("#codeCount").value, 10) || 1;
  if (n < 1 || n > 50) return toast("Make between 1 and 50 at a time", true);
  const typed = $("#code").value.trim();
  if (typed && n > 1) return toast("Leave the code blank to make several at once", true);
  const note = $("#note").value.trim(), btn = $("#createBtn"), made = [];
  const exp = $("#codeExpire").value.trim();
  if (exp && !(+exp > 0)) return toast("Expiry must be a number of days", true);
  btn.disabled = true;
  try {
    for (let i = 0; i < n; i++) {
      btn.textContent = n > 1 ? `Creating ${i + 1}/${n}...` : "Creating...";
      const r = await api("/api/codes", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: typed, contents, note, expires_days: exp ? +exp : 0 })
      });
      made.push(r.code);
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Create code"; }
  if (!made.length) return;
  toast(made.length === 1 ? "Created " + made[0] : `Created ${made.length} codes`);
  $("#code").value = ""; $("#codeHint").textContent = "Format XXXX-XXXX-XXXX-XXXX-XXXX. Leave blank for a random code.";
  $("#newCodesList").textContent = made.join("\n");
  $("#newCodesCount").textContent = `Just created: ${made.length} code${made.length === 1 ? "" : "s"}`;
  $("#newCodes").hidden = false;
  loadCodes();
};
$("#newCodesCopy").onclick = () => {
  const t = $("#newCodesList").textContent.trim();
  if (t) copyText(t, `${t.split("\n").length} code(s)`);
};

// ---- accounts ----
let ACCOUNTS = [], ACC_SHOWN = [];
const accFilter = segControl("#accFilter", "accFilter", "all", () => renderAccounts());

async function loadAccounts() {
  try {
    const rows = await api("/api/accounts");
    if (rows.error) { toast(rows.error, true); return; }
    ACCOUNTS = rows;
    // Fill the ID picker on the grant form from the same fetch.
    $("#polidList").innerHTML = rows.map((r) => `<option value="${esc(r.polid)}">`).join("");
    renderAccounts();
  } catch (e) { toast(e.message, true); }
}

function accountsShown() {
  const f = accFilter(), q = $("#accSearch").value.trim().toLowerCase();
  return ACCOUNTS.filter((r) => {
    const owned = (r.contents || []).length;
    if (f === "unlinked" && !(r.unlinked || []).length) return false;
    if (f === "nocontent" && owned) return false;
    return !q || [r.polid, r.handle, r.contents_label, r.mail, (r.clients || []).join(" ")]
      .some((v) => String(v || "").toLowerCase().includes(q));
  });
}

function renderAccounts() {
  const body = $("#accountsBody");
  ACC_SHOWN = accountsShown();
  $("#accCount").textContent = ACC_SHOWN.length === ACCOUNTS.length
    ? `${ACCOUNTS.length} account${ACCOUNTS.length === 1 ? "" : "s"}`
    : `${ACC_SHOWN.length} of ${ACCOUNTS.length}`;
  if (!ACC_SHOWN.length) {
    body.innerHTML = `<tr><td colspan="7" class="muted">${ACCOUNTS.length
      ? "No accounts match." : "No accounts yet."}</td></tr>`;
    return;
  }
  body.innerHTML = ACC_SHOWN.map((r, i) => {
    // PLAYABLE is not the same as OWNED. The launcher reads the per-handle
    // links (lobby 1:3), so a grant that was never linked shows as owned here
    // and as "You have no Content ID" on the client.
    const owned = (r.contents || []).length;
    const missing = (r.unlinked || []).length;
    const play = !owned ? `<span class="muted">-</span>`
      : missing ? `<span class="pill used">${owned - missing}/${owned}, ${missing} not linked</span>`
                : `<span class="pill open">all ${owned}</span>`;
    const clients = [...new Set(r.clients || [])];
    return `<tr>` +
      `<td style="white-space:nowrap"><a class="acct-link mono" data-i="${i}" data-act="detail">${esc(r.polid)}</a>` +
        `<button class="copy" data-i="${i}" data-act="copy" title="Copy">copy</button></td>` +
      `<td>${esc(r.handle) || "-"}</td>` +
      `<td>${esc(r.contents_label) || "-"}</td>` +
      `<td>${play}</td>` +
      `<td class="clients">${clients.length ? esc(clients.join(", ")) : "never signed in"}</td>` +
      `<td class="muted" style="white-space:nowrap" title="${esc(r.created_at || "")}">${esc(fmtWhen(r.created_at))}</td>` +
      (!isOwner() ? `<td><div class="rowacts"><button class="ghost" data-i="${i}" data-act="detail">Details</button></div></td>` :
      `<td><div class="rowacts">` +
        `<button class="ghost" data-i="${i}" data-act="detail">Details</button>` +
        `<button class="ghost" data-i="${i}" data-act="grant" title="Grant or revoke content">Content</button>` +
        `<button class="ghost" data-i="${i}" data-act="pw">Password</button>` +
        `<button class="ghost" data-i="${i}" data-act="tok">Tokens</button>` +
        `<button class="${r.ext_mail ? "act" : "ghost"}" data-i="${i}" data-act="ext" ` +
          `title="Mail to and from the internet as ${esc(outsideAddr(r))}">` +
          `Ext mail: ${r.ext_mail ? "on" : "off"}</button>` +
        `<button class="danger" data-i="${i}" data-act="del">Delete</button>` +
      `</div></td>`) + `</tr>`;
  }).join("");
}

$("#accountsBody").onclick = (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const r = ACC_SHOWN[+b.dataset.i];
  if (!r) return;
  const act = b.dataset.act;
  if (act === "copy") copyText(r.polid, r.polid);
  if (act === "detail") openAccount(r.polid);
  if (act === "grant") prefillGrant(r);
  if (act === "pw") askPassword(r.polid);
  if (act === "tok") askTokens(r.polid);
  if (act === "ext") toggleExtMail(r, b);
  if (act === "del") askDelete(r.polid);
};
let accSearchTimer;
$("#accSearch").addEventListener("input", () => {
  clearTimeout(accSearchTimer); accSearchTimer = setTimeout(renderAccounts, 100);
});
$("#accCsv").onclick = () => downloadCsv("accounts.csv",
  ["polid", "handle", "content", "not_linked", "clients", "mail", "ext_mail", "created_at"],
  accountsShown().map((r) => [r.polid, r.handle, r.contents_label,
    (r.unlinked || []).map((c) => CONTENT[c] || c).join("; "),
    [...new Set(r.clients || [])].join("; "), r.mail, r.ext_mail ? "on" : "off",
    r.created_at]));

// "Content" on a row: aim the grant form at that account and tick what it
// already owns, so the operator sees the current state before changing it.
function prefillGrant(r) {
  $("#grantPol").value = r.polid;
  const owned = new Set((r.contents || []).map(String));
  grantChecks().forEach((c) => { c.checked = owned.has(c.value); });
  const p = $("#grantPanel");
  p.scrollIntoView({ behavior: "smooth", block: "center" });
  flash(p);
  toast(`Loaded ${r.polid}. Titles it owns are ticked.`);
}

// ---- outside mail ----
// Per account and OFF by default: sign-up may be open, and an enabled account
// can mail anyone on the internet from the server's outside domain
// (POL_EXT_MAIL_DOMAIN). services/extmail.py has the rest.
function outsideAddr(r) {
  return r.ext_addr || "(no outside address - no mail name, or POL_EXT_MAIL_DOMAIN unset)";
}
async function toggleExtMail(r, btn) {
  const on = !r.ext_mail;
  if (on && !confirm(`Let ${r.polid} send and receive mail outside PlayOnline as ` +
                     `${outsideAddr(r)}?`)) return;
  btn.disabled = true;
  try {
    const res = await api("/api/account-extmail", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid: r.polid, enabled: on })
    });
    r.ext_mail = res.ext_mail;
    btn.textContent = "Ext mail: " + (res.ext_mail ? "on" : "off");
    btn.className = res.ext_mail ? "act" : "ghost";
    toast(`Outside mail ${res.ext_mail ? "ON" : "off"} for ${res.polid}`);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
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
        `<td><b>${esc(r.handle) || "-"}</b><br>` +
          `<span style="color:var(--muted)">${esc(r.host) || ""}</span></td>` +
        `<td>${esc(r.title) || "-"}</td>` +
        `<td>${esc(first) || "<i>(no description)</i>"}</td>` +
        // The honest-negative column. A bundle whose lines could not be tied to
        // this client is the case you must not read as "the server was quiet".
        `<td>${w.correlation_warning
          ? `<span title="${esc(w.correlation_warning)}">WARNING: ${esc(w.correlated_lines || 0)}</span>`
          : esc(w.correlated_lines ?? "-")}</td>`;
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
      c ? `${c.lines} lines${c.truncated ? " (truncated)" : ""}` : "");
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
        `<td><b>${esc(r.suspect) || "-"}</b></td>` +
        `<td>${esc(r.application) || "-"}</td>` +
        `<td><code class="mono">${esc(r.from) || "-"}</code></td>` +
        `<td>${esc(r.explanation) || "-"}</td>`;
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

// ---- the GM desk: requests, the duty switch, and the room ----
//
// Laid out as a help desk: requests on the left, the one you picked on the
// right with the chat under it. What is and is not proven, so nothing here
// over-claims:
//
//   * the DUTY switch is fully measured -- Join is flag 0x40, Start is 0x20,
//     the queue is 0x801 body +0x02, all confirmed against a live client -- so
//     what it sets is what a caller's GM Call screen shows on its next poll;
//   * a reply RENDERS only when it goes out under the player's OWN nick
//     ("self"): the client draws a T line only for a speaker in its member
//     table, and no inbound record can add the GM to it. So replies default to self + a "GM: " prefix, and
//     the old nick/raw probes live under Diagnostics;
//   * each request has its OWN room (gmd names it #gmcallNNN from the request
//     number and records it in the ticket), so selecting a request switches the
//     chat to that room. Requests filed before that change were all put in the
//     one shared room (#gmchat001), and say so. The room picker is there for
//     the case where a client turns out to join somewhere else.
//
// WARNING: The poll only rebuilds a list when what it shows CHANGED. Rebuilding with
// innerHTML every tick replaces the element under a press and the click never
// lands.
let GM_TIMER = null, GM_ROOM = "", GM_LOG_SIG = "", GM_LIST_SIG = "", GM_TICKET_SIG = "";
let GM_CALLS = [], GM_SEL = null, GM_DESK = null, GM_AUTOSEL = true;
let GM_FILTER = "open";
try { GM_FILTER = localStorage.getItem("gmFilter") || "open"; } catch (e) {}

function stopGmDesk() { if (GM_TIMER) { clearInterval(GM_TIMER); GM_TIMER = null; } }
function startGmDesk() {
  stopGmDesk();
  gmTick();
  // 4s: fast enough that the room reads as a conversation, slow enough that a
  // panel left open all day is not a load. The on-duty renew rides this tick,
  // so a claim only expires by the tab actually going away.
  GM_TIMER = setInterval(gmTick, 4000);
}
function gmTick() { loadGmDesk(); loadGmCalls(); }

const gmTime = (t) => new Date((t || 0) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const gmDay = (t) => new Date((t || 0) * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
function gmAgo(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  if (s < 7 * 86400) return Math.round(s / 86400) + " d ago";
  return gmDay(t);
}
const gmIso = (iso) => (Date.parse(iso || "") / 1000) || 0;
const gmHex = (n) => "0x" + (n >>> 0).toString(16);
function gmFlagsSay(f) {
  const j = f & 0x40, s = f & 0x20;
  return j && s ? "Start and Join" : j ? "Join only" : s ? "Start only, no Join"
    : "no chat buttons";
}
// The ticket body comes straight off the wire and can carry control bytes
// (ticket #3 ends in a \x07). Drop them for reading; the file keeps them.
const gmClean = (s) => String(s ?? "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");

// -- the desk ---------------------------------------------------------------
async function loadGmDesk() {
  let d;
  try {
    d = await api("/api/gm-desk" + (GM_ROOM ? "?room=" + encodeURIComponent(GM_ROOM) : ""));
  } catch (e) { return; }          // a poll that fails must not toast every 4s
  GM_DESK = d;
  GM_ROOM = d.room || GM_ROOM;
  gmRenderRoomPick(d);
  gmRenderDuty(d);
  gmRenderDiag(d);
  gmRenderLog(d);
  gmRenderChatState(d);
  // Hold the on-duty claim open while this tab is. It EXTENDS and never creates,
  // so a forgotten tab cannot re-open a desk somebody deliberately closed.
  if (d.on_duty) {
    try { await api("/api/gm-control", gmPost({ renew: true })); } catch (e) {}
  }
}

function gmRenderDuty(d) {
  const on = !!d.on_duty;
  $("#gmLight").classList.toggle("on", on);
  $("#gmState").textContent = on ? "You're on duty"
    : d.duty === null ? "Nobody is at the desk" : "Off duty";
  const sv = d.serving || {};
  const polled = sv.at ? ` · a caller last checked in ${gmAgo(sv.at)}` : "";
  $("#gmStateSub").textContent =
    `Callers see ${gmFlagsSay(d.flags)} · ${d.waiting} on a GM Call now${polled}`;
  const btn = $("#gmDutyBtn");
  btn.textContent = on ? "Go off duty" : "Go on duty";
  btn.className = on ? "ghost" : "act";

  // A pin beats the duty switch. That is exactly how the desk ended up telling
  // every caller "no chat buttons" while showing "on duty" (09-26): flags had
  // been pinned to 0 and nothing on the front of the tab said so.
  const pf = d.pinned_flags, pq = d.pinned_queue;
  const has = (v) => v !== null && v !== undefined;
  if (has(pf) || has(pq)) {
    const bits = [];
    if (has(pf)) bits.push(`flags pinned to ${gmHex(pf)}: callers see ${gmFlagsSay(pf)} regardless of duty`);
    if (has(pq)) bits.push(`queue count pinned to ${pq}`);
    $("#gmWarnText").textContent = bits.join("; ").replace(/^./, (c) => c.toUpperCase()) + ".";
    $("#gmWarn").hidden = false;
  } else {
    $("#gmWarn").hidden = true;
  }
}

function gmRenderDiag(d) {
  const line = (k, v, hot) =>
    `<div class="${hot ? "hot" : ""}"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
  const sv = d.serving || {};
  let html =
    line("Flags now", `${gmHex(d.flags)} (${gmFlagsSay(d.flags)}): ${d.flags_why || ""}`) +
    line("Last served to a caller",
         sv.flags === undefined ? "nothing polled yet" : `${gmHex(sv.flags)} at ${gmTime(sv.at)}`,
         sv.flags !== undefined && sv.flags !== d.flags) +
    line("Queue", d.pinned_queue !== null && d.pinned_queue !== undefined
         ? `${d.pinned_queue} (pinned)`
         : `${d.waiting} waiting (live)` + (d.env_queue ? `, POL_GMD_QUEUE=${d.env_queue}` : ""));
  if (d.by) html += line("Last changed by", d.by);
  html += line("Undelivered in the spool", d.pending, d.pending > 0);
  (d.callers || []).forEach((c) => html += line(
    `Caller ${c.peer}`,
    (c.request_no ? `request #${c.request_no}` : "no ticket yet")
    + `, idle ${c.idle}s` + (c.live ? "" : " (aged out)")));
  $("#gmDesk").innerHTML = html;
  $("#gmClearDuty").disabled = d.duty === null;
}

// -- the room ---------------------------------------------------------------
// Which rooms the picker offers: every room with traffic, plus the selected
// request's own room, which has no file until someone speaks in it.
function gmRenderRoomPick(d) {
  const sel = $("#gmRoomPick");
  const t = GM_CALLS.find((x) => x.id === GM_SEL);
  const rooms = [...new Set([GM_ROOM, t && t.room, ...(d.rooms || [])].filter(Boolean))];
  const sig = rooms.join("|") + ">" + GM_ROOM;
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.innerHTML = rooms.map((r) => `<option${r === GM_ROOM ? " selected" : ""}>${esc(r)}</option>`).join("")
      || `<option>(no chat room)</option>`;
  }
  const note = $("#gmRoomNote");
  if (t && t.room_shared && GM_ROOM === t.room) {
    note.textContent = "Shared room (older request): it contains messages from every caller.";
    note.hidden = false;
  } else if (t && t.room && GM_ROOM !== t.room) {
    note.textContent = `Showing ${GM_ROOM}, not this request's room (${t.room}).`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

function gmSwitchRoom(room) {
  if (!room || room === GM_ROOM) return;
  GM_ROOM = room;
  GM_LOG_SIG = "";
  $("#gmLog").innerHTML = "";
  loadGmDesk();
}
$("#gmRoomPick").onchange = (e) => gmSwitchRoom(e.target.value);

const gmShowTech = () => $("#gmShowTech").checked;

function gmRenderLog(d) {
  const rows = d.transcript || [];
  const tech = gmShowTech();
  const last = rows.length ? rows[rows.length - 1].at : 0;
  const sig = `${rows.length}:${last}:${tech}:${d.pending}`;
  if (sig === GM_LOG_SIG) return;
  GM_LOG_SIG = sig;
  const log = $("#gmLog");
  const wasBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  let html = "", day = "";
  rows.forEach((r) => {
    const cls = (r.raw || "").slice(0, 2);           // the record's class byte, in hex
    // 'H' is the client's presence heartbeat. It is the bulk of the log and
    // says nothing to a GM, so it only shows with the technical view on.
    if (cls === "48" && !tech) return;
    const dd = gmDay(r.at);
    if (dd !== day) { day = dd; html += `<div class="gm-day">${esc(dd)}</div>`; }
    const hex = tech ? `<span class="gm-hex">${esc(r.raw)}</span>` : "";
    if (cls === "54") {                               // 'T', a chat line
      let text = r.text || "";
      if (!tech) text = text.replace(/\s+\[head '[^']*'\]$/, "");
      const out = r.dir === "out";
      const who = out ? "You (GM)" : `Player${r.nick ? " · " + r.nick : ""}`;
      html += `<div class="gm-msg ${out ? "out" : "in"}">` +
        `<span class="gm-meta">${esc(who)} · ${esc(gmTime(r.at))}</span>` +
        `<span class="gm-text">${esc(text)}</span>${hex}</div>`;
    } else {
      // 'U' membership lines read as sentences already ("Abe joined").
      html += `<div class="gm-sys">${esc(r.text || "")} · ${esc(gmTime(r.at))}` +
        (tech ? `<br>${hex}` : "") + `</div>`;
    }
  });
  if (d.pending > 0)
    html += `<div class="gm-sys">${d.pending} line${d.pending > 1 ? "s" : ""} queued ` +
      `until the player is in the room.</div>`;
  log.innerHTML = html || `<div class="gm-log-empty">No messages yet. Messages appear ` +
    `here once the player joins the chat from their GM Call screen.</div>`;
  if (wasBottom) log.scrollTop = log.scrollHeight;
}

function gmRenderChatState(d) {
  const el = $("#gmChatState");
  const rows = d.transcript || [];
  let lastIn = 0;
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].dir === "in") { lastIn = rows[i].at; break; }
  let text, cls = "";
  if (d.pending > 0) {
    text = "Waiting for the player to join"; cls = "warn";
  } else if (lastIn && Date.now() / 1000 - lastIn < 600) {
    text = `Player active, last heard ${gmAgo(lastIn)}`; cls = "good";
  } else if (!(d.flags & 0x40)) {
    text = "Go on duty to let callers join"; cls = "warn";
  } else {
    text = "Nobody in the room right now";
  }
  el.textContent = text;
  el.className = "gm-chat-state " + cls;
  $("#gmComposeHint").textContent = !(d.flags & 0x40)
    ? "Go on duty to let callers join the chat."
    : `Sent to ${GM_ROOM || "the room"}. Shown to the player as ` +
      `"<name> > ${$("#gmPrefix").value}your text".`;
}

const gmPost = (body) => ({
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body)
});

async function gmControl(body, msg) {
  try {
    const r = await api("/api/gm-control", gmPost(body));
    if (msg) toast(msg);
    GM_LOG_SIG = "";
    loadGmDesk();
    return r;
  } catch (e) { toast(e.message, true); }
}

$("#gmDutyBtn").onclick = () => {
  const d = GM_DESK || {};
  if (d.on_duty) return gmControl({ on_duty: false }, "Off duty");
  // Going on duty means "let callers in", so it also drops any pins that would
  // hide the buttons. A pin is a diagnostic; leaving one in force here is how
  // the 09-26 caller got a desk that looked open and was not.
  const body = { on_duty: true };
  const pinned = (d.pinned_flags ?? null) !== null || (d.pinned_queue ?? null) !== null;
  if (pinned) { body.flags = null; body.queue = null; }
  gmControl(body, "On duty" + (pinned ? " (pins cleared)" : ""));
};
$("#gmWarnFix").onclick = () => gmControl({ flags: null, queue: null }, "Pins cleared");
$("#gmClearDuty").onclick = () =>
  gmControl({ on_duty: null }, "Duty reset to the server default");
$("#gmPin").onclick = () => {
  const q = $("#gmQueue").value.trim(), f = $("#gmFlags").value.trim();
  gmControl({ queue: q === "" ? null : q, flags: f === "" ? null : f }, "Pins applied");
};
$("#gmUnpin").onclick = () => {
  $("#gmQueue").value = ""; $("#gmFlags").value = "";
  gmControl({ queue: null, flags: null }, "Pins cleared");
};
$("#gmShowTech").onchange = () => { GM_LOG_SIG = ""; if (GM_DESK) gmRenderLog(GM_DESK); };

async function gmSay(body) {
  if (!GM_ROOM) { toast("No chat room is available. Check that the GM service is running.", true); return false; }
  try {
    await api("/api/gm-say", gmPost({ ...body, room: GM_ROOM,
      nick: $("#gmNick").value.trim() || "self" }));
    GM_LOG_SIG = "";
    loadGmDesk();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

$("#gmSend").onclick = async () => {
  // The spool is one record per LINE, so a multi-line reply goes out as one
  // chat line per line rather than a record with a raw newline in it.
  const lines = $("#gmSay").value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return;
  const prefix = $("#gmPrefix").value;
  for (const l of lines) if (!(await gmSay({ say: prefix + l }))) return;
  $("#gmSay").value = "";
  // Replying is what "answered" means on a help desk; saves a click.
  const t = GM_CALLS.find((r) => r.id === GM_SEL);
  if (t && t.status === "open") gmSetStatus(t.id, "answered", true);
};
$("#gmSay").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#gmSend").click(); }
};
$("#gmPrefix").oninput = () => { if (GM_DESK) gmRenderChatState(GM_DESK); };
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

// -- the requests -------------------------------------------------------------
// Filed by the `gmd` service from the 0x102 the client sends on Submit; the
// open / answered / closed state is the desk's own, kept beside them.
function gmBadge(rows) {
  const n = rows.filter((r) => r.status === "open").length;
  const b = $("#gmBadge");
  b.textContent = n; b.hidden = !n;
  document.title = (n ? `(${n}) ` : "") + "OpenLobby Admin";
}

async function loadGmCalls() {
  let rows;
  try { rows = await api("/api/gm-calls"); } catch (e) { return; }
  if (!Array.isArray(rows)) return;
  GM_CALLS = rows;
  gmBadge(rows);
  if (GM_AUTOSEL && !GM_SEL) {
    // First visit: open the newest request that still needs an answer.
    const first = rows.find((r) => r.status === "open");
    if (first) GM_SEL = first.id;
    GM_AUTOSEL = false;
    gmFollowTicketRoom();
  }
  gmRenderList();
  gmRenderTicket();
}

function gmRenderList() {
  const shown = GM_CALLS.filter((r) => GM_FILTER === "all" || r.status !== "closed");
  const sig = GM_FILTER + "|" + GM_SEL + "|" + shown.map((r) =>
    [r.id, r.status, r.connected, gmAgo(gmIso(r.received_at))].join(",")).join(";");
  if (sig === GM_LIST_SIG) return;
  GM_LIST_SIG = sig;
  document.querySelectorAll(".gm-filter button").forEach(
    (b) => b.classList.toggle("on", b.dataset.f === GM_FILTER));
  if (!shown.length) {
    $("#gmList").innerHTML = `<div class="gm-none">` + (GM_CALLS.length
      ? "Nothing open. Every request has been closed."
      : "No GM calls yet. They appear here the moment a player submits one.") + `</div>`;
    return;
  }
  $("#gmList").innerHTML = shown.map((r) =>
    `<div class="gm-item${r.id === GM_SEL ? " sel" : ""}${r.status === "closed" ? " closed" : ""}" data-id="${esc(r.id)}">` +
      `<div class="gm-item-top">` +
        `<span class="gm-item-who">${r.connected ? `<span class="gm-dot" title="On a GM Call now"></span>` : ""}` +
          `${esc(gmClean(r.handle)) || "(no handle)"}</span>` +
        `<span class="gm-item-when">${esc(gmAgo(gmIso(r.received_at)))}</span></div>` +
      `<div class="gm-item-subj">${esc(gmClean(r.subject)) || "(no subject)"}</div>` +
      `<div><span class="gm-st ${esc(r.status)}">${esc(r.status)}</span></div>` +
    `</div>`).join("");
}

$("#gmList").onclick = (e) => {
  const it = e.target.closest(".gm-item");
  if (!it) return;
  GM_SEL = it.dataset.id;
  gmRenderList();
  gmRenderTicket();
  gmFollowTicketRoom();
};

// Selecting a request shows ITS room. Called on a click and when the first
// load auto-selects one, never on a plain poll, so a room the operator picked
// by hand is not yanked away every 4 seconds.
function gmFollowTicketRoom() {
  const t = GM_CALLS.find((x) => x.id === GM_SEL);
  if (t && t.room) gmSwitchRoom(t.room);
}
document.querySelectorAll(".gm-filter button").forEach((b) => {
  b.onclick = () => {
    GM_FILTER = b.dataset.f;
    try { localStorage.setItem("gmFilter", GM_FILTER); } catch (e) {}
    gmRenderList();
  };
});

function gmRenderTicket() {
  const r = GM_CALLS.find((x) => x.id === GM_SEL);
  const sig = r ? [r.id, r.status, r.connected, gmAgo(gmIso(r.received_at))].join(",") : "";
  if (sig === GM_TICKET_SIG) return;
  GM_TICKET_SIG = sig;
  $("#gmEmpty").hidden = !!r;
  $("#gmTicket").hidden = !r;
  if (!r) return;
  $("#gmTWho").textContent = gmClean(r.handle) || "(no handle)";
  $("#gmTSubj").textContent = gmClean(r.subject) || "(no subject)";
  $("#gmTChips").innerHTML =
    (r.connected ? `<span class="gm-st live">On a GM Call now</span>` : "") +
    `<span class="gm-st ${esc(r.status)}">${esc(r.status)}</span>`;
  const t = gmIso(r.received_at);
  const meta = [
    r.content_label || (r.content_id != null ? `content ${r.content_id}` : ""),
    r.issue != null ? `issue type ${r.issue}` : "",
    r.request_no != null ? `request #${r.request_no}` : "",
    t ? `${gmDay(t)} ${gmTime(t)} (${gmAgo(t)})` : "",
    r.peer ? `from ${String(r.peer).replace(/:\d+$/, "")}` : "",
  ].filter(Boolean);
  $("#gmTMeta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");
  $("#gmTBody").textContent = gmClean(r.body).trim() || "(no message text)";
  const btn = (st, label, cls) =>
    `<button class="${cls}" data-st="${st}">${label}</button>`;
  $("#gmTActions").innerHTML =
    r.status === "open" ? btn("answered", "Mark answered", "ghost") + btn("closed", "Close", "ghost")
    : r.status === "answered" ? btn("closed", "Close", "ghost") + btn("open", "Reopen", "ghost")
    : btn("open", "Reopen", "ghost");
}

$("#gmTActions").onclick = (e) => {
  const b = e.target.closest("button[data-st]");
  if (b && GM_SEL) gmSetStatus(GM_SEL, b.dataset.st);
};

async function gmSetStatus(id, status, quiet) {
  try {
    await api("/api/gm-ticket", gmPost({ id, status }));
    const r = GM_CALLS.find((x) => x.id === id);
    if (r) r.status = status;
    GM_LIST_SIG = GM_TICKET_SIG = "";
    gmBadge(GM_CALLS);
    gmRenderList();
    gmRenderTicket();
    if (!quiet) toast(status === "open" ? "Reopened" : `Marked ${status}`);
  } catch (e) { toast(e.message, true); }
}

// The badge keeps counting while you are on another tab, so a new call is
// never only visible to someone who happens to be looking at this one.
setInterval(() => { if (!GM_TIMER) loadGmCallsBadge(); }, 30000);
async function loadGmCallsBadge() {
  if (SESSION.role && !can("gm")) return;
  try { const rows = await api("/api/gm-calls"); if (Array.isArray(rows)) gmBadge(rows); }
  catch (e) {}
}
loadGmCallsBadge();

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
  const members = fp.members.map((m) => m.login_name).join(", ") || "-";
  let html =
    line("Members", members) +
    line("Handles", fp.handles.join(", ") || "-") +
    line("Content", fp.contents_label || "-") +
    line("Friend entries", fp.friends) +
    line("Groups", fp.groups) +
    line("On other people's lists", fp.referenced_by, fp.referenced_by > 0) +
    line("Mail messages", fp.mail, fp.mail > 0) +
    line("Open sessions", fp.sessions);
  if (fp.online) html += line("Signed in", "yes (they will be signed out)", true);
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
      line("Content", r.contents_label || "-") +
      line("Mail", r.mail) +
      `<div class="hot"><span>Save this password</span>` +
      `<span>It cannot be shown again.</span></div>`;
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
          + (r.token_reset ? ". Login token reset." : ""));
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
};

// ---- login tokens, one per client ----
// The lobby's trust-on-first-use is keyed (account x client build): one recorded
// token per machine and build. So being locked out is normally ONE row gone
// stale -- a console and an emulator present the same signature and re-seed each
// other's -- and the fix is to clear THAT row. The Password dialog's checkbox
// clears the account token, which drops every client row with it, so unsticking
// the PS2 that way makes the PC re-seed too.
//
// Every string in here is escaped: a client signature is 21 bytes off the NICK
// line, which is to say off the wire.
let TOK_POLID = null;

function askTokens(polid) {
  TOK_POLID = polid;
  $("#tokPolid").textContent = polid;
  $("#tokList").innerHTML = `<div><span>Reading…</span></div>`;
  $("#tokModal").classList.add("show");
  renderTokens();
}

function closeTokens() {
  $("#tokModal").classList.remove("show");
  TOK_POLID = null;
}

async function renderTokens() {
  const polid = TOK_POLID, list = $("#tokList");
  let r;
  try { r = await api("/api/account-clients?polid=" + encodeURIComponent(polid)); }
  catch (e) { list.innerHTML = ""; return toast(e.message, true); }
  if (TOK_POLID !== polid) return;          // the dialog moved on while we asked
  list.innerHTML = "";
  if (!r.clients.length) {
    // Not an error: an account that has never signed in to the lobby, or
    // one whose tokens were just cleared, legitimately has no rows.
    list.innerHTML = `<div><span>No device has signed in to the lobby yet.</span></div>`;
    if (!r.legacy_token && !r.armed) {
      list.innerHTML += `<div class="hot"><span>Next sign-in</span>` +
        `<span>reads as this account's first ever - arm it first ` +
        `(accounts.py arm)</span></div>`;
    }
    return;
  }
  r.clients.forEach((c) => {
    const row = document.createElement("div");
    const who = document.createElement("span");
    who.className = "who";
    // A signature we have not identified is shown RAW rather than guessed at.
    // `last_seen` is what separates two rigs that share one.
    who.innerHTML =
      `<span><b>${esc(c.known ? c.label + " Viewer" : "Unknown device")}</b>` +
      `<span style="color:var(--muted)"> · last seen ` +
      `${esc((c.last_seen || "").slice(0, 19).replace("T", " ")) || "never"}</span></span>` +
      `<span class="sig">${esc(c.client_sig)}</span>`;
    const btn = document.createElement("button");
    btn.className = "danger";
    btn.textContent = "Clear";
    btn.onclick = () => clearToken(polid, c.client_sig, btn);
    row.append(who, btn);
    list.append(row);
  });
}

async function clearToken(polid, sig, btn) {
  btn.disabled = true;
  try {
    const r = await api("/api/account-clear-token", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, client_sig: sig })
    });
    // `rearm_needed` is the trap: nothing recorded anywhere reads to the lobby
    // as this account's FIRST login, and the arm gate refuses that unless the
    // account is armed. Say so rather than reporting a flat success.
    toast(r.rearm_needed
      ? `Cleared - but nothing is recorded now, so the next sign-in needs this `
        + `account armed (accounts.py arm)`
      : `Cleared ${r.label} on ${r.polid} - its next sign-in records a fresh token`,
      !!r.rearm_needed);
    renderTokens();
  } catch (e) { btn.disabled = false; toast(e.message, true); }
}

$("#tokClose").onclick = closeTokens;
$("#tokModal").onclick = (e) => { if (e.target === $("#tokModal")) closeTokens(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#tokModal").classList.contains("show")) closeTokens();
});

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
    toast(r.linked ? `Granted to ${polid}`
                   : "Granted, but the account has no handle yet, so it cannot play",
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
  if (!confirm(`Revoke ${names} from ${polid}?\n\nTheir characters are removed from the launcher. Granting again restores them.`)) return;
  try {
    const r = await api("/api/revoke", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polid, content_codes: codes })
    });
    toast(`Revoked from ${r.polid}`);
    loadAccounts();
  } catch (e) { toast(e.message, true); }
};

// ---- PML preview ----
// A page browser on the left, the page drawn with the Viewer's own font in the
// middle, and the source on the right. Links on the page navigate inside the
// preview (with Back/Forward), `sd:show=1@panel` reveals a hidden panel, and
// Inspect shows an element's settings and jumps the source to its line.
const stage = $("#stage");
// The file being previewed. Relative src/href resolve against it.
let ACTIVE_FILE = null;
// Pages built from the template layer need the server-side expander first.
const TEMPLATED = /<(for|if|include|array|define)\b|&var=|&calc=|="[^"]*\$[A-Za-z_]|\{\$/i;
let REVEAL = null;          // a hidden panel to draw, or "all"
let renderSeq = 0;
let EXPAND_REPORT = {};
let LAST_RENDER = null;
let INSPECT = false;
let PICKED = null;
let ZOOM = store.get("pvZoom", "fit");
const PV_HIST = [], PV_FWD = [];

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
    } catch (e) { /* draw the raw text */ }
    if (seq !== renderSeq) return;
  }
  await PML.ready();
  if (seq !== renderSeq) return;
  try {
    MISSING_ART.clear();
    LAST_RENDER = PML.render(toRender, stage, ACTIVE_FILE || "", {
      reveal: REVEAL, onMissing: noteMissingArt,
      onRedraw: (r) => { LAST_RENDER = r; describeRender(r); },
    });
    describeRender(LAST_RENDER);
  } catch (e) {
    stage.innerHTML = `<div style="color:#ff8a8a;padding:12px;font:13px monospace">Could not draw this file: ${esc(e.message)}</div>`;
    $("#stageNote").textContent = "";
  }
  stage.classList.toggle("pml-inspect", INSPECT);
  paintMissingToggle();
  PICKED = null;
  $("#pvInspector").hidden = true;
  fitStage();
}

// Images the page names that the mirror does not have. Counted as they fail
// to load, so the status line can say "missing from the mirror" rather than
// leaving a red outline to be read as a renderer bug.
const MISSING_ART = new Set();
function noteMissingArt(src) {
  MISSING_ART.add(src);
  let el = $("#pvMissing");
  if (!el) {
    el = document.createElement("div");
    el.id = "pvMissing";
    el.className = "warn";
    $("#stageNote").appendChild(el);
  }
  el.textContent = `${MISSING_ART.size} image${MISSING_ART.size === 1 ? "" : "s"} not in the mirror (hatched plates; "Missing art" highlights them)`;
  el.title = [...MISSING_ART].join("\n");
}

// One status line, in plain words, plus the warnings that explain a wrong
// or empty drawing.
function describeRender(r) {
  const note = $("#stageNote");
  renderLayerSwitch(r.layers || []);
  const bits = [];
  if (r.mode === "page" || r.mode === "layout") {
    bits.push(`<b>${r.placed}</b> element${r.placed === 1 ? "" : "s"}`);
    const parts = PARTS[ACTIVE_FILE] || 0;
    if (parts) bits.push(`built from <b>${parts}</b> other file${parts === 1 ? "" : "s"}`);
    if (!r.hasBody) bits.push("a piece of a page (no &lt;body&gt;)");
  } else if (r.mode === "document") {
    bits.push(`<b>${r.records}</b> block${r.records === 1 ? "" : "s"} of text, shown the way a page displays it`);
  } else {
    bits.push("<b>Nothing to draw.</b> This file only defines data for other pages");
  }
  const warn = [];
  if (!r.fontReady) warn.push("the Viewer font did not load, so text is missing");
  if (r.standIn) warn.push(`${r.standIn} character${r.standIn === 1 ? "" : "s"} not in the Viewer font (drawn in a stand-in font)`);
  if (r.varErrors) warn.push(`${r.varErrors} unresolved variable${r.varErrors === 1 ? "" : "s"} (the Viewer shows "(Variable error)")`);
  if (r.unresolved) warn.push(`${r.unresolved} element${r.unresolved === 1 ? "" : "s"} with values that could not be worked out (dotted outline)`);
  if ((r.missingStyles || []).length) warn.push("missing styles: " + r.missingStyles.slice(0, 4).map(esc).join(", "));
  if ((r.missingData || []).length) warn.push("missing text data: " + r.missingData.slice(0, 4).map(esc).join(", "));
  if ((r.unsupported || []).length) warn.push("not drawn: &lt;" + r.unsupported.slice(0, 5).map(esc).join("&gt;, &lt;") + "&gt;");
  const miss = (EXPAND_REPORT.missing || []).slice(0, 5);
  if (miss.length && r.mode !== "page") warn.push("uses " + miss.map((v) => "$" + esc(v)).join(", ") + ", which the page that includes this defines");
  const unres = (EXPAND_REPORT.unresolved || []).slice(0, 3);
  if (unres.length) warn.push("included file not found: " + unres.map(esc).join(", "));
  note.innerHTML = bits.join(" · ") + (warn.length ? `<br><span class="warn">${warn.join("<br>")}</span>` : "");
  $("#pvTitle").textContent = r.title || (ACTIVE_FILE ? ACTIVE_FILE.split("/").pop() : "Pasted PML");
}

// Hidden panels: the page stacks them and reveals one at a time.
function renderLayerSwitch(layers) {
  const box = $("#stageLayers");
  box.innerHTML = "";
  if (!layers.length) return;
  const lbl = document.createElement("span");
  lbl.className = "lbl";
  lbl.textContent = "Hidden panels:";
  box.appendChild(lbl);
  const btn = (value, text) => {
    const b = document.createElement("button");
    b.textContent = text;
    if (REVEAL === value) b.className = "on";
    b.onclick = () => { REVEAL = value; renderPreview(); };
    box.appendChild(b);
  };
  if (layers.length > 8) {
    // A page with dozens of panels: a dropdown, not a wall of buttons.
    const sel = document.createElement("select");
    sel.style.width = "auto";
    sel.innerHTML = `<option value="">as loaded</option>` +
      layers.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("") +
      `<option value="*all">all at once</option>`;
    sel.value = REVEAL === "all" ? "*all" : (REVEAL || "");
    sel.onchange = () => { REVEAL = sel.value === "*all" ? "all" : (sel.value || null); renderPreview(); };
    lbl.textContent = `${layers.length} hidden panels:`;
    box.appendChild(sel);
    return;
  }
  btn(null, "as loaded");
  layers.forEach((name) => btn(name, name));
  btn("all", "all at once");
}

// ---- zoom ----
function fitStage() {
  const boxEl = $("#pvStageBox"), sc = $("#pvStageScale");
  let z = ZOOM === "fit" ? Math.max(0.5, (boxEl.clientWidth - 22) / 640) : +ZOOM;
  if (ZOOM === "fit") z = Math.min(z, 2.5);
  sc.style.transform = `scale(${z})`;
  sc.style.width = 640 * z + "px";
  sc.style.height = 480 * z + "px";
  sc.style.transform = "";
  stage.style.transform = `scale(${z})`;
  stage.style.transformOrigin = "0 0";
  stage.style.imageRendering = z >= 2 ? "pixelated" : "";
  document.querySelectorAll("#pvZoom button").forEach((b) => b.classList.toggle("on", b.dataset.z === ZOOM));
}
$("#pvZoom").onclick = (e) => {
  const b = e.target.closest("button[data-z]");
  if (!b) return;
  ZOOM = b.dataset.z; store.set("pvZoom", ZOOM); fitStage();
};
window.addEventListener("resize", () => { if (CUR_TAB === "pml") fitStage(); });

// ---- links, panels, inspect ----
stage.addEventListener("click", async (e) => {
  if (INSPECT) {
    e.preventDefault();
    const hit = PML.nodeAt(stage, e.target);
    if (hit) pickElement(hit);
    return;
  }
  const link = e.target.closest("[data-href]");
  if (link) followHref(link.dataset.href);
});

async function followHref(href) {
  href = String(href || "").trim();
  // A page can carry several actions: "sd:show=1@menu;sd:show=0@top".
  const sd = [...href.matchAll(/sd:show=(\d)@([\w-]+)/g)];
  if (sd.length) {
    const show = sd.find((m) => m[1] === "1");
    if (show) { REVEAL = show[2]; renderPreview(); toast("Showing panel " + show[2]); }
    else { REVEAL = null; renderPreview(); }
    return;
  }
  if (/^toviewer:/i.test(href)) return toast("This link returns to the Viewer's own menu");
  if (/^(eval|javascript|sd):/i.test(href) || !href) return toast("This link runs a page action the preview does not follow");
  let r;
  try { r = await api(`/api/pml-resolve?from=${encodeURIComponent(ACTIVE_FILE || "")}&href=${encodeURIComponent(href)}`); }
  catch (e) { return toast(e.message, true); }
  if (r.path) return openPmlFile(r.path, { push: true });
  if (r.external) return toast("Link to " + r.external + " (outside the mirror)");
  toast("Link target not found in the mirror: " + href, true);
}

// Highlight the art the mirror does not have. Remembered per browser.
let SHOW_MISSING = store.get("pvMissing", "0") === "1";
function paintMissingToggle() {
  $("#pvMissingBtn").classList.toggle("on", SHOW_MISSING);
  stage.classList.toggle("pml-show-missing", SHOW_MISSING);
}
$("#pvMissingBtn").onclick = () => {
  SHOW_MISSING = !SHOW_MISSING;
  store.set("pvMissing", SHOW_MISSING ? "1" : "0");
  paintMissingToggle();
};
paintMissingToggle();

$("#pvInspect").onclick = () => {
  INSPECT = !INSPECT;
  $("#pvInspect").classList.toggle("on", INSPECT);
  stage.classList.toggle("pml-inspect", INSPECT);
  if (!INSPECT) { $("#pvInspector").hidden = true; if (PICKED) PICKED.classList.remove("pml-picked"); }
  toast(INSPECT ? "Inspect: click any element" : "Inspect off");
};

function pickElement(hit) {
  if (PICKED) PICKED.classList.remove("pml-picked");
  PICKED = hit.el;
  PICKED.classList.add("pml-picked");
  const n = hit.node;
  const attrs = Object.entries(n.attrs || {}).filter(([k]) => k !== "pml-line");
  const line = n.line || 0;
  const ins = $("#pvInspector");
  ins.hidden = false;
  ins.innerHTML = `<div class="pv-row" style="justify-content:space-between">` +
    `<b>&lt;${esc(n.tag)}&gt;</b>` +
    (line ? `<button class="ghost sm" id="pvGoLine">Show line ${line} in source</button>` : "") +
    `</div><div class="kv">` +
    (attrs.length ? attrs.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")
                  : `<span>-</span><span class="muted">no attributes</span>`) + `</div>`;
  if (line) $("#pvGoLine").onclick = () => gotoLine(line);
}

// ---- the source editor ----
function updateGutter() {
  const n = ($("#pml").value.match(/\n/g) || []).length + 1;
  const g = $("#pvGutter");
  if (+g.dataset.n !== n) {
    g.dataset.n = n;
    g.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
  }
  g.scrollTop = $("#pml").scrollTop;
}
$("#pml").addEventListener("scroll", () => { $("#pvGutter").scrollTop = $("#pml").scrollTop; });

function showSource(on) {
  $("#pvSource").hidden = !on;
  $("#pvSourceBtn").classList.toggle("act", on);
  store.set("pvSource", on ? "1" : "0");
  fitStage();
}
$("#pvSourceBtn").onclick = () => showSource($("#pvSource").hidden);

function gotoLine(line) {
  showSource(true);
  const ta = $("#pml");
  const lines = ta.value.split("\n");
  let pos = 0;
  for (let i = 0; i < line - 1 && i < lines.length; i++) pos += lines[i].length + 1;
  ta.focus();
  ta.setSelectionRange(pos, pos + (lines[line - 1] || "").length);
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 18;
  ta.scrollTop = Math.max(0, (line - 5) * lh);
  updateGutter();
}

let renderTimer;
$("#pml").addEventListener("input", () => {
  updateGutter();
  clearTimeout(renderTimer);
  renderTimer = setTimeout(renderPreview, 150);
});
$("#renderBtn").onclick = renderPreview;

// Which pages pull this file in, and which it pulls in or links to.
const REF_SHOWN = 8;
async function loadRefs(path) {
  const box = $("#stageRefs");
  box.innerHTML = "";
  if (!path) return;
  let j;
  try { j = await api("/api/pml-refs?path=" + encodeURIComponent(path)); }
  catch (e) { return; }
  if (ACTIVE_FILE !== path) return;
  const line = (label, rows) => {
    if (!rows.length) return;
    const d = document.createElement("div");
    const l = document.createElement("span");
    l.className = "rl";
    l.textContent = `${label} (${rows.length})`;
    d.appendChild(l);
    const add = (from, to) => rows.slice(from, to).forEach((r) => {
      const a = document.createElement("a");
      a.textContent = TITLES[r.path] || r.path.replace(/^_(lang|eras)\/[^/]+\//, "…/");
      a.title = r.path;
      a.onclick = () => openPmlFile(r.path, { push: true });
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
  line("Built from", j.built_from || []);
  line("Used by", j.included_by || []);
  line("Links to", j.links_to || []);
  line("Linked from", j.linked_from || []);
}

// ---- the page browser ----
let ALLFILES = [];
let PML_LIST_READY = Promise.resolve();
let SHAPES = {}, PARTS = {}, TITLES = {};
const GAME_BY_SEG = {
  ff11: "FFXI", ffxi: "FFXI", tetra: "Tetra Master", fmo: "Front Mission Online",
  fe: "Fantasy Earth", fantasyearth: "Fantasy Earth", dc: "Dirge of Cerberus",
  eq2: "EverQuest II", ff14: "FINAL FANTASY XIV", ffxiv: "FINAL FANTASY XIV",
  jan: "Janhourou", janhourou: "Janhourou", ambrosia: "Ambrosia Odyssey",
};
const LOCALE_RE = /^(en-US|en-GB|ja-JP|fr-FR|de-DE|ja|fr|de)$/i;
const SHAPE_WORD = { page: "page", layout: "piece", content: "text", data: "data" };

function classify(path) {
  const segs = path.split("/");
  let game = null, locale = null;
  for (const s of segs) {
    const g = GAME_BY_SEG[s.toLowerCase()];
    if (g && !game) game = g;
    if (!locale && LOCALE_RE.test(s)) locale = s;
  }
  const host = segs[0] === "_lang" || segs[0] === "_eras" ? segs[2] : segs[0];
  return { path, host, game, locale, shape: SHAPES[path] || "", title: TITLES[path] || "",
           name: segs[segs.length - 1] };
}

function fillFacet(sel, values, label) {
  const cur = sel.value;
  sel.innerHTML = `<option value="">Any ${label}</option>` +
    [...values.entries()].sort((a, b) => a[0].localeCompare(b[0]))
      .map(([v, n]) => `<option value="${esc(v)}">${esc(v)} (${n})</option>`).join("");
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

async function loadPmlFileList() {
  try {
    const j = await api("/api/pml-list");
    SHAPES = j.shapes || {};
    PARTS = j.parts || {};
    TITLES = j.titles || {};
    ALLFILES = j.files.map(classify);
    const hosts = new Map(), games = new Map(), locales = new Map();
    const bump = (m, k) => { if (k) m.set(k, (m.get(k) || 0) + 1); };
    ALLFILES.forEach((f) => { bump(hosts, f.host); bump(games, f.game); bump(locales, f.locale); });
    fillFacet($("#fHost"), hosts, "site");
    fillFacet($("#fGame"), games, "game");
    fillFacet($("#fLocale"), locales, "language");
    renderFileList();
  } catch (e) { $("#pmlCount").textContent = "(list unavailable)"; }
}

const kindMatches = (kind, shape) => !kind || (kind === "draws" ? shape !== "data" : shape === kind);
const visibleUnderFilter = (path) => kindMatches($("#fKind").value, SHAPES[path] || "");
let FILE_ROWS = [];

function renderFileList() {
  const host = $("#fHost").value, game = $("#fGame").value, locale = $("#fLocale").value,
    q = $("#fSearch").value.toLowerCase().trim(), sort = $("#fSort").value, kind = $("#fKind").value;
  document.querySelectorAll("#fKindSeg button").forEach((b) => b.classList.toggle("on", b.dataset.f === kind));
  FILE_ROWS = ALLFILES.filter((f) =>
    (!host || f.host === host) && (!game || f.game === game) &&
    (!locale || f.locale === locale) && kindMatches(kind, f.shape) &&
    (!q || f.path.toLowerCase().includes(q) || f.title.toLowerCase().includes(q)));
  FILE_ROWS.sort((a, b) => sort === "title"
    ? (a.title || "~" + a.name).localeCompare(b.title || "~" + b.name) || a.path.localeCompare(b.path)
    : a.path.localeCompare(b.path));
  $("#pmlCount").textContent = `(${FILE_ROWS.length.toLocaleString()} of ${ALLFILES.length.toLocaleString()})`;
  const list = $("#fileList");
  const CAP = 400;
  list.innerHTML = FILE_ROWS.slice(0, CAP).map((f, i) =>
    `<div class="pv-item${f.path === ACTIVE_FILE ? " active" : ""}" data-i="${i}">` +
    `<div class="t">${esc(f.title || f.name)}${f.shape && f.shape !== "page" ? `<span class="k">${SHAPE_WORD[f.shape] || f.shape}</span>` : ""}</div>` +
    `<div class="p" title="${esc(f.path)}">${esc(f.path)}</div></div>`).join("")
    + (FILE_ROWS.length > CAP ? `<div class="pv-more">${(FILE_ROWS.length - CAP).toLocaleString()} more. Search or filter to narrow.</div>` : "")
    || `<div class="pv-more">No files match.</div>`;
}
$("#fileList").onclick = (e) => {
  const it = e.target.closest(".pv-item");
  if (it) openPmlFile(FILE_ROWS[+it.dataset.i].path, { push: true });
};
$("#fKindSeg").onclick = (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  $("#fKind").value = b.dataset.f;
  renderFileList();
};
["fHost", "fGame", "fLocale", "fSort"].forEach((id) => $("#" + id).addEventListener("change", renderFileList));
let searchTimer;
$("#fSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderFileList, 120); });

async function openPmlFile(path, opts) {
  if (!path) return;
  opts = opts || {};
  try {
    const r = await fetch("/api/pml-load?path=" + encodeURIComponent(path));
    const txt = await r.text();
    if (!r.ok) throw new Error(r.status === 404 ? "file not found" : txt);
    if (opts.push && ACTIVE_FILE && ACTIVE_FILE !== path) { PV_HIST.push(ACTIVE_FILE); PV_FWD.length = 0; }
    $("#pml").value = txt;
    updateGutter();
    const kind = r.headers.get("X-PML-Kind") || "";
    $("#pmlKind").textContent = kind.replace("plaintext/", "").replace("pmlus-cipher/", "encrypted, ");
    ACTIVE_FILE = path;
    REVEAL = null;
    $("#pvPath").textContent = path;
    writeHash("pml", path);
    if (Object.keys(SHAPES).length && !visibleUnderFilter(path)) $("#fKind").value = "";
    renderFileList();
    // Scroll the LIST to the open file, never the window (that pushed the
    // toolbar off screen).
    const act = document.querySelector("#fileList .pv-item.active");
    const list = $("#fileList");
    if (act && (act.offsetTop < list.scrollTop || act.offsetTop > list.scrollTop + list.clientHeight - 40)) {
      list.scrollTop = act.offsetTop - list.clientHeight / 2;
    }
    renderPreview();
    loadRefs(path);
  } catch (e) { toast("Could not open " + path + ": " + e.message, true); }
  updateNavButtons();
}

function updateNavButtons() {
  $("#pvBack").disabled = !PV_HIST.length;
  $("#pvFwd").disabled = !PV_FWD.length;
}
$("#pvBack").onclick = () => {
  if (!PV_HIST.length) return;
  if (ACTIVE_FILE) PV_FWD.push(ACTIVE_FILE);
  openPmlFile(PV_HIST.pop());
};
$("#pvFwd").onclick = () => {
  if (!PV_FWD.length) return;
  if (ACTIVE_FILE) PV_HIST.push(ACTIVE_FILE);
  openPmlFile(PV_FWD.pop());
};
document.addEventListener("keydown", (e) => {
  if (CUR_TAB !== "pml" || !e.altKey) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); $("#pvBack").click(); }
  if (e.key === "ArrowRight") { e.preventDefault(); $("#pvFwd").click(); }
});

$("#loadBtn").onclick = async () => {
  try {
    const kinou = $("#liveKinou").value, step = $("#liveStep").value;
    const r = await fetch(`/api/page?kinou_id=${kinou}&step=${step}`);
    const txt = await r.text();
    if (!r.ok) throw new Error(txt);
    if (ACTIVE_FILE) { PV_HIST.push(ACTIVE_FILE); PV_FWD.length = 0; }
    $("#pml").value = txt;
    updateGutter();
    ACTIVE_FILE = null;
    $("#pvPath").textContent = `registration wizard, step ${step}`;
    $("#stageRefs").innerHTML = "";
    renderFileList();
    renderPreview();
    updateNavButtons();
  } catch (e) { toast("Could not load the wizard page: " + e.message, true); }
};

if (store.get("pvSource", "0") === "1") showSource(true);
updateNavButtons();

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
      st.innerHTML = `<b>Publishing is unavailable:</b> ${esc(j.www)} is read-only.`;
    } else {
      st.style.display = "none";
    }
    $("#newsStoreHint").textContent = j.using_store
      ? `Saved to ${j.store}`
      : `Showing the default announcements. Saving creates ${j.store}.`;

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
    `<b>Unsaved draft restored</b> from ${esc(when.toLocaleString())}. It has not been
     saved or published.
     ${moved ? `<b>The saved announcements have changed since then</b>; saving will
       replace them. ` : ""}
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
    box.innerHTML = `<div class="empty">No announcements yet.</div>`;
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
              title="Content ID ${id}">
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
  el.innerHTML = info && info.differs
    ? `Note: this service shows the <b>${esc(info.icon)}</b> badge on the login ticker.`
    : "";
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
            : `<b style="color:var(--warn)">has nothing to open</b>: add a body or a link`);

  const pages = cid === 1
    ? "every Information page"
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
      body ? `<span class="mono">${it.serial || "…"}.pml</span>, with the body below.`
           : (link ? "none (uses the external link)" : "none (no body)"),
      !!body],
    ["Status / Maintenance",
      it.status ? `listed as <b>${k.maint === 2 ? "trouble" : k.maint === 3 ? "maintenance" : "other"}</b>.`
                : "not listed.", !!it.status],
  ];

  box.innerHTML = `<h4>Where this shows up</h4>` + rows.map(([w, v, on]) =>
    `<div class="er${on ? "" : " off"}"><span class="w">${w}</span>
       <span class="v">${v}</span></div>`).join("")
    ;
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
  if (!v) { el.innerHTML = "Shown on the Information and detail pages."; return; }
  el.innerHTML = NEWS_DATE_RE.test(v)
    ? ""
    : `Usual format: <code class="mono">${esc(newsDateString(new Date()))}</code>`;
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
    bits.push("<b>No layout</b> (data file)");
  }
  el.innerHTML = bits.join(" &middot; ");
}

// ---- overview ----
// Everything here is read from the same endpoints the tabs use, so the numbers
// cannot disagree with what a tab shows when you click through to it.
async function loadOverview() {
  const get = (u, ok) => (ok ? api(u).catch(() => null) : Promise.resolve(null));
  const [acc, codes, issues, reports, calls, desk, news] = await Promise.all([
    get("/api/accounts", can("accounts_view")), get("/api/codes", can("codes")),
    get("/api/issues", can("reports")), get("/api/reports", can("reports")),
    get("/api/gm-calls", can("gm")), get("/api/gm-desk", can("gm")),
    get("/api/news", isOwner())]);
  const A = Array.isArray(acc) ? acc : [], C = Array.isArray(codes) ? codes : [];
  const I = Array.isArray(issues) ? issues : [], R = Array.isArray(reports) ? reports : [];
  const G = Array.isArray(calls) ? calls : [];
  const notPlayable = A.filter((r) => (r.unlinked || []).length);
  const unused = C.filter((r) => !codeUsed(r)).length;
  const openCalls = G.filter((r) => r.status === "open");
  const newI = countNew("issues", I), newR = countNew("reports", R);
  const week = Date.now() - 7 * 86400e3;
  const newAcc = A.filter((r) => isoMs(r.created_at) > week).length;
  const card = (go, lab, num, sub, cls) => !tabAllowed(go) ? "" :
    `<button class="stat ${cls || ""}" data-go="${go}"><span class="lab">${lab}</span>` +
    `<span class="num">${num}</span><span class="sub">${sub}</span></button>`;
  const duty = desk ? (desk.on_duty ? "GM on duty" : "no GM on duty") : "GM service unavailable";
  $("#ovStats").innerHTML =
    card("accounts", "Accounts", acc ? A.length : "?",
         `${newAcc} new this week` + (notPlayable.length ? `, ${notPlayable.length} not playable` : ""),
         notPlayable.length ? "warn" : "") +
    card("codes", "Unused codes", codes ? unused : "?", `of ${C.length} total`) +
    card("gmcalls", "Open GM calls", calls ? openCalls.length : "?", duty,
         openCalls.length ? "warn" : "") +
    card("issues", "Issue reports", issues ? I.length : "?",
         newI ? `${newI} new` : "none new", newI ? "warn" : "") +
    card("reports", "User reports", reports ? R.length : "?",
         newR ? `${newR} new` : "none new", newR ? "warn" : "") +
    card("news", "Announcements", news && news.items ? news.items.length : "?",
         news && news.writable === false ? "publishing unavailable" : "published",
         news && news.writable === false ? "warn" : "");

  const li = (go, t1, t2, extra) =>
    `<li data-go="${go}"${extra || ""}><span class="t1">${t1}</span><span class="t2">${t2}</span></li>`;
  const recent = [...A].sort((x, y) => isoMs(y.created_at) - isoMs(x.created_at)).slice(0, 6);
  $("#ovAccounts").innerHTML = recent.map((r) =>
    li("accounts", `<code class="mono">${esc(r.polid)}</code> ${esc(r.handle || "")}`,
       esc(ago(isoMs(r.created_at))), ` data-q="${esc(r.polid)}"`)).join("")
    || `<li class="empty">No accounts yet.</li>`;

  const att = [];
  openCalls.slice(0, 4).forEach((r) => att.push(li("gmcalls",
    `GM call from <b>${esc(r.handle || "?")}</b>: ${esc(r.subject || "(no subject)")}`,
    esc(ago(isoMs(r.received_at))))));
  if (newI) att.push(li("issues", `${newI} new issue report${newI === 1 ? "" : "s"}`, ""));
  if (newR) att.push(li("reports", `${newR} new user report${newR === 1 ? "" : "s"}`, ""));
  notPlayable.slice(0, 4).forEach((r) => att.push(li("accounts",
    `<code class="mono">${esc(r.polid)}</code> has titles that cannot be played yet`,
    `${(r.unlinked || []).length} not linked`, ` data-q="${esc(r.polid)}"`)));
  if (news && news.writable === false)
    att.push(li("news", "News publishing is unavailable (read-only web root)", ""));
  if (codes && !unused && isOwner()) att.push(li("codes", "No unused registration codes left", ""));
  $("#ovAttention").innerHTML = att.join("") || `<li class="empty">Nothing needs attention.</li>`;
  setBadge("#issuesBadge", newI);
  setBadge("#reportsBadge", newR);
  loadOnline();
  if (isOwner()) loadHealth();
}

// Anything with data-go="tab" or "tab:fieldId" navigates; data-q pre-fills the
// accounts search so a click lands on that one account.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-go]");
  if (!el) return;
  const [tab, field] = el.dataset.go.split(":");
  if (el.dataset.q !== undefined && tab === "accounts") {
    if (can("accounts_view")) { e.preventDefault(); return openAccount(el.dataset.q); }
    $("#accSearch").value = el.dataset.q;
  }
  location.hash = "#" + tab;
  if (field) setTimeout(() => {
    const f = $("#" + field);
    if (!f) return;
    f.scrollIntoView({ behavior: "smooth", block: "center" });
    f.focus();
    flash(f.closest(".panel") || f);
  }, 60);
});

// ---- "new since you looked" badges for Issues and Reports ----
// Per browser, in localStorage. The first visit sets the baseline rather than
// calling the whole archive new.
function countNew(kind, rows) {
  const seen = +store.get("seen:" + kind, 0);
  if (!seen) { store.set("seen:" + kind, String(Date.now())); return 0; }
  return rows.filter((r) => isoMs(r.received_at) > seen).length;
}
function markSeen(kind) {
  store.set("seen:" + kind, String(Date.now()));
  setBadge(kind === "issues" ? "#issuesBadge" : "#reportsBadge", 0);
}
function setBadge(sel, n) {
  const b = $(sel);
  if (!b) return;
  b.textContent = n; b.hidden = !n;
}
async function loadBadges() {
  if (!SESSION.authenticated && SESSION.auth_required !== false) return;
  if (!can("reports")) return;
  const [i, r] = await Promise.all([api("/api/issues").catch(() => null),
                                    api("/api/reports").catch(() => null)]);
  if (Array.isArray(i) && CUR_TAB !== "issues") setBadge("#issuesBadge", countNew("issues", i));
  if (Array.isArray(r) && CUR_TAB !== "reports") setBadge("#reportsBadge", countNew("reports", r));
}
setInterval(loadBadges, 60000);

// ---- signed-in browsers ----
function uaName(ua) {
  ua = String(ua || "");
  const b = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera"
    : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "";
  const os = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android"
    : /iPhone|iPad/.test(ua) ? "iOS" : /Mac OS X/.test(ua) ? "macOS"
    : /Linux/.test(ua) ? "Linux" : "";
  return [b, os].filter(Boolean).join(" on ") || (ua ? ua.slice(0, 40) : "unknown browser");
}
async function loadSessions() {
  const body = $("#sessBody");
  let rows;
  try { rows = await api("/api/sessions"); } catch (e) { return; }
  $("#sessCount").textContent = `${rows.length} signed in`;
  $("#sessOthers").disabled = rows.filter((r) => !r.current).length === 0;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No sessions.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r) =>
    `<tr><td>${esc(uaName(r.ua))} <span class="muted">(${esc(r.user || "?")})</span>` +
      (r.current ? ` <span class="pill open">this browser</span>` : "") + `</td>` +
    `<td class="mono">${esc(r.addr || "")}</td>` +
    `<td class="muted">${esc(ago((r.created || 0) * 1000))}</td>` +
    `<td class="muted">${esc(ago((r.seen || 0) * 1000))}</td>` +
    `<td>${r.remember ? `<span class="pill open" title="Until ${esc(new Date((r.expires || 0) * 1000).toLocaleString())}">remembered</span>`
                      : `<span class="muted">this visit</span>`}</td>` +
    `<td style="text-align:right"><button class="danger sm" data-sid="${esc(r.id)}"` +
      ` data-cur="${r.current ? 1 : ""}">Sign out</button></td></tr>`).join("");
}
$("#sessBody").onclick = async (e) => {
  const b = e.target.closest("button[data-sid]");
  if (!b) return;
  if (b.dataset.cur && !confirm("Sign out this browser?")) return;
  b.disabled = true;
  try {
    const r = await api("/api/sessions/revoke", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: b.dataset.sid })
    });
    if (r.was_current) return location.reload();
    toast("Signed out");
    loadSessions();
  } catch (err) { b.disabled = false; toast(err.message, true); }
};
$("#sessOthers").onclick = async () => {
  if (!confirm("Sign out every other browser?")) return;
  try {
    const r = await api("/api/sessions/revoke", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ others: true })
    });
    toast(`Signed out ${r.revoked} other session${r.revoked === 1 ? "" : "s"}`);
    loadSessions();
  } catch (e) { toast(e.message, true); }
};

// ---- refresh + keyboard ----
$("#refreshBtn").onclick = () => { loadTab(CUR_TAB, true); toast("Refreshed"); };
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) {
    if (e.key === "Escape" && t.matches("[data-search]") && t.value) {
      t.value = ""; t.dispatchEvent(new Event("input"));
    }
    return;
  }
  if (document.querySelector(".modal.show")) return;
  if (e.key === "/") {
    const f = document.querySelector(`#tab-${CUR_TAB} [data-search]`)
      || (CUR_TAB === "pml" ? $("#fSearch") : null);
    if (f) { e.preventDefault(); f.focus(); f.select(); }
  } else if (e.key === "r" || e.key === "R") {
    $("#refreshBtn").click();
  } else if (/^[1-9]$/.test(e.key) && visibleTabs()[+e.key - 1]) {
    location.hash = "#" + visibleTabs()[+e.key - 1];
  }
});

// ---- moderators (owner) ----
let MODS = [], MOD_LABELS = {}, MOD_EDIT = null;
const post = (u, body) => api(u, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });

async function loadMods() {
  let r;
  try { r = await api("/api/mods"); } catch (e) { return; }
  MODS = r.mods || []; MOD_LABELS = r.perm_labels || {};
  $("#modCount").textContent = `${MODS.length} moderator${MODS.length === 1 ? "" : "s"}`;
  $("#modAdd").disabled = !r.owner_ready;
  $("#modAdd").title = r.owner_ready ? "" : "Set an owner password first";
  const body = $("#modsBody");
  if (!MODS.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No moderators. ` +
      `${r.owner_ready ? "" : "Set an owner password above before adding moderators."}</td></tr>`;
    return;
  }
  const short = { gm: "GM desk", codes: "Codes", reports: "Reports", accounts_view: "Look up accounts" };
  body.innerHTML = MODS.map((m, i) =>
    `<tr><td><b>${esc(m.username)}</b></td>` +
    `<td>${m.perms.length ? m.perms.map((p) => `<span class="permchip" title="${esc(MOD_LABELS[p] || "")}">${esc(short[p] || p)}</span>`).join("")
                          : `<span class="muted">nothing yet</span>`}</td>` +
    `<td>${m.perms.includes("codes") ? `${m.codes_24h} / ${m.code_limit ?? "no limit"}` : `<span class="muted">-</span>`}</td>` +
    `<td>${m.disabled ? `<span class="pill used">disabled</span>` : `<span class="pill open">active</span>`}</td>` +
    `<td class="muted">${m.last_seen ? esc(ago(m.last_seen * 1000)) + ` (${m.sessions} signed in)` : "not signed in"}</td>` +
    `<td><div class="rowacts">` +
      `<button class="ghost" data-mi="${i}" data-ma="edit">Edit</button>` +
      `<button class="ghost" data-mi="${i}" data-ma="password">New password</button>` +
      `<button class="ghost" data-mi="${i}" data-ma="toggle">${m.disabled ? "Enable" : "Disable"}</button>` +
      (m.sessions ? `<button class="ghost" data-mi="${i}" data-ma="signout">Sign out</button>` : "") +
      `<button class="danger" data-mi="${i}" data-ma="delete">Delete</button>` +
    `</div></td></tr>`).join("");
}

function showModResult(html) {
  const el = $("#modResult");
  el.innerHTML = html; el.hidden = false; flash(el);
}
const pwLine = (who, pw) =>
  `<div class="hot"><span>Password for ${esc(who)}</span><span class="mono">${esc(pw)}</span></div>` +
  `<div><span>Shown once</span><span>Copy it now. It cannot be shown again.</span></div>`;

$("#modsBody").onclick = async (e) => {
  const b = e.target.closest("button[data-ma]");
  if (!b) return;
  const m = MODS[+b.dataset.mi], act = b.dataset.ma;
  if (!m) return;
  if (act === "edit") return openModModal(m);
  if (act === "delete" && !confirm(`Delete moderator ${m.username}? They are signed out at once.`)) return;
  if (act === "password" && !confirm(`Give ${m.username} a new generated password? Their current one stops working and they are signed out.`)) return;
  b.disabled = true;
  try {
    const body = act === "toggle" ? { action: "update", id: m.id, disabled: !m.disabled }
                                  : { action: act, id: m.id };
    const r = await post("/api/mods", body);
    if (r.password) showModResult(pwLine(r.username, r.password));
    toast(act === "toggle" ? `${m.username} ${m.disabled ? "enabled" : "disabled"}`
      : act === "delete" ? `Deleted ${m.username}` : act === "signout" ? `Signed out ${m.username}`
      : `New password for ${m.username}`);
    loadMods(); loadAudit(); loadSessions();
  } catch (err) { b.disabled = false; toast(err.message, true); }
};

function openModModal(m) {
  MOD_EDIT = m || null;
  $("#modTitle").textContent = m ? `Edit ${m.username}` : "Add a moderator";
  $("#modUser").value = m ? m.username : "";
  $("#modUser").readOnly = !!m;
  $("#modLimit").value = m && m.code_limit !== null && m.code_limit !== undefined ? m.code_limit : "";
  $("#modPw").value = "";
  $("#modPwWrap").hidden = !!m;
  $("#modPerms").innerHTML = Object.entries(MOD_LABELS).map(([k, label]) =>
    `<label><input type="checkbox" value="${esc(k)}" ${m && m.perms.includes(k) ? "checked" : ""}> ${esc(label)}</label>`).join("");
  const own = new Set(((m && m.titles) || []).map(String));
  $("#modTitles").innerHTML = Object.entries(CONTENT).map(([code, name]) =>
    `<label class="chip"><input type="checkbox" value="${esc(code)}" ${own.has(code) ? "checked" : ""}> ${esc(name)}</label>`).join("");
  $("#modModal").classList.add("show");
  (m ? $("#modPerms input") : $("#modUser")).focus();
}
function closeModModal() { $("#modModal").classList.remove("show"); MOD_EDIT = null; }
$("#modAdd").onclick = () => openModModal(null);
$("#modCancel").onclick = closeModModal;
$("#modModal").onclick = (e) => { if (e.target === $("#modModal")) closeModModal(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#modModal").classList.contains("show")) closeModModal();
});
$("#modSave").onclick = async () => {
  const perms = [...document.querySelectorAll("#modPerms input:checked")].map((c) => c.value);
  const titles = [...document.querySelectorAll("#modTitles input:checked")].map((c) => +c.value);
  const lim = $("#modLimit").value.trim();
  const body = MOD_EDIT
    ? { action: "update", id: MOD_EDIT.id, perms, code_limit: lim === "" ? null : lim, titles }
    : { action: "create", username: $("#modUser").value.trim(), perms,
        code_limit: lim === "" ? null : lim, password: $("#modPw").value, titles };
  if (!MOD_EDIT && !body.username) return toast("Enter a username", true);
  if (!perms.length && !confirm("No permissions selected. Save anyway?")) return;
  const btn = $("#modSave");
  btn.disabled = true;
  try {
    const r = await post("/api/mods", body);
    closeModModal();
    if (r.password) showModResult(pwLine(r.username, r.password));
    else if (!MOD_EDIT && body.password) showModResult(
      `<div><span>Added ${esc(r.username)}</span><span>with the password you typed</span></div>`);
    toast(MOD_EDIT ? `Saved ${r.username}` : `Added ${r.username}`);
    loadMods(); loadAudit();
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
};

// ---- activity log (owner) ----
let AUDIT = [];
async function loadAudit() {
  const actor = $("#auditActor").value;
  let r;
  try { r = await api("/api/audit?limit=300" + (actor ? "&actor=" + encodeURIComponent(actor) : "")); }
  catch (e) { return; }
  AUDIT = r.rows || [];
  const sel = $("#auditActor"), keep = sel.value;
  sel.innerHTML = `<option value="">Everyone</option>` +
    (r.actors || []).map((a) => `<option>${esc(a)}</option>`).join("");
  sel.value = keep;
  renderAudit();
}
// Stored as JSON; read as "key: value" pairs.
function auditDetail(d) {
  if (!d) return "";
  let o;
  try { o = JSON.parse(d); } catch (e) { return d; }
  if (!o || typeof o !== "object") return String(o);
  return Object.entries(o).map(([k, v]) =>
    `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ");
}
function renderAudit() {
  const q = $("#auditSearch").value.trim().toLowerCase();
  const rows = AUDIT.filter((a) => !q || [a.action, a.target, a.detail, a.actor]
    .some((v) => String(v || "").toLowerCase().includes(q)));
  $("#auditCount").textContent = `${rows.length} shown`;
  $("#auditBody").innerHTML = rows.map((a) =>
    `<tr class="${a.ok ? "" : "audit-bad"}"><td class="muted" style="white-space:nowrap" title="${esc(new Date(a.at * 1000).toLocaleString())}">${esc(ago(a.at * 1000))}</td>` +
    `<td><b>${esc(a.actor)}</b>${a.role === "mod" ? ` <span class="muted">(mod)</span>` : ""}</td>` +
    `<td>${esc(a.action)}${a.ok ? "" : " (refused)"}</td>` +
    `<td class="mono">${esc(a.target || "")}</td>` +
    `<td class="muted" style="font-size:12px; overflow-wrap:anywhere">${esc(auditDetail(a.detail))}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Nothing recorded yet.</td></tr>`;
}
$("#auditActor").onchange = loadAudit;
$("#auditSearch").addEventListener("input", renderAudit);

// ---- a moderator's own password ----
$("#meSave").onclick = async () => {
  const pw = $("#meNew").value;
  if (pw !== $("#meNew2").value) return toast("The two new passwords do not match", true);
  if (pw.length < 8) return toast("Password must be at least 8 characters", true);
  try {
    await post("/api/me/password", { current: $("#meCurrent").value, password: pw });
    $("#meCurrent").value = $("#meNew").value = $("#meNew2").value = "";
    toast("Password changed. Your other browsers were signed out.");
    loadSessions();
  } catch (e) { toast(e.message, true); }
};
function renderMeState() {
  if (isOwner()) return;
  const labels = SESSION.perm_labels || {};
  const perms = SESSION.perms || [];
  $("#meState").innerHTML = `Signed in as moderator <b>${esc(SESSION.user)}</b>. ` +
    (perms.length ? "Access: " + perms.map((p) => esc(labels[p] || p)).join("; ") + "."
                  : "No access has been assigned yet.");
}

// ---- a moderator only sees the titles they may put on a code ----
function limitCodeChips() {
  const allowed = (SESSION.titles || []).map(String);
  if (isOwner() || !allowed.length) return;
  document.querySelectorAll("#contentChips label").forEach((lab) => {
    const cb = lab.querySelector("input");
    const ok = allowed.includes(cb.value);
    lab.hidden = !ok;
    if (!ok) cb.checked = false;
  });
  if (!document.querySelector("#contentChips input:checked")) {
    const first = document.querySelector("#contentChips label:not([hidden]) input");
    if (first) first.checked = true;
  }
}

// ---- Overview: who is on, and service health ----
async function loadOnline() {
  let r;
  try { r = await api("/api/online"); } catch (e) { return; }
  $("#ovGames").innerHTML = (r.games || []).map((g) =>
    `<span class="gamechip${g.fresh ? "" : " stale"}" title="${g.fresh ? "" : "No update for " + g.age + " s"}">` +
    `<b>${g.fresh ? g.count : "?"}</b>${esc(g.label)}</span>`).join("")
    || `<span class="muted">No game services report sessions here.</span>`;
  const list = $("#ovOnline");
  if (r.accounts === null) { list.innerHTML = ""; return; }
  list.innerHTML = r.accounts.slice(0, 12).map((a) =>
    `<li data-go="accounts" data-q="${esc(a.polid)}"><span class="t1"><code class="mono">${esc(a.polid)}</code> ` +
    `${esc(a.handle || "")}</span><span class="t2">since ${esc(ago(isoMs(a.since)))}` +
    `${a.addr ? " · " + esc(a.addr) : ""}</span></li>`).join("")
    + (r.accounts.length > 12 ? `<li class="empty">and ${r.accounts.length - 12} more</li>` : "")
    || `<li class="empty">Nobody is signed in.</li>`;
}

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}

async function loadHealth(fresh) {
  let r;
  try { r = await api("/api/health" + (fresh ? "?fresh=1" : "")); } catch (e) { return; }
  const rows = (r.services || []).map((sv) =>
    `<li><span class="t1"><span class="dot ${sv.ok ? "ok" : "bad"}"></span>${esc(sv.name)}</span>` +
    `<span class="t2">${sv.ok ? sv.ms + " ms" : esc(sv.error || "down")}</span></li>`);
  const b = r.backup;
  let bl;
  if (!b) bl = `<li><span class="t1"><span class="dot warn"></span>Backups</span><span class="t2">no status yet</span></li>`;
  else {
    const last = b.last_ok || (b.ok ? b : null);
    const age = last ? (Date.now() / 1000 - last.at) : Infinity;
    const cls = !b.ok ? "bad" : age > 36 * 3600 ? "warn" : "ok";
    bl = `<li><span class="t1"><span class="dot ${cls}"></span>Backups</span><span class="t2">` +
      (b.ok ? "" : "last run failed · ") +
      (last ? `last good ${esc(ago(last.at * 1000))}, ${esc(fmtBytes(last.bytes))}` : "none succeeded") +
      `</span></li>`;
  }
  $("#ovHealth").innerHTML = bl + rows.join("");
  const d = r.disk;
  $("#ovHealthFoot").innerHTML = (d ? `Disk: ${esc(fmtBytes(d.free))} free of ${esc(fmtBytes(d.total))}. ` : "") +
    `Checked ${esc(ago(r.checked_at * 1000))}. <a class="acct-link" id="healthRecheck">Check now</a>`;
  $("#healthRecheck").onclick = () => loadHealth(true);
}

// The Overview refreshes itself while it is on screen.
setInterval(() => { if (CUR_TAB === "overview" && !document.hidden) loadOverview(); }, 30000);

// ---- account detail ----
let ACCT = null;
async function openAccount(polid) {
  let r;
  try { r = await api("/api/account-detail?polid=" + encodeURIComponent(polid)); }
  catch (e) { return toast(e.message, true); }
  ACCT = r;
  const when = (iso) => iso ? `${esc(fmtWhen(iso))} (${esc(ago(isoMs(iso)))})` : "-";
  const kv = (k, v) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`;
  const sec = (title, inner) => `<div class="acct-sec"><h4>${title}</h4>${inner}</div>`;
  const none = (t) => `<div class="none">${t}</div>`;
  const m = r.members[0] || {};
  $("#acctTitle").innerHTML = `<code class="mono">${esc(r.polid)}</code> ${esc(r.handles[0] || "")}`;
  $("#acctSub").textContent = r.online.length ? "Signed in now" : "Not signed in";
  const status = r.status && r.status !== "active" ? `<span class="pill used">${esc(r.status)}</span>` : `<span class="pill open">active</span>`;
  let html = `<div class="acct-grid">`;
  html += sec("Account",
    kv("Status", status) + kv("Created", when(r.created_at)) +
    kv("Login name", esc(m.login_name || "-")) +
    kv("Handles", esc(r.handles.join(", ") || "-")) +
    kv("Mail", esc(m.mail || "-") + (m.ext_mail ? " (outside mail on)" : "")) +
    kv("Messages / friends", `${r.mail_count ?? "-"} / ${r.friends ?? "-"}`));
  html += sec("Signed in",
    r.online.length ? r.online.map((o) => kv("Since " + esc(ago(isoMs(o.since))), esc(o.addr || ""))).join("")
                    : none("Not signed in."));
  html += sec("Titles",
    r.owned.length ? r.owned.map((t) => kv(esc(t.label), t.linked ? `<span class="pill open">playable</span>`
                                                              : `<span class="pill used">not linked</span>`)).join("")
                   : none("No titles."));
  html += sec("Devices",
    r.devices.length ? r.devices.map((d) => kv(esc(d.known ? d.label : "Unknown device"),
                                                "last seen " + esc(ago(isoMs(d.last_seen))))).join("")
                     : none("No device has signed in to the lobby."));
  html += sec("Registration codes",
    r.codes.length ? r.codes.map((c) => kv(`<code class="mono">${esc(c.code)}</code>`,
                                            esc(c.contents_label || "") + " · " + esc(fmtWhen(c.redeemed_at)))).join("")
                   : none("None redeemed."));
  if (r.gm_calls !== null) html += sec("GM calls",
    r.gm_calls.length ? r.gm_calls.map((g) => kv(esc(g.subject || "(no subject)"), esc(ago(isoMs(g.received_at))))).join("")
                      : none("None."));
  if (r.reports !== null) html += sec("Reports about this player",
    r.reports.length ? r.reports.map((x) => kv(esc(x.explanation || "(no text)").slice(0, 80), esc(ago(isoMs(x.received_at))))).join("")
                     : none("None."));
  if (r.activity !== null) html += sec("Panel activity",
    r.activity.length ? r.activity.map((a) => kv(`${esc(a.actor)}: ${esc(a.action)}${a.ok ? "" : " (refused)"}`,
                                                  esc(ago(a.at * 1000)))).join("")
                      : none("No changes recorded."));
  html += `</div>`;
  $("#acctBody").innerHTML = html;
  $("#acctActions").innerHTML = isOwner()
    ? `<button class="ghost" data-a="grant">Content</button><button class="ghost" data-a="pw">Password</button>` +
      `<button class="ghost" data-a="tok">Tokens</button><button class="danger" data-a="del">Delete</button>`
    : "";
  $("#acctModal").classList.add("show");
}
function closeAccount() { $("#acctModal").classList.remove("show"); ACCT = null; }
$("#acctClose").onclick = closeAccount;
$("#acctModal").onclick = (e) => { if (e.target === $("#acctModal")) closeAccount(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#acctModal").classList.contains("show")) closeAccount();
});
$("#acctActions").onclick = (e) => {
  const b = e.target.closest("button[data-a]");
  if (!b || !ACCT) return;
  const polid = ACCT.polid, a = b.dataset.a;
  closeAccount();
  if (a === "grant") {
    location.hash = "#accounts";
    // The Accounts tab is (re)loading; aim the grant form once the row is there.
    setTimeout(() => prefillGrant(ACCOUNTS.find((x) => x.polid === polid) || { polid, contents: [] }), 700);
  }
  if (a === "pw") askPassword(polid);
  if (a === "tok") askTokens(polid);
  if (a === "del") askDelete(polid);
};

// ---- GM call alerts on this device (Web Push) ----
// Browsers only allow push on a secure origin (https, or localhost).
const pushSupported = () => window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window;
function urlB64ToBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function pushState() {
  if (!pushSupported()) return { supported: false };
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = reg ? await reg.pushManager.getSubscription() : null;
  let mine = [];
  try { mine = (await api("/api/push/key")).mine || []; } catch (e) {}
  return { supported: true, reg, sub, on: !!(sub && mine.includes(sub.endpoint)) };
}
async function renderAlertBtn() {
  const btn = $("#gmAlertBtn");
  if (!btn) return;
  const st = await pushState();
  if (!st.supported) {
    btn.textContent = "Alerts: unavailable";
    btn.disabled = true;
    btn.title = "Browser alerts need the panel to be opened over https.";
    return;
  }
  btn.disabled = false;
  btn.textContent = st.on ? "Alerts: on" : "Alerts: off";
  btn.className = st.on ? "act" : "ghost";
  btn.title = st.on ? "This device gets a notification for each new GM call" : "Get a notification on this device for each new GM call";
}
$("#gmAlertBtn").onclick = async () => {
  const btn = $("#gmAlertBtn");
  btn.disabled = true;
  try {
    const st = await pushState();
    if (st.on) {
      await post("/api/push/unsubscribe", { endpoint: st.sub.endpoint });
      await st.sub.unsubscribe();
      toast("Alerts turned off on this device");
    } else {
      if ((await Notification.requestPermission()) !== "granted") {
        toast("Notifications are blocked for this site in the browser settings", true);
        return;
      }
      const reg = st.reg || await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const { key } = await api("/api/push/key");
      let sub = await reg.pushManager.getSubscription();
      if (sub) await sub.unsubscribe();            // a key from an older setup
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToBytes(key) });
      await post("/api/push/subscribe", { subscription: sub.toJSON(), origin: location.origin });
      const t = await post("/api/push/test", {}).catch((e) => ({ error: e.message }));
      toast(t.error ? "Alerts on, but the test failed: " + t.error : "Alerts on. A test notification was sent.", !!t.error);
    }
  } catch (e) { toast(e.message, true); }
  finally { renderAlertBtn(); }
};

// ---- alert settings (owner, Security tab) ----
async function loadAlerts() {
  let r;
  try { r = await api("/api/alerts"); } catch (e) { return; }
  $("#discordUrl").value = "";
  $("#discordUrl").placeholder = r.discord_set ? "Saved. Paste a new URL to replace it." : "https://discord.com/api/webhooks/...";
  $("#discordHint").textContent = r.discord_set ? `(saved, ends ${r.discord_hint})` : "(not set)";
  $("#discordTest").disabled = $("#discordClear").disabled = !r.discord_set;
  $("#panelUrl").value = r.panel_url || "";
  $("#healthTargets").value = r.health_targets || "";
  $("#healthHost").value = r.health_host || "";
  $("#alertDevices").innerHTML = r.devices.map((d) =>
    `<tr><td>${esc(uaName(d.ua))}</td><td>${esc(d.username)}${d.role === "mod" ? " (mod)" : ""}</td>` +
    `<td class="muted">${esc(ago(d.created_at * 1000))}</td>` +
    `<td class="muted">${d.last_error ? `<span style="color:var(--bad)">${esc(d.last_error)}</span>`
                                      : d.last_ok ? esc(ago(d.last_ok * 1000)) : "not yet"}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No devices. Turn alerts on from the GM Calls tab.</td></tr>`;
  $("#alertRecent").innerHTML = r.recent.map((a) =>
    `<li><span class="t1">${esc(a.ticket.replace(/\.json$/, ""))}</span><span class="t2">${esc(a.result || "")} · ${esc(ago(a.at * 1000))}</span></li>`).join("")
    || `<li class="empty">No GM calls since alerts were set up.</li>`;
}
$("#alertsSave").onclick = async () => {
  try {
    await post("/api/alerts", { discord_webhook: $("#discordUrl").value.trim(), panel_url: $("#panelUrl").value.trim() });
    toast("Saved");
    loadAlerts();
  } catch (e) { toast(e.message, true); }
};
$("#discordTest").onclick = async () => {
  try { await post("/api/alerts", { action: "test_discord" }); toast("Test sent to Discord"); }
  catch (e) { toast(e.message, true); }
};
$("#discordClear").onclick = async () => {
  if (!confirm("Remove the Discord webhook? GM calls will no longer be posted there.")) return;
  try { await post("/api/alerts", { clear_discord: true }); toast("Webhook removed"); loadAlerts(); }
  catch (e) { toast(e.message, true); }
};
$("#checksSave").onclick = async () => {
  try {
    await post("/api/alerts", { health_targets: $("#healthTargets").value, health_host: $("#healthHost").value.trim() });
    toast("Checks saved");
  } catch (e) { toast(e.message, true); }
};

// ---- phone layout: label every card-table cell with its column ----
// Watches the tbodies rather than asking every renderer to remember.
function labelCells(tbody) {
  const heads = [...tbody.closest("table").querySelectorAll("thead th")]
    .map((th) => th.textContent.trim());
  tbody.querySelectorAll(":scope > tr").forEach((tr) => {
    [...tr.children].forEach((td, i) => {
      if (!td.hasAttribute("data-label")) td.setAttribute("data-label", heads[i] || "");
    });
  });
}
document.querySelectorAll("table.cards tbody").forEach((tb) => {
  new MutationObserver(() => labelCells(tb)).observe(tb, { childList: true });
});

// ---- installable app ----
// The service worker only adds an offline page (see sw.js); browsers refuse
// to run one over plain http, and the panel does not need it to work.
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
let INSTALL_PROMPT = null;
const isStandalone = () => matchMedia("(display-mode: standalone)").matches
  || navigator.standalone === true;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();                  // show our own button instead of the mini-bar
  INSTALL_PROMPT = e;
  $("#installBtn").hidden = false;
  renderAppState();
});
window.addEventListener("appinstalled", () => {
  INSTALL_PROMPT = null;
  $("#installBtn").hidden = true;
  toast("App installed");
  renderAppState();
});
$("#installBtn").onclick = async () => {
  if (!INSTALL_PROMPT) return;
  INSTALL_PROMPT.prompt();
  const r = await INSTALL_PROMPT.userChoice.catch(() => null);
  if (r && r.outcome === "accepted") $("#installBtn").hidden = true;
  INSTALL_PROMPT = null;
};
function renderAppState() {}
renderAppState();

// ---- boot ----
(async function () {
  await loadSession();
  applyRole();
  renderMeState();
  await loadContentNames();
  limitCodeChips();
  if (isOwner()) PML_LIST_READY = loadPmlFileList();
  // Seed the editor with a tiny sample so the preview isn't blank -- but not
  // when the hash names a file, or the restore would flash the sample first.
  if (!parseHash().path) $("#pml").value =
    `<pml><head>\n  <style name="hdr" face="6" size="18" color="#ffffffff">\n</head>\n<body>\n  <text pos="30,30" size="400,24" style="hdr">PlayOnline PML preview</text>\n  <scrollarea pos="30,70" size="200,24" skin="0" bgcolor="#efe9dcff" skincolor="#00000000"></scrollarea>\n  <input name="demo" type="text" pos="30,70" size="200,24" style="hdr" skin="1" skincolor="#efe9dcff" value="type here">\n</body></pml>`;
  updateGutter();
  if (!parseHash().path) renderPreview();
  applyHash();          // restore the tab, and the file if the hash names one
  loadBadges();
})();
