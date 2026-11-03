// comsites.js -- News > Community sites (admincomsites.py): fan sites registered
// for the Community Site Tool banner, and the code each one's owner pastes in.
"use strict";

let CS = { base: "", games: [], skins: 12, custom: [], sites: [] };
let CS_SKIN = null;            // the picked skin; null = the game's own

const CS_OWNER = makePicker({ id: "csOwnerQ", placeholder: "Search accounts", search: findAccounts });
if ($("#csOwner")) $("#csOwner").replaceWith(CS_OWNER.el);
CS_OWNER.el.id = "csOwner";

async function loadComsites() {
  try {
    CS = await api("/api/comsites");
    renderCsGames();
    renderCsSkins();
    renderCsSites();
  } catch (e) { toast(e.message, true); }
}

const csGame = (key) => CS.games.find((g) => g.key === key) || { key, label: key, skin: 0 };
const csSkinOf = (s) => (s.skin === null || s.skin === undefined ? csGame(s.game).skin : s.skin);
const csThumb = (n) => `${CS.base}/static/comtool/skin${n}/bg.png?v=${encodeURIComponent(CS.asset_version || "")}`;

function renderCsGames() {
  const sel = $("#csGame"), cur = sel.value || "playonline";
  sel.innerHTML = CS.games.map((g) => `<option value="${esc(g.key)}">${esc(g.label)}</option>`).join("");
  sel.value = cur;
}

// one button per skin; the game's own skin is marked, and picking it again
// leaves the record on "the game's own"
function renderCsSkins() {
  const own = csGame($("#csGame").value).skin;
  const pick = CS_SKIN === null ? own : CS_SKIN;
  $("#csSkins").innerHTML = Array.from({ length: CS.skins }, (_, n) =>
    `<button type="button" class="cs-skin" role="radio" aria-checked="${n === pick}" data-skin="${n}">` +
    `<img src="${esc(csThumb(n))}" alt="" loading="lazy"><span>Skin ${n}` +
    `${(CS.custom || []).includes(n) ? " · custom" : ""}${n === own ? " · this game's" : ""}</span></button>`
  ).join("");
  csLive();
}

// The banner drawn in a sandbox with no access to the panel, from a record given to it
// rather than asked for, so it shows a site that is not saved yet.
function csPreviewDoc(id, rec, lang) {
  const r = JSON.stringify(rec).replace(/</g, "\\u003c");
  return `<!doctype html><html lang="${lang === "ja" ? "ja" : "en"}"><body style="margin:0;background:#000">` +
    `<script src="${esc(CS.base)}/comtool/comTool"></script>` +
    `<script>comTool(${JSON.stringify(String(id))}, {record: ${r}});</script></body></html>`;
}

// the form's own preview: redrawn a moment after the name, game, skin or page language changes
let CS_LIVE_T = 0;
function csLive() {
  clearTimeout(CS_LIVE_T);
  CS_LIVE_T = setTimeout(() => {
    if (!CS.base) return;
    const game = $("#csGame").value || "playonline";
    const rec = { name: $("#csName").value.trim() || "Site name",
                  game, skin: CS_SKIN === null ? csGame(game).skin : CS_SKIN };
    $("#csLive").srcdoc = csPreviewDoc($("#csId").value || "new", rec, $("#csLiveJa").checked ? "ja" : "en");
  }, 350);
}
$("#csName").addEventListener("input", csLive);
$("#csLiveJa").onchange = csLive;
$("#csSkins").onclick = (e) => {
  const b = e.target.closest("[data-skin]");
  if (!b) return;
  const n = +b.dataset.skin;
  CS_SKIN = n === csGame($("#csGame").value).skin ? null : n;
  renderCsSkins();
};
$("#csGame").onchange = renderCsSkins;

function csShown() {
  const q = $("#csSearch").value.trim().toLowerCase();
  return CS.sites.filter((s) => !q || [s.id, s.name, s.url, s.about, s.owner_polid, csGame(s.game).label]
    .some((v) => String(v ?? "").toLowerCase().includes(q)));
}
$("#csSearch").oninput = () => renderCsSites();

