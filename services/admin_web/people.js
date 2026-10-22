// people.js -- People: the panel's record of one human across PlayOnline and the games
// (adminpeople.py). The list is #people, a person's page #people/<id> (or #people/pol/<POL ID>,
// which makes the person of a PlayOnline account the first time it is opened).
"use strict";

const PP = { filter: "all", rows: [], cur: null, timer: null, seq: 0, histF: "all", link: null };

const ppDate = (t) => {
  if (!t) return "";
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  if (isNaN(d)) return String(t).slice(5, 10);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return "today";
  const p2 = (n) => String(n).padStart(2, "0");
  return d.getFullYear() === today.getFullYear() ? `${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
    : `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
};
const ppSeen = (r) => r.online ? "now" : r.last_seen ? ago(isoMs(r.last_seen)) : "-";

function loadPeople() {
  const path = parseHash().path;
  const list = !path;
  $("#ppListView").hidden = !list;
  $("#ppPageView").hidden = list;
  if (list) { PP.cur = null; return loadPeopleList(); }
  if (path.startsWith("pol/")) return openPerson({ polid: path.slice(4) });
  return openPerson({ id: path });
}

async function loadPeopleList() {
  const seq = ++PP.seq;
  const q = $("#ppSearch").value.trim();
  let r;
  try { r = await api(`/api/people?q=${encodeURIComponent(q)}&f=${PP.filter}`); }
  catch (e) { $("#ppBody").innerHTML = `<tr><td colspan="5" class="muted">${esc(e.message)}</td></tr>`; return; }
  if (seq !== PP.seq) return;
  PP.rows = r.people || [];
  const core = r.core !== false;
  document.body.classList.toggle("pp-core-off", !core);
  $("#ppFilter").hidden = !core;
  $("#ppHint").textContent = core ? "* linked by staff, not yet proven by the player. A person without a "
    + "PlayOnline account is normal: they only play a game that signs in on its own." : "";
  $("#ppCount").textContent = PP.rows.length >= 100 ? "the first 100" : PP.rows.length ? `${PP.rows.length}` : "";
  $("#ppBody").innerHTML = PP.rows.map((p) => {
    const href = p.id ? `#people/${p.id}` : `#people/pol/${encodeURIComponent(p.polid || "")}`;
    const sub = p.pol ? (p.polid ? esc(p.polid) : "PlayOnline account") : (core ? "no PlayOnline account" : "");
    const accts = p.accounts.map((a) => esc(a.title) + (a.staff && core ? "*" : "")).join(", ") || "-";
    return `<tr><td data-label="Person"><a class="pp-name" href="${href}">${esc(p.name)}</a>`
      + (sub ? `<div class="pp-sub">${sub}</div>` : "") + `</td>`
      + `<td data-label="Accounts" class="muted">${accts}</td>`
      + `<td data-label="Playing now" class="pp-core">${p.online ? '<span class="dot ok" aria-hidden="true"></span> PlayOnline' : '<span class="muted">-</span>'}</td>`
      + `<td data-label="Last seen" class="muted pp-core">${esc(ppSeen(p))}</td>`
      + `<td data-label=""><a href="${href}">Open</a></td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">${q ? "Nobody matches." : core ? "No people yet." : "No people yet. Add one with New person, or link a login to them."}</td></tr>`;
}

$("#ppSearch").addEventListener("input", () => { clearTimeout(PP.timer); PP.timer = setTimeout(loadPeopleList, 250); });
$("#ppFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  PP.filter = b.dataset.f;
  document.querySelectorAll("#ppFilter button").forEach((x) => x.classList.toggle("on", x === b));
  loadPeopleList();
});
$("#ppNew").onclick = async () => {
  const name = (prompt("The new person's name, as staff will know them:") || "").trim();
  if (!name) return;
  try {
    const r = await post("/api/person", { action: "create", name });
    location.hash = "#people/" + r.id;
  } catch (e) { toast(e.message, true); }
};

// ---- a person's page ----
function linkLine(l) {
  const when = l.at ? ppDate(l.at) : "";
  const login = l.name && l.name !== l.key ? `${esc(l.name)} (id ${esc(l.key)})` : esc(l.key || l.name);
  if (l.system === "pol") {
    if (l.hidden) return "A PlayOnline account. Seeing it needs the accounts permission.";
    const hs = (PP.cur.handles || []);
    return `PlayOnline ID ${esc(l.key)}` + (hs.length ? `, ${hs.length > 1 ? "handles" : "handle"} ${esc(hs.join(", "))}` : "");
  }
  if (l.how === "staff") {
    return `${l.system === "discord" ? "Discord user" : "Login"} ${login}. ${esc(l.by || "Staff")} linked it ${esc(when)}`
      + (l.why ? `: "${esc(l.why)}"` : "");
  }
  if (l.source === "bot") return `${login}, through the Discord bot's link command`;
  return `Login ${login}, linked on the website${when ? " " + esc(when) : ""}`;
}

async function openPerson(which) {
  const view = $("#ppPageView");
  if (!PP.cur || String(PP.cur.id) !== String(which.id || "")) view.innerHTML = '<p class="muted">Loading...</p>';
  let p;
  const qs = which.id ? "id=" + encodeURIComponent(which.id) : "polid=" + encodeURIComponent(which.polid);
  try { p = await api("/api/person?" + qs); }
  catch (e) { view.innerHTML = `<div class="pp-crumb"><a href="#people">People</a></div><p class="muted">${esc(e.message)}</p>`; return; }
  if (String(p.id) !== String(which.id || "")) history.replaceState(null, "", "#people/" + p.id);
  PP.cur = p;
  renderPerson();
  loadPersonGames(p.id);
}

function renderPerson() {
  const p = PP.cur, core = p.core !== false;
  const since = ppDate(p.created_at);
  let status = `Person since ${esc(/^\d\d-\d\d$/.test(since) ? new Date().getFullYear() + "-" + since : since)}.`;
  if (p.online) status += " Signed in to PlayOnline now.";
  else if (p.last_seen) status += ` Last signed in ${esc(ago(isoMs(p.last_seen)))}.`;
  else if (core && !p.pol) status += " No PlayOnline account.";
  const pill = (l) => l.how === "staff" ? '<span class="pp-how staff">Linked by staff</span>' : '<span class="pp-how">Proven</span>';
  const cards = p.links.map((l) => `<div class="pp-card"><div class="pp-card-h"><b>${esc(l.title)}</b>${pill(l)}</div>`
    + `<div class="pp-card-d">${linkLine(l)}</div>`
    + (l.how === "staff" && p.can_link ? `<button class="pp-mini" data-unlink="${l.id}" data-what="${esc(l.title + " " + (l.name || l.key))}">Unlink</button>` : "")
    + `</div>`).join("") || `<div class="muted">No logins linked yet.</div>`;
  const verb = (a) => a.kind === "ban" ? `Ban in ${a.title}...` : `Kick from ${a.title}`;
  const acts = p.actions.map((a, i) => `<button class="${a.kind === "ban" ? "pp-btn bad" : "pp-btn"}" data-act="${i}" title="${esc(a.label)}">${esc(verb(a))}</button>`).join("");
  const games = (p.games || []).map((g) => `<a href="#games/${esc(g.key)}">${esc(g.title)} page</a>`).join(" · ");
  $("#ppPageView").innerHTML = `
    <div class="pp-crumb"><a href="#people">People</a> / ${esc(p.name)}</div>
    <div class="pp-head">
      <div class="pp-ava" aria-hidden="true">${esc((p.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="pp-title"><h2>${esc(p.name)}</h2><div class="muted">${status}</div></div>
      <div class="pp-grow"></div>
      ${p.polid && can("accounts_view") ? `<button class="ghost" id="ppAcct">PlayOnline account</button>` : ""}
      ${p.can_link ? `<button class="ghost" id="ppMergeBtn">Merge</button><button class="ghost" id="ppNoteBtn">Add a note</button>
      <button class="act" id="ppLinkBtn">Link an account</button>` : ""}
    </div>
    <div class="panel"><h2>Linked accounts</h2><div class="pp-cards">${cards}</div></div>
    <div class="pp-two">
      <div class="panel"><h2>In the games</h2><div id="ppGames"><div class="muted">Asking the games...</div></div></div>
      <div class="panel"><h2>Actions</h2>
        <div class="pp-acts">${acts || '<span class="muted">No game here has an action for this person.</span>'}</div>
        ${games ? `<div class="pp-games">${games}</div>` : ""}
        <div class="hint">Each game's own permission still applies. Every action is in the activity log.</div>
      </div>
    </div>
    <div class="panel"><div class="pp-histh"><h2>History</h2>
      <div class="seg" id="ppHistF"><button data-f="all">All</button><button data-f="people">Links</button>${p.pol && p.polid ? '<button data-f="account">Account</button>' : ""}<button data-f="note">Notes</button></div></div>
      <div id="ppHist"></div></div>
    ${p.can_link ? `<div class="panel" id="ppNotes"><h2>Add a note</h2>
      <textarea id="ppNoteText" rows="3" maxlength="2000" placeholder="Only staff see notes."></textarea>
      <div class="pp-foot"><button class="act" id="ppNoteSave">Save the note</button></div></div>` : ""}`;
  renderHistory();
}

function renderHistory() {
  const p = PP.cur;
  const items = p.history.map((h) => {
    let why = "";
    try { const d = JSON.parse(h.detail || "{}"); why = d.why || ""; } catch (e) { why = ""; }
    const text = h.kind === "people" ? `${esc(h.by)} ${esc(h.text)}${why ? `: "${esc(why)}"` : ""}.`
      : `${esc(h.by)}: ${esc(h.text)} (PlayOnline account).`;
    return { at: h.at, kind: h.kind, html: text };
  }).concat(p.notes.map((n) => ({ at: n.at, kind: "note", html: `Note by ${esc(n.by)}: "${esc(n.text)}"` })));
  items.sort((a, b) => b.at - a.at);
  const f = PP.histF;
  document.querySelectorAll("#ppHistF button").forEach((b) => b.classList.toggle("on", b.dataset.f === f));
  const shown = items.filter((i) => f === "all" || i.kind === f);
  $("#ppHist").innerHTML = shown.map((i) => `<div class="pp-hrow"><span class="pp-when">${esc(ppDate(i.at))}</span><span>${i.html}</span></div>`).join("")
    || `<div class="muted">Nothing yet.</div>`;
}

async function loadPersonGames(id) {
  let r;
  try { r = await api("/api/person/games?id=" + encodeURIComponent(id)); }
  catch (e) { if ($("#ppGames")) $("#ppGames").innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  const box = $("#ppGames");
  if (!box || !PP.cur || PP.cur.id !== id) return;
  const rows = [];
  (r.games || []).forEach((g) => {
    g.characters.forEach((c) => rows.push(`<div class="pp-grow-row"><span>${c.online ? '<span class="dot ok" aria-label="online"></span> ' : ""}<b>${esc(g.title)}</b>, ${esc(c.name)}</span><span class="muted">${esc(c.detail)}</span></div>`));
    if (g.note) rows.push(`<div class="muted">${esc(g.title)}: ${esc(g.note)}</div>`);
  });
  box.innerHTML = rows.join("") || `<div class="muted">${(r.asked || []).length ? "None of " + esc(r.asked.join(", ")) + " knows this person yet." : "No game here can look up players."}</div>`;
  if ((r.problems || []).length) box.innerHTML += `<div class="muted">${esc(r.problems.join("; "))}</div>`;
}

async function personPost(body) {
  return post("/api/person", Object.assign({ id: PP.cur.id }, body));
}

$("#ppPageView").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.closest("#ppAcct")) return openAccount(PP.cur.polid);
  if (t.closest("#ppLinkBtn")) return openLinkDialog();
  if (t.closest("#ppMergeBtn")) return openMergeDialog();
  if (t.closest("#ppNoteBtn")) { const n = $("#ppNoteText"); n.scrollIntoView({ block: "center" }); return n.focus(); }
  const hf = t.closest("#ppHistF button[data-f]");
  if (hf) { PP.histF = hf.dataset.f; return renderHistory(); }
  if (t.closest("#ppNoteSave")) {
    const text = $("#ppNoteText").value.trim();
    if (!text) return toast("Write the note first.", true);
    try { await personPost({ action: "note", text }); toast("Note saved"); openPerson({ id: PP.cur.id }); }
    catch (err) { toast(err.message, true); }
    return;
  }
  const un = t.closest("button[data-unlink]");
  if (un) {
    const why = (prompt(`Unlink ${un.dataset.what} from ${PP.cur.name}? Say why (kept in the history):`) || "").trim();
    if (!why) return;
    try { await personPost({ action: "unlink", link: +un.dataset.unlink, why }); toast("Unlinked"); openPerson({ id: PP.cur.id }); }
    catch (err) { toast(err.message, true); }
    return;
  }
  const ab = t.closest("button[data-act]");
  if (ab) {
    const a = PP.cur.actions[+ab.dataset.act];
    const params = Object.assign({}, a.params);
    for (const f of a.ask || []) {          // what the page cannot fill in: the moderator types it
      const v = prompt(`${a.label} in ${a.title}, ${PP.cur.name}: ${f.label}`, f.default || "");
      if (v === null) return;
      params[f.id] = v;
    }
    if (!(a.ask || []).length && !confirm(`${a.label} in ${a.title}: ${PP.cur.name}?`)) return;
    try {
      const r = await post("/api/modules/action", { key: a.game, action: a.id, params });
      toast(r.msg || "Done");
    } catch (err) { toast(err.message, true); }
  }
});

