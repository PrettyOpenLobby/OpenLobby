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
  $("#ppFilter").querySelector('[data-f="online"]').hidden = !core;
  $("#ppHint").textContent = core ? "* linked by staff, not yet proven by the player. A person without a "
    + "PlayOnline account is normal: they only play a game that signs in on its own." : "";
  $("#ppCount").textContent = PP.rows.length >= 100 ? "the first 100" : PP.rows.length ? `${PP.rows.length}` : "";
  $("#ppBody").innerHTML = PP.rows.map((p) => {
    const href = p.id ? `#people/${p.id}` : `#people/pol/${encodeURIComponent(p.polid || "")}`;
    let sub = p.pol ? (p.polid ? esc(p.polid) : "PlayOnline account") : (core ? "no PlayOnline account" : "");
    const maybe = (p.maybe || []).map((o) => {
      // the one without a PlayOnline account goes into the one with it; else the newer into the older
      const into = can("people_link") && p.id && !(p.pol && o.pol) && ((!p.pol && o.pol) || (!p.pol && !o.pol && p.id > o.id));
      return `May be <a href="#people/${o.id}">${esc(o.name)}</a>: ${esc(o.why)}`
        + (into ? ` <button type="button" class="pp-mini" data-merge-from="${p.id}" data-merge-into="${o.id}"`
          + ` data-from-name="${esc(p.name)}" data-into-name="${esc(o.name)}">Merge into ${esc(o.name)}...</button>` : "");
    });
    if (maybe.length) sub = (sub ? sub + "<br>" : "") + `<span class="pp-maybe">${maybe.join("<br>")}</span>`;
    const accts = p.accounts.map((a) => esc(a.title) + (a.staff && core ? "*" : "")).join(", ") || "-";
    const face = p.avatar
      ? `<img class="pp-lava" src="${esc(p.avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
      : `<div class="pp-lava mono" aria-hidden="true">${esc((p.name || "?").slice(0, 1).toUpperCase())}</div>`;
    return `<tr><td data-label="Person"><div class="pp-prow">${face}<div class="pp-pwho">`
      + `<a class="pp-name" href="${href}">${esc(p.name)}</a>`
      + (sub ? `<div class="pp-sub">${sub}</div>` : "") + `</div></div></td>`
      + `<td data-label="Accounts" class="muted">${accts}</td>`
      + `<td data-label="Playing now" class="pp-core">${p.online ? '<span class="dot ok" aria-hidden="true"></span> PlayOnline' : '<span class="muted">-</span>'}</td>`
      + `<td data-label="Last seen" class="muted pp-core">${esc(ppSeen(p))}</td>`
      + `<td data-label=""><a href="${href}">Open</a></td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">${PP.filter === "dupes" ? "No two people look alike." : q ? "Nobody matches." : core ? "No people yet." : "No people yet. Add one with New person, or link a login to them."}</td></tr>`;
}

$("#ppBody").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-merge-from]");
  if (!b) return;
  const from = b.dataset.fromName, into = b.dataset.intoName;
  if (!await uiAsk({ title: `Merge ${from} into ${into}?`, ok: "Merge",
                     text: `${from}'s linked logins and notes move to ${into}, and ${from}'s page leads to ${into} from now on. `
                       + "Only merge them if you know they are the same person." })) return;
  try {
    await post("/api/person", { action: "merge", id: +b.dataset.mergeInto, other: +b.dataset.mergeFrom });
    toast(`Merged ${from} into ${into}`);
    loadPeopleList();
  } catch (err) { toast(err.message, true); }
});

