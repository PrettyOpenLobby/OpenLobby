// helpdesk.js -- the Inbox's Game help (adminhelp.py) and the account window's "In the games"
// section (adminplayers.py).
"use strict";

// ---- Inbox > Game help ----
// Each game's help desk needs that game's help permission; the owner sees them all.
const canGameHelp = () => isOwner() || (SESSION.perms || []).some((p) => p.startsWith("game.") && p.endsWith(".help"));
let GH = { open: true, rows: [], cur: null };

async function loadGameHelp() {
  $("#ghList").innerHTML = '<li class="empty">Loading...</li>';
  let r;
  try { r = await api("/api/help?open=" + (GH.open ? "1" : "0")); }
  catch (e) { $("#ghList").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  GH.rows = r.threads || [];
  $("#ghProblems").textContent = (r.problems || []).join("; ");
  $("#ghProblems").hidden = !(r.problems || []).length;
  $("#ghNone").hidden = (r.games || []).length > 0;
  const waiting = GH.rows.filter((t) => t.waiting && !t.closed).length;
  $("#ghCount").textContent = waiting ? `${waiting} waiting for an answer` : "";
  $("#ghList").innerHTML = GH.rows.map((t) => `<li data-gh="${esc(t.game)}|${esc(t.id)}" class="${GH.cur && GH.cur.game === t.game && GH.cur.id === t.id ? "on" : ""}">`
    + `<span class="t1">${t.waiting && !t.closed ? '<span class="dot warn" aria-label="waiting"></span>' : ""}<b>${esc(t.subject)}</b>`
    + `<span class="muted"> ${esc(t.who || "?")}, ${esc(t.game_title)}${t.category ? ", " + esc(t.category) : ""}</span></span>`
    + `<span class="t2">${t.closed ? "closed" : t.waiting ? "waiting" : "answered"}${t.at ? ", " + esc(ago(t.at * 1000)) : ""}</span></li>`).join("")
    || `<li class="empty">${GH.open ? "No open requests." : "No requests."}</li>`;
  if (GH.cur) showGameHelp(GH.cur.game, GH.cur.id, true);
}

async function showGameHelp(game, id, quiet) {
  GH.cur = { game, id };
  document.querySelectorAll("#ghList li[data-gh]").forEach((li) => li.classList.toggle("on", li.dataset.gh === `${game}|${id}`));
  const box = $("#ghThread");
  if (!quiet) box.innerHTML = '<p class="muted">Loading...</p>';
  let t;
  try { t = await api(`/api/help/thread?game=${encodeURIComponent(game)}&id=${encodeURIComponent(id)}`); }
  catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const draft = $("#ghText") ? $("#ghText").value : "";
  box.innerHTML = `<h3>${esc(t.subject || "(no subject)")}</h3>`
    + `<p class="muted">${esc(t.who.name || "?")} in ${esc(t.game_title)}`
    + `${t.who.online === true ? ", online now" : t.who.online === false ? ", not online" : ""}${t.closed ? ". Closed." : ""}</p>`
    + `<div class="gh-msgs">${t.messages.map((m) => `<div class="gh-msg ${m.from}">`
      + `<div class="gh-meta">${m.from === "staff" ? "Staff" : esc(t.who.name || "Player")}${m.at ? ", " + esc(ago(m.at * 1000)) : ""}</div>`
      + `${m.subject && m.subject !== t.subject ? `<div class="strong">${esc(m.subject)}</div>` : ""}<div class="gh-text">${esc(m.text)}</div></div>`).join("")}</div>`
    + `<label class="gh-answer">Answer<textarea id="ghText" rows="4" maxlength="4000"></textarea></label>`
    + `<div class="gh-acts"><label class="chip"><input type="checkbox" id="ghClose"> Close it after</label>`
    + `<span class="grow"></span>${t.closed ? "" : '<button class="ghost" id="ghCloseOnly">Close without answering</button>'}`
    + `<button class="act" id="ghSend">Send answer</button></div>`;
  $("#ghText").value = draft;
  const msgs = box.querySelector(".gh-msgs");
  msgs.scrollTop = msgs.scrollHeight;
}

async function gameHelpAct(action) {
  if (!GH.cur) return;
  const body = { game: GH.cur.game, id: GH.cur.id, action };
  if (action === "reply") { body.text = $("#ghText").value; body.close = $("#ghClose").checked; }
  try {
    const r = await api("/api/help", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.msg || "Done");
    if (action === "reply") $("#ghText").value = "";
  } catch (e) { return toast(e.message, true); }
  loadGameHelp();
}

document.addEventListener("click", (e) => {
  const li = e.target.closest("#ghList li[data-gh]");
  if (li) { const [g, ...rest] = li.dataset.gh.split("|"); return showGameHelp(g, rest.join("|")); }
  const f = e.target.closest("#ghFilter [data-f]");
  if (f) {
    GH.open = f.dataset.f === "open";
    document.querySelectorAll("#ghFilter [data-f]").forEach((b) => b.classList.toggle("on", b === f));
    return loadGameHelp();
  }
  if (e.target.closest("#ghSend")) return gameHelpAct("reply");
  if (e.target.closest("#ghCloseOnly") && confirm("Close this request without an answer?")) return gameHelpAct("close");
});

// ---- the account window: what each game knows of this player ----
async function loadAccountGames(polid) {
  const box = $("#acctGames");
  if (!box) return;
  let r;
  try { r = await api("/api/account-games?polid=" + encodeURIComponent(polid)); }
  catch (e) { box.innerHTML = `<h4>In the games</h4><div class="none">${esc(e.message)}</div>`; return; }
  if (!$("#acctGames") || (ACCT && ACCT.polid !== polid)) return;     // another account was opened meanwhile
  const games = r.games || [];
  const body = games.map((g) => `<div class="ag-game"><div class="strong">${esc(g.title)}</div>`
    + g.characters.map((c) => `<div class="kv"><span>${c.online ? '<span class="dot good" aria-label="online"></span>' : ""}${esc(c.name)}</span>`
      + `<span>${esc(c.detail)}</span></div>`
      + (c.facts.length ? `<div class="ag-facts">${c.facts.map((f) => `${esc(f.label)}: ${esc(f.value)}`).join("; ")}</div>` : "")).join("")
    + (g.note ? `<div class="muted">${esc(g.note)}</div>` : "") + `</div>`).join("");
  box.innerHTML = `<h4>In the games</h4>`
    + (body || `<div class="none">${(r.asked || []).length ? "None of " + esc(r.asked.join(", ")) + " has seen this player." : "No game here can look up players yet."}</div>`)
    + ((r.problems || []).length ? `<div class="muted">${esc(r.problems.join("; "))}</div>` : "");
}