// ---- Link an account ----
function openLinkDialog() {
  const p = PP.cur;
  PP.link = { target: null, picked: null, pk: null };
  $("#ppLinkTitle").textContent = `Link an account to ${p.name}`;
  $("#ppLinkAbout").textContent = p.core !== false
    ? 'Find their account in a game and link it. It shows as "Linked by staff" until the player proves it on the website.'
    : 'Find their account in a game and link it. It shows as "Linked by staff", with your name and your reason.';
  $("#ppLinkGames").innerHTML = p.link_targets.map((t, i) => `<button type="button" data-t="${i}">${esc(t.title)}</button>`).join("");
  $("#ppLinkWhy").value = "";
  $("#ppLinkWho").innerHTML = "";
  $("#ppLinkWhoWrap").hidden = true;
  $("#ppLinkErr").hidden = true;
  linkWarn();
  $("#ppLinkModal").classList.add("show");
  const first = p.link_targets.findIndex((t) => t.search);
  if (first >= 0) pickLinkTarget(first);
}

function pickLinkTarget(i) {
  const t = PP.cur.link_targets[i];
  PP.link.target = t; PP.link.picked = null;
  document.querySelectorAll("#ppLinkGames button").forEach((b) => b.classList.toggle("on", +b.dataset.t === i));
  $("#ppLinkWhoWrap").hidden = false;
  $("#ppLinkErr").hidden = true;
  const box = $("#ppLinkWho");
  box.innerHTML = "";
  if (t.search) {
    $("#ppLinkWhoLabel").textContent = `Their ${t.title} account`;
    // the rows the picker showed, to know whom a picked login belongs to already
    let shown = [];
    PP.link.pk = makePicker({
      id: "ppLinkQ", placeholder: `Search ${t.title} by name or id`,
      search: (q) => api(`/api/people/logins?game=${encodeURIComponent(t.game)}&q=${encodeURIComponent(q)}`)
        .then((r) => (shown = r.players || []).map((x) => Object.assign({}, x, {
          detail: [x.detail, x.person ? (x.person.id === PP.cur.id ? "Already linked to " + PP.cur.name
            : `Already linked to ${x.person.name} (another person)`) : ""].filter(Boolean).join(". ")}))),
      onpick: (row) => {
        PP.link.picked = Object.assign({ name: row.label, key: row.id },
                                       shown.find((x) => String(x.id) === row.id) || {});
        linkWarn();
      },
    });
    box.appendChild(PP.link.pk.el);
    PP.link.pk.box.addEventListener("input", () => { PP.link.picked = null; linkWarn(); });
  } else {
    $("#ppLinkWhoLabel").textContent = "Their Discord user id";
    box.innerHTML = `<input type="text" id="ppLinkDid" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="for example 123456789012345678">`
      + `<label for="ppLinkDname">Their Discord name (optional)</label><input type="text" id="ppLinkDname" autocomplete="off" spellcheck="false">`;
    PP.link.pk = null;
    box.querySelectorAll("input").forEach((x) => x.addEventListener("input", linkWarn));
  }
  linkWarn();
}