function renderCsSites() {
  const rows = csShown();
  $("#csCount").textContent = `${rows.length} of ${CS.sites.length}`;
  if (!rows.length) {
    $("#csBody").innerHTML = `<tr><td colspan="8" class="muted">${CS.sites.length
      ? "No matches." : "No community sites yet."}</td></tr>`;
    return;
  }
  $("#csBody").innerHTML = rows.map((s) =>
    `<tr>` +
    `<td class="mono">${esc(s.id)}</td>` +
    `<td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>` +
      (s.about ? `<div class="muted">${esc(s.about)}</div>` : "") +
      (s.note ? `<div class="muted" title="Staff note">${esc(s.note)}</div>` : "") + `</td>` +
    `<td>${esc(csGame(s.game).label)}</td>` +
    `<td><img src="${esc(csThumb(csSkinOf(s)))}" alt="Skin ${csSkinOf(s)}" title="Skin ${csSkinOf(s)}" style="width:96px;border-radius:3px;display:block"></td>` +
    `<td class="muted">${[s.listed ? "listed" : "unlisted", s.featured ? "featured" : ""].filter(Boolean).join(", ")}</td>` +
    `<td class="muted">${esc(s.owner_polid || "")}</td>` +
    `<td class="muted" style="white-space:nowrap" title="${esc(utcTitle(s.created_at))}">${esc(fmtWhen(s.created_at))}` +
      (s.made_by ? `<div>${esc(s.made_by)}</div>` : "") + `</td>` +
    `<td style="text-align:right;white-space:nowrap">` +
      `<button class="ghost sm" data-code="${s.id}">Code</button> ` +
      `<button class="ghost sm" data-edit="${s.id}">Edit</button> ` +
      `<button class="danger sm" data-del="${s.id}">Remove</button></td>` +
    `</tr>`).join("");
}

function csFill(s) {
  $("#csId").value = s ? s.id : "";
  $("#csName").value = s ? s.name : "";
  $("#csUrl").value = s ? s.url : "";
  $("#csGame").value = s ? s.game : "playonline";
  $("#csAbout").value = s ? s.about : "";
  $("#csNote").value = s ? s.note : "";
  $("#csListed").checked = s ? s.listed : true;
  $("#csFeatured").checked = s ? s.featured : false;
  CS_SKIN = s && s.skin !== null && s.skin !== undefined ? s.skin : null;
  if (s && s.owner_polid) CS_OWNER.set(s.owner_polid, s.owner_polid); else CS_OWNER.clear();
  $("#csFormTitle").textContent = s ? `Edit ${s.name} (site ${s.id})` : "Register a community site";
  $("#csSave").textContent = s ? "Save changes" : "Save site";
  $("#csCancel").hidden = !s;
  renderCsSkins();
}
$("#csCancel").onclick = () => csFill(null);

function csShowCode(s) {
  $("#csCode").hidden = false;
  $("#csCodeTitle").textContent = `Code for ${s.name} (site ${s.id})`;
  $("#csScript").textContent = s.code.script;
  $("#csTag").textContent = s.code.tag;
  $("#csPreview").srcdoc = csPreviewDoc(s.id, { name: s.name, game: s.game, skin: csSkinOf(s) }, "en");
  $("#csCode").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("#csCode").onclick = (e) => {
  const b = e.target.closest("[data-copyfrom]");
  if (b) copyText($("#" + b.dataset.copyfrom).textContent, "the code");
};

$("#csSave").onclick = async () => {
  const id = $("#csId").value;
  const body = {
    id: id || undefined, name: $("#csName").value, url: $("#csUrl").value.trim(),
    game: $("#csGame").value, skin: CS_SKIN, about: $("#csAbout").value, note: $("#csNote").value,
    listed: $("#csListed").checked, featured: $("#csFeatured").checked,
    owner_polid: can("accounts_view") ? CS_OWNER.value() || "" : undefined,
  };
  if (!can("accounts_view") && id) {
    const was = CS.sites.find((s) => String(s.id) === id);
    body.owner_polid = was ? was.owner_polid : "";       // kept as it was
  }
  $("#csSave").disabled = true;
  try {
    const s = await post("/api/comsites", body);
    toast(id ? `Saved ${s.name}` : `Registered ${s.name} as site ${s.id}`);
    csFill(null);
    await loadComsites();
    csShowCode(s);
  } catch (e) { toast(e.message, true); }
  finally { $("#csSave").disabled = false; }
};

$("#csBody").onclick = async (e) => {
  const find = (id) => CS.sites.find((s) => String(s.id) === id);
  const c = e.target.closest("[data-code]");
  if (c) return csShowCode(find(c.dataset.code));
  const ed = e.target.closest("[data-edit]");
  if (ed) {
    csFill(find(ed.dataset.edit));
    return $("#csForm").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const d = e.target.closest("[data-del]");
  if (!d) return;
  const s = find(d.dataset.del);
  if (!await uiAsk({ title: `Remove ${s.name}?`, ok: "Remove", danger: true,
                     text: "Its banner shows an error from then on, wherever it is still pasted." })) return;
  try {
    await post("/api/comsites/delete", { id: s.id });
    toast(`Removed ${s.name}`);
    if ($("#csId").value === String(s.id)) csFill(null);
    $("#csCode").hidden = true;
    loadComsites();
  } catch (err) { toast(err.message, true); }
};
