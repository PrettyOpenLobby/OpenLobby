// sw.js -- the service worker that makes the admin panel installable.
//
// It caches NOTHING the panel serves behind its sign-in. Every page and API
// answer here is account data, and a copy of it sitting in a phone's cache
// after a sign-out, or shown stale as if it were current, is worse than no
// answer. So: everything goes to the network, as if this file did not exist,
// and the one thing it adds is a plain "can't reach the panel" page when a
// navigation fails, instead of the browser's dinosaur.
//
// Browsers only run a service worker from a secure origin (https, or
// localhost). Over plain http on a LAN address this file is simply never
// registered and the panel works exactly as before.
"use strict";

const OFFLINE = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1c1f28"><title>OpenLobby Admin</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#14161c;color:#e6e8ee;font:15px/1.5 'Segoe UI',system-ui,sans-serif;padding:24px;
box-sizing:border-box;text-align:center}.c{max-width:340px}h1{font-size:18px;margin:0 0 8px}
p{color:#9aa2b4;margin:0 0 18px}button{background:#5aa9ff;color:#04121f;border:0;
border-radius:8px;padding:10px 18px;font:600 14px 'Segoe UI',system-ui,sans-serif;cursor:pointer}
</style></head><body><div class="c"><h1>Can't reach the panel</h1>
<p>This device is offline, or not on the network the panel is served on.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// GM-call alerts (Web Push). The server sends {title, body, tag, url}.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "OpenLobby Admin", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag,
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png",
    data: { url: d.url || "/#gmcalls" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (c.url.startsWith(self.location.origin)) { c.navigate(url); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});

self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;       // not ours: the network as usual
  e.respondWith(fetch(e.request).catch(() =>
    new Response(OFFLINE, { status: 503,
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } })));
});