function linkChoice() {
  const t = PP.link && PP.link.target;
  if (!t) return null;
  if (t.search) {
    const pk = PP.link.picked;
    return pk ? { system: t.system, key: pk.id || pk.key, name: pk.label || pk.name, person: pk.person } : null;
  }
  const id = ($("#ppLinkDid") || {}).value || "";
  if (!id.trim()) return null;
  return { system: "discord", key: id.trim(), name: (($("#ppLinkDname") || {}).value || "").trim() || id.trim() };
}

function linkWarn() {
  const c = linkChoice(), p = PP.cur, t = PP.link && PP.link.target;
  const go = $("#ppLinkGo"), warn = $("#ppLinkWarn");
  if (!c) {
    go.textContent = "Link"; go.disabled = true;
    warn.textContent = t ? (t.search ? `Search for their ${t.title} account and pick it.` : "Type their Discord user id.") : "Pick a game.";
    return;
  }
  go.textContent = `Link ${c.name}`;
  if (c.person && c.person.id !== p.id) {
    go.disabled = true;
    warn.innerHTML = `${esc(c.name)} already belongs to ${esc(c.person.name)}. A login belongs to one person: `
      + `<a href="#people/${c.person.id}" id="ppLinkOther">open ${esc(c.person.name)}</a> and merge the two people if they are the same person.`;
    return;
  }
  if (c.person) { go.disabled = true; warn.textContent = `That login is linked to ${p.name} already.`; return; }
  go.disabled = false;
  if (c.system === "discord") {
    const who = c.name === c.key ? `Discord user ${c.key}` : `${c.name} (Discord user ${c.key})`;
    warn.textContent = `${who} will be listed on ${p.name}'s page as their Discord account, marked as linked by staff. `
      + `Moderators who may see linked accounts will see it.`;
    return;
  }
  warn.textContent = `${c.name} will be ${p.name}'s ${t.title} account: its characters show on ${p.name}'s page, `
    + `and ${t.title}'s actions on that page (such as kick or ban) act on it. Moderators who may see linked accounts will see the link.`;
}