$("#ppSearch").addEventListener("input", () => { clearTimeout(PP.timer); PP.timer = setTimeout(loadPeopleList, 250); });
$("#ppFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  PP.filter = b.dataset.f;
  document.querySelectorAll("#ppFilter button").forEach((x) => x.classList.toggle("on", x === b));
  loadPeopleList();
});
$("#ppNew").onclick = async () => {
  const got = await uiAsk({ title: "New person", ok: "Add the person",
                            fields: [{ id: "name", label: "Their name, as staff will know them", max: 80, required: true }] });
  if (!got) return;
  const name = got.name;
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
  const cards = p.links.map((l) => {
    const cava = l.avatar ? `<img class="pp-cava" src="${esc(l.avatar)}" alt="" referrerpolicy="no-referrer">` : "";
    return `<div class="pp-card"><div class="pp-card-h"><b>${cava}${esc(l.title)}</b>${pill(l)}</div>`
      + `<div class="pp-card-d">${linkLine(l)}</div>`
      + (l.how === "staff" && p.can_link ? `<button class="pp-mini" data-unlink="${l.id}" data-what="${esc(l.title + " " + (l.name || l.key))}">Unlink</button>` : "")
      + `</div>`;
  }).join("") || `<div class="muted">No logins linked yet.</div>`;
  const verb = (a) => a.kind === "ban" ? `Ban in ${a.title}...` : `Kick from ${a.title}`;
  const acts = p.actions.map((a, i) => `<button class="${a.kind === "ban" ? "pp-btn bad" : "pp-btn"}" data-act="${i}" title="${esc(a.label)}">${esc(verb(a))}</button>`).join("");
  const games = (p.games || []).map((g) => `<a href="#games/${esc(g.key)}">${esc(g.title)} page</a>`).join(" · ");
  const headAva = p.avatar
    ? `<img class="pp-ava" src="${esc(p.avatar)}" alt="Discord avatar" referrerpolicy="no-referrer">`
    : `<div class="pp-ava" aria-hidden="true">${esc((p.name || "?").slice(0, 1).toUpperCase())}</div>`;
  $("#ppPageView").innerHTML = `
    <div class="pp-crumb"><a href="#people">People</a> / ${esc(p.name)}</div>
    <div class="pp-head">
      ${headAva}
      <div class="pp-title"><h2>${esc(p.name)}</h2><div class="muted">${status}</div></div>
      <div class="pp-grow"></div>
      ${p.polid && can("accounts_view") ? `<button class="ghost" id="ppAcct">PlayOnline account</button>` : ""}
      ${p.can_link ? `<button class="ghost" id="ppRenameBtn">Rename</button><button class="ghost" id="ppMergeBtn">Merge</button><button class="ghost" id="ppNoteBtn">Add a note</button>
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
    ${tabAllowed("inbox") ? `<div class="panel pp-inbox"><h2>Inbox<a class="h2act" href="#inbox/person/${p.id}">All their cases</a></h2>
      <ul class="ib-list" id="ppInbox"><li class="empty">Reading the Inbox...</li></ul></div>` : ""}
    <div class="panel"><div class="pp-histh"><h2>History</h2>
      <div class="seg" id="ppHistF"><button data-f="all">All</button><button data-f="people">Links</button>${p.pol && p.polid ? '<button data-f="account">Account</button>' : ""}<button data-f="note">Notes</button></div></div>
      <div id="ppHist"></div></div>
    ${p.can_link ? `<div class="panel" id="ppNotes"><h2>Add a note</h2>
      <textarea id="ppNoteText" rows="3" maxlength="2000" placeholder="Only staff see notes."></textarea>
      <div class="pp-foot"><button class="act" id="ppNoteSave">Save the note</button></div></div>` : ""}`;
  renderHistory();
  if (tabAllowed("inbox")) loadPersonInbox(p.id);
}

// The person's cases from every source the moderator may read (admininbox.py).
async function loadPersonInbox(id) {
  let r;
  try { r = await api(`/api/inbox?person=${encodeURIComponent(id)}&state=all&limit=10`); }
  catch (e) { if ($("#ppInbox")) $("#ppInbox").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  const box = $("#ppInbox");
  if (!box || !PP.cur || PP.cur.id !== id) return;
  PP.inbox = r.threads || [];
  // on their own page, their name needs no link to it
  box.innerHTML = (ibRows(PP.inbox.map((t) => Object.assign({}, t, { person: null })), false)
    + (r.total > PP.inbox.length ? `<li class="empty"><a href="#inbox/person/${id}">${r.total - PP.inbox.length} older</a></li>` : "")
    + ((r.problems || []).length ? `<li class="empty">${esc(r.problems.join(". "))}.</li>` : ""))
    || `<li class="empty">No cases from ${esc(PP.cur.name)}.</li>`;
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
  const ib = t.closest("#ppInbox li[data-ib]");
  if (ib && !t.closest("a")) {
    Object.assign(IB, { source: "all", state: "all", game: "", offset: 0, cur: ib.dataset.ib });
    location.hash = "#inbox/person/" + PP.cur.id;
    return;
  }
  if (t.closest("#ppRenameBtn")) {
    const got = await uiAsk({ title: `Rename ${PP.cur.name}`, ok: "Rename", text: "The name staff know them by. The old name stays in the history.",
                              fields: [{ id: "name", label: "Name", value: PP.cur.name, max: 80, required: true }] });
    if (!got || got.name === PP.cur.name) return;
    try { await personPost({ action: "rename", name: got.name }); toast("Renamed"); openPerson({ id: PP.cur.id }); }
    catch (err) { toast(err.message, true); }
    return;
  }
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
    const got = await uiAsk({ title: `Unlink ${un.dataset.what}?`, ok: "Unlink", danger: true,
                              text: `It stops being ${PP.cur.name}'s. The link and your reason stay in the history.`,
                              fields: [{ id: "why", label: "Why", rows: 2, max: 300, required: "Say why you are unlinking it." }] });
    if (!got) return;
    const why = got.why;
    try { await personPost({ action: "unlink", link: +un.dataset.unlink, why }); toast("Unlinked"); openPerson({ id: PP.cur.id }); }
    catch (err) { toast(err.message, true); }
    return;
  }
  const ab = t.closest("button[data-act]");
  if (ab) {
    const a = PP.cur.actions[+ab.dataset.act];
    const params = Object.assign({}, a.params);
    // what the page cannot fill in, the moderator types
    const got = await uiAsk({ title: `${a.label} in ${a.title}`, ok: a.label, danger: a.kind === "ban" || a.danger,
                              text: `For ${PP.cur.name}.`,
                              fields: (a.ask || []).map((f) => ({ id: f.id, label: f.label, value: f.default || "" })) });
    if (!got) return;
    if (got !== true) Object.assign(params, got);
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
  $("#ppLinkGames").hidden = $("#ppLinkGames").previousElementSibling.hidden = false;
  $("#ppLinkWhoLabel").hidden = false;
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
  if (PP.link && PP.link.mode === "login") return loginLinkWarn();
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
// The same dialog the other way round, from the Inbox: a game login that is nobody's yet, linked
// to a person picked here (or to a new person). lg: {system, game, key, name, title, search}.
function openLoginLinkDialog(lg, after) {
  PP.link = { mode: "login", lg, after, login: lg.key ? { key: lg.key, name: lg.name || lg.key } : null, person: null };
  $("#ppLinkTitle").textContent = `Link ${lg.name || lg.key} to a person`;
  $("#ppLinkAbout").textContent = `${lg.name || lg.key} asked for help in ${lg.title || "a game"}, and nobody here has that login yet. `
    + "Pick the person it belongs to. It shows as \"Linked by staff\" on their page.";
  $("#ppLinkGames").hidden = $("#ppLinkGames").previousElementSibling.hidden = true;
  $("#ppLinkWhy").value = "";
  $("#ppLinkErr").hidden = true;
  $("#ppLinkWhoWrap").hidden = false;
  $("#ppLinkWhoLabel").textContent = "The person";
  const box = $("#ppLinkWho");
  box.innerHTML = "";
  if (!lg.key && lg.search) {
    // a help desk names the player, not always their login's id: find the login in the game
    const lab = document.createElement("label");
    lab.textContent = `Their ${lg.title} account`;
    lab.htmlFor = "ppLinkLoginQ";
    const pk = makePicker({
      id: "ppLinkLoginQ", placeholder: `Search ${lg.title} by name or id`,
      search: (q) => api(`/api/people/logins?game=${encodeURIComponent(lg.game)}&q=${encodeURIComponent(q)}`)
        .then((r) => (r.players || []).map((x) => Object.assign({}, x, {
          detail: [x.detail, x.person ? `Already linked to ${x.person.name}` : ""].filter(Boolean).join(". ") }))),
      onpick: (row) => { PP.link.login = { key: row.id, name: row.label }; linkWarn(); },
    });
    pk.box.value = lg.name || "";
    pk.box.addEventListener("input", () => { PP.link.login = null; linkWarn(); });
    box.append(lab, pk.el);
    $("#ppLinkWhoLabel").hidden = true;
    const lab2 = document.createElement("label");
    lab2.textContent = "The person";
    lab2.htmlFor = "ppLinkPersonQ";
    box.append(lab2);
  } else {
    if (!lg.key) PP.link.login = { key: lg.name, name: lg.name };
    $("#ppLinkWhoLabel").hidden = false;
  }
  const ppk = makePicker({
    id: "ppLinkPersonQ", placeholder: "Search people by name, ID or login",
    search: (q) => api(`/api/people?q=${encodeURIComponent(q)}`).then((r) => [{ id: "new:", label: `A new person named ${lg.name || lg.key}`,
      detail: "Made now, with this login", online: null }].concat((r.people || []).slice(0, 19).map((x) => ({
        id: x.id ? "id:" + x.id : "pol:" + x.polid, label: x.name, online: x.online === null ? null : !!x.online,
        avatar: x.avatar || null,
        detail: [x.polid || "", x.accounts.map((a) => a.title).join(", ")].filter(Boolean).join(". ") })))),
    onpick: (row) => { PP.link.person = row; linkWarn(); },
  });
  ppk.box.addEventListener("input", () => { PP.link.person = null; linkWarn(); });
  box.append(ppk.el);
  linkWarn();
  $("#ppLinkModal").classList.add("show");
}

function loginLinkWarn() {
  const L = PP.link, go = $("#ppLinkGo"), warn = $("#ppLinkWarn");
  go.disabled = !(L.login && L.person);
  go.textContent = L.login ? `Link ${L.login.name}` : "Link";
  if (!L.login) { warn.textContent = `Find their ${L.lg.title} account and pick it.`; return; }
  if (!L.person) { warn.textContent = "Search for the person and pick them, or pick a new person."; return; }
  const who = L.person.id === "new:" ? `a new person named ${L.lg.name || L.login.name}` : L.person.label;
  warn.textContent = `${L.login.name} will be ${who}'s ${L.lg.title} account: its help requests and characters show on their page.`;
}

async function loginLinkGo() {
  const L = PP.link, err = $("#ppLinkErr");
  const why = $("#ppLinkWhy").value.trim();
  if (!why) { err.textContent = "Say why you are linking it: it is kept in the history."; err.hidden = false; return; }
  try {
    let id;
    if (L.person.id === "new:") id = (await post("/api/person", { action: "create", name: L.lg.name || L.login.name })).id;
    else if (L.person.id.startsWith("id:")) id = +L.person.id.slice(3);
    else id = (await api("/api/person?polid=" + encodeURIComponent(L.person.id.slice(4)))).id;
    await post("/api/person", { action: "link", id, system: L.lg.system, key: L.login.key, name: L.login.name, why });
    $("#ppLinkModal").classList.remove("show");
    toast(`Linked ${L.login.name}`);
    if (L.after) L.after();
  } catch (e) { err.textContent = e.message; err.hidden = false; }
}

$("#ppLinkGo").onclick = async () => {
  if (PP.link && PP.link.mode === "login") return loginLinkGo();
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
                     avatar: x.avatar || null,
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
  if (!await uiAsk({ title: `Merge ${pk.label} into ${PP.cur.name}?`, ok: "Merge",
                     text: `${pk.label}'s page leads to ${PP.cur.name} from now on.` })) return;
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
