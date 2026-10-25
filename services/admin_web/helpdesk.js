// helpdesk.js -- the Inbox's Game help (adminhelp.py) and the account window's "In the games"
// section (adminplayers.py).
"use strict";

// ---- Inbox > Game help ----
// Each game's help desk needs that game's help permission; the owner sees them all.
const canGameHelp = () => isOwner() || (SESSION.perms || []).some((p) => p.startsWith("game.") && p.endsWith(".help"));
// The filter is the Inbox's: Waiting on us, Open, Closed, All.
// (helpdesk.js loads before app.js, so the remembered choice is read on first use)
let GH = { filter: null, rows: [], cur: null };
const GH_EMPTY = { waiting: "Nobody is waiting on us.", open: "Nothing open.", closed: "Nothing closed.", all: "No requests yet." };
const ghOk = (t, f) => f === "all" || (f === "waiting" ? t.waiting && !t.closed : f === "closed" ? !!t.closed : !t.closed);

// Nothing picked: the list takes the whole width, with no empty reading side.
function ghLayout() { $("#tab-gamehelp .gh-split").classList.toggle("solo", !GH.cur); }

async function loadGameHelp() {
  if (GH.filter === null) GH.filter = store.get("ghFilter", "waiting");
  if (!GH_EMPTY[GH.filter]) GH.filter = "waiting";
  document.querySelectorAll("#ghFilter [data-f]").forEach((b) => b.classList.toggle("on", b.dataset.f === GH.filter));
  if (!GH.rows.length) $("#ghList").innerHTML = '<li class="empty">Loading...</li>';
  let r;
  const open = GH.filter === "waiting" || GH.filter === "open";
  try { r = await api("/api/help?open=" + (open ? "1" : "0")); }
  catch (e) { $("#ghList").innerHTML = `<li class="empty">${esc(e.message)}</li>`; return; }
  const all = r.threads || [];
  GH.rows = all.filter((t) => ghOk(t, GH.filter));
  $("#ghProblems").textContent = (r.problems || []).join("; ");
  $("#ghProblems").hidden = !(r.problems || []).length;
  // one message at a time: no help desk at all says only that
  const none = !(r.games || []).length;
  $("#ghNone").hidden = !none;
  $("#tab-gamehelp .gh-split").hidden = none;
  $("#tab-gamehelp .toolbar").hidden = none;
  const waiting = all.filter((t) => t.waiting && !t.closed).length;
  $("#ghCount").textContent = waiting ? `${waiting} waiting for an answer` : "";
  $("#ghList").innerHTML = GH.rows.map((t) => `<li data-gh="${esc(t.game)}|${esc(t.id)}" class="${GH.cur && GH.cur.game === t.game && GH.cur.id === t.id ? "on" : ""}">`
    + `<span class="t1">${t.waiting && !t.closed ? '<span class="dot warn" aria-label="waiting"></span>' : ""}<b>${esc(t.subject)}</b>`
    + `<span class="muted"> ${esc(t.who || "?")}, ${esc(t.game_title)}${t.category ? ", " + esc(t.category) : ""}</span></span>`
    + `<span class="t2">${t.closed ? "Closed" : t.waiting ? "Waiting" : "Answered"}${t.at ? ", " + esc(ago(t.at * 1000)) : ""}</span></li>`).join("")
    || `<li class="empty">${GH_EMPTY[GH.filter]}</li>`;
  ghLayout();
  if (GH.cur) showGameHelp(GH.cur.game, GH.cur.id, true);
}

async function showGameHelp(game, id, quiet) {
  GH.cur = { game, id };
  ghLayout();
  document.querySelectorAll("#ghList li[data-gh]").forEach((li) => li.classList.toggle("on", li.dataset.gh === `${game}|${id}`));
  return renderHelpThread($("#ghThread"), game, id, quiet, loadGameHelp);
}

// One help request with its answer box, drawn into `box` (Game help, and the Inbox's
// reading pane). `after` runs once an answer or a close went through.
const HELP_AFTER = new WeakMap();
async function renderHelpThread(box, game, id, quiet, after, head) {
  box.dataset.game = game; box.dataset.id = id;
  HELP_AFTER.set(box, after);
  if (!quiet) box.innerHTML = '<p class="muted">Loading...</p>';
  let t;
  try { t = await api(`/api/help/thread?game=${encodeURIComponent(game)}&id=${encodeURIComponent(id)}`); }
  catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return null; }
  if (box.dataset.game !== game || box.dataset.id !== id) return null;   // another one was opened meanwhile
  const old = box.querySelector(".gh-reply");
  const draft = old ? old.value : "";
  box.innerHTML = `<h3>${esc(t.subject || "(no subject)")}</h3>`
    + (head ? head(t) : `<p class="muted">${esc(t.who.name || "?")} in ${esc(t.game_title)}`
      + `${t.who.online === true ? ", online now" : t.who.online === false ? ", not online" : ""}${t.closed ? ". Closed." : ""}</p>`)
    + `<div class="gh-msgs">${t.messages.map((m) => `<div class="gh-msg ${m.from}">`
      + `<div class="gh-meta">${m.from === "staff" ? "Staff" : esc(t.who.name || "Player")}${m.at ? ", " + esc(ago(m.at * 1000)) : ""}</div>`
      + `${m.subject && m.subject !== t.subject ? `<div class="strong">${esc(m.subject)}</div>` : ""}<div class="gh-text">${esc(m.text)}</div></div>`).join("")}</div>`
    + `<label class="gh-answer">Answer (goes to the player in ${esc(t.game_title)})<textarea class="gh-reply" rows="4" maxlength="4000"></textarea></label>`
    + `<div class="gh-acts"><label class="chip"><input type="checkbox" class="gh-close"> Close it after</label>`
    + `<span class="grow"></span>${t.closed ? "" : '<button class="ghost" data-gh-act="close">Close without answering</button>'}`
    + `<button class="act" data-gh-act="reply">Send answer</button></div>`;
  box.querySelector(".gh-reply").value = draft;
  const msgs = box.querySelector(".gh-msgs");
  msgs.scrollTop = msgs.scrollHeight;
  return t;
}

async function gameHelpAct(box, action) {
  const body = { game: box.dataset.game, id: box.dataset.id, action };
  if (!body.game) return;
  const text = box.querySelector(".gh-reply");
  if (action === "reply") { body.text = text.value; body.close = box.querySelector(".gh-close").checked; }
  try {
    const r = await post("/api/help", body);
    toast(r.msg || "Done");
    if (action === "reply") text.value = "";
  } catch (e) { return toast(e.message, true); }
  const after = HELP_AFTER.get(box);
  if (after) after();
  loadInboxCounts();
}

document.addEventListener("click", async (e) => {
  const li = e.target.closest("#ghList li[data-gh]");
  if (li) { const [g, ...rest] = li.dataset.gh.split("|"); return showGameHelp(g, rest.join("|")); }
  const f = e.target.closest("#ghFilter [data-f]");
  if (f) {
    GH.filter = f.dataset.f;
    store.set("ghFilter", GH.filter);
    return loadGameHelp();
  }
  const act = e.target.closest("[data-gh-act]");
  if (!act) return;
  const box = act.closest("[data-game]");
  if (!box) return;
  if (act.dataset.ghAct === "reply") return gameHelpAct(box, "reply");
  if (await uiAsk({ title: "Close this request?", text: "It closes without an answer. The player sees it as closed.",
                    ok: "Close it" })) return gameHelpAct(box, "close");
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