$("#ppLinkGames").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-t]");
  if (b) pickLinkTarget(+b.dataset.t);
});
$("#ppLinkCancel").onclick = () => $("#ppLinkModal").classList.remove("show");
$("#ppLinkModal").addEventListener("click", (e) => {
  if (e.target === $("#ppLinkModal") || e.target.closest("#ppLinkOther")) $("#ppLinkModal").classList.remove("show");
});
$("#ppLinkGo").onclick = async () => {
  const c = linkChoice();
  const why = $("#ppLinkWhy").value.trim();
  const err = $("#ppLinkErr");
  if (!c) return;
  if (!why) { err.textContent = "Say why you are linking it: it is kept in the history."; err.hidden = false; return; }
  try {
    await personPost({ action: "link", system: c.system, key: c.key, name: c.name, why });
    $("#ppLinkModal").classList.remove("show");
    toast(`Linked ${c.name}`);
    openPerson({ id: PP.cur.id });
  } catch (e) { err.textContent = e.message; err.hidden = false; }
};

// ---- Merge ----
function openMergeDialog() {
  const p = PP.cur;
  $("#ppMergeTitle").textContent = `Merge someone into ${p.name}`;
  $("#ppMergeAbout").textContent = `Their linked logins and notes move to ${p.name}, and their page leads here from now on. `
    + "Two people who each have a PlayOnline account stay two people.";
  $("#ppMergeErr").hidden = true;
  $("#ppMergeForce").checked = false;
  $("#ppMergeForceWrap").hidden = !p.can_force || p.core === false;
  const box = $("#ppMergeWho");
  box.innerHTML = "";
  PP.merge = makePicker({
    id: "ppMergeQ", placeholder: "Search people by name, ID or login",
    search: (q) => api(`/api/people?q=${encodeURIComponent(q)}`).then((r) => (r.people || [])
      .filter((x) => x.id !== p.id).slice(0, 20)
      .map((x) => ({ id: x.id ? "id:" + x.id : "pol:" + x.polid, label: x.name, online: x.online === null ? null : !!x.online,
                     detail: [x.polid || (x.pol ? "PlayOnline account" : p.core !== false ? "no PlayOnline account" : ""),
                              x.accounts.map((a) => a.title).join(", ")].filter(Boolean).join(". ") }))),
  });
  box.appendChild(PP.merge.el);
  $("#ppMergeModal").classList.add("show");
}
$("#ppMergeCancel").onclick = () => $("#ppMergeModal").classList.remove("show");
$("#ppMergeModal").addEventListener("click", (e) => { if (e.target === $("#ppMergeModal")) $("#ppMergeModal").classList.remove("show"); });
$("#ppMergeGo").onclick = async () => {
  const pk = PP.merge && PP.merge.picked();
  const err = $("#ppMergeErr");
  if (!pk) { err.textContent = "Search for the person to merge and pick them."; err.hidden = false; return; }
  if (!confirm(`Merge ${pk.label} into ${PP.cur.name}? ${pk.label}'s page then leads to ${PP.cur.name}.`)) return;
  const body = { action: "merge", force: $("#ppMergeForce").checked };
  if (pk.id.startsWith("id:")) body.other = pk.id.slice(3); else body.other_polid = pk.id.slice(4);
  try {
    await personPost(body);
    $("#ppMergeModal").classList.remove("show");
    toast(`Merged ${pk.label} into ${PP.cur.name}`);
    openPerson({ id: PP.cur.id });
  } catch (e) { err.textContent = e.message; err.hidden = false; }
};
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  ["#ppLinkModal", "#ppMergeModal"].forEach((m) => $(m).classList.remove("show"));
});
