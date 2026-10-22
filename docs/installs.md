# Installs that find each other

A game server installed on its own (with its own setup program) can be run from an
OpenLobby admin panel on the same machine. The setup programs find each other through
Docker labels, talk over one shared Docker network, and pair with a one-time code, so no
token is ever copied by hand and nothing joins a panel without its owner's say.

The game side of the admin protocol itself (what a game serves to the panel) is in
[admin-modules.md](admin-modules.md) and `tools/olport.py`. This page is about installing.

Status: the labels, network and update object are agreed. The panel has its labels, joins
the network, and answers `/api/pair` and `/api/pair/update`; its owner can also add a game by
hand (Settings, Modules, "Add a game"). `POL_ADMIN_GAME_<KEY>_URL` and `_TOKEN` still work and
win over a game stored in the panel ([admin-modules.md](admin-modules.md), "Games added in the
panel").

## Labels

Every install labels its containers in its compose file. A setup program lists them with
`docker ps --filter label=org.openlobby.role`.

The panel:

    org.openlobby.role=panel
    org.openlobby.panel.url=http://127.0.0.1:8090

A game (on the service that runs its admin port):

    org.openlobby.role=game
    org.openlobby.game.key=kart
    org.openlobby.game.title=Kart Club
    org.openlobby.admin.url=http://kart:8820

`game.key` is the module key: 1 to 16 of `a-z` and `0-9`, the same key the game's
`module.json` gives.

## The network

One external Docker network, `openlobby`. Whichever setup program runs first creates it:

    docker network inspect openlobby >/dev/null 2>&1 || docker network create openlobby

No install ever removes it, not on uninstall and not when cleaning up after an error,
because the other installs share it. A setup program whose `docker network create` fails
because the network already exists (another setup made it in between) goes on as normal.

The panel joins it. Each game's admin-port service joins it with an alias equal to its key:

```yaml
services:
  game:
    networks:
      default: {}
      openlobby:
        aliases: [kart]
networks:
  openlobby:
    external: true
```

The admin URL is then `http://<key>:<port>` from the panel, the same on Windows, macOS and
Linux. The admin port is never published to the host or the internet.

## Pairing

1. The panel's owner opens Settings, Modules, "Pair with a setup program". The panel shows
   an 8-character code, good for 10 minutes and one use.
2. The game's setup program asks for that code, then calls

       POST <panel.url>/api/pair
       {"code": "...", "key": "kart", "title": "Kart Club",
        "url": "http://kart:8820", "token": "<the game's admin token>"}

   The panel loads `<url>/openlobby/module.json` with the token (header `X-Devtool-Token`);
   it must answer and name the same key.
3. Answers:
   - `200 {"ok": true, "title": "...", "pairing": "<secret>"}`: keep the secret (for example
     as `OPENLOBBY_PAIRING` in the game's `.env`); the panel keeps only its hash.
   - `400 {"error": "..."}`: a bad request, or the probe failed.
   - `403 {"error": "..."}`: a wrong, expired or used code.
   - `429`: too many tries (5 a minute from one address).
4. When the setup program changes the game's token later, it tells the panel:

       POST <panel.url>/api/pair/update
       {"key": "kart", "pairing": "<secret>", "token": "<new token>", "url": "<optional>"}

   `200 {"ok": true}`, `403` for a wrong pairing, `404` for a key the panel does not have.
   Unpairing in the panel ends the secret.

Both calls are written to the panel's activity log.

A setup program that finds a running panel asks something like "An OpenLobby admin panel
is running here. Manage Kart Club from it?" If the owner says no, the game runs on its own.

## One DNS for every install

A console takes one DNS server, and a resolver needs port 53, so every install on a machine
shares one: OpenLobby's `dns` service, labelled `org.openlobby.role=dns`. A game's setup:

1. Looks for it: `docker ps --filter label=org.openlobby.role=dns`.
2. If there is none (a game installed without OpenLobby), starts it from the published
   image, as its own small service with that label:

   ```yaml
   services:
     dns:
       image: ghcr.io/prettyopenlobby/openlobby:latest
       command: ["dns"]
       restart: unless-stopped
       labels:
         org.openlobby.role: dns
       environment:
         POL_ADVERTISE: "<this machine's address>"
       ports:
         - "0.0.0.0:53:53/udp"
       volumes:
         - dns-data:/data
   volumes:
     dns-data:
   ```

   Whichever setup runs first owns it; the others only add names. Never start a second.
3. Adds its names under its key:

       docker exec <the dns container> python stub.py dns-names add kart lobby.kart.example ranking.kart.example

   A name covers its subdomains. All names are answered with the machine's address
   (`POL_ADVERTISE`); everything else is forwarded upstream. The resolver picks the names up
   within a few seconds, with no restart. Adding again under the same key replaces that
   key's names; `dns-names remove kart` takes them away (an uninstall should); `dns-names
   list` shows them. They are kept in `/data/dns.d/<key>.txt` on the service's data volume.

The resolver answers loopback and the private ranges by default (`POL_DNS_CLIENTS`), and
`POL_DNS_BIND` pins it to one address on a host whose own resolver holds port 53.

## One port 80 for every install

Port 80 is one per machine too, so every install shares OpenLobby's `http` service,
labelled `org.openlobby.role=http`. It sends a request whose Host header names a game's
host to that game's web server, and answers everything else itself. A game's setup:

1. Looks for it: `docker ps --filter label=org.openlobby.role=http`.
2. If there is none, starts it from the published image, as its own small service with
   that label, on the `openlobby` network:

   ```yaml
   services:
     http:
       image: ghcr.io/prettyopenlobby/openlobby:latest
       command: ["http"]
       restart: unless-stopped
       labels:
         org.openlobby.role: http
       ports:
         - "80:80"
       volumes:
         - http-data:/data
       networks:
         - openlobby
   volumes:
     http-data:
   networks:
     openlobby:
       external: true
   ```

   Whichever setup runs first owns it; the others only add hosts. Never start a second.
3. Adds its hosts under its key, with the upstream they go to:

       docker exec <the http container> python stub.py http-hosts add kart http://kart:8080 patch.kart.example www.kart.example

   The upstream is the game's alias on the `openlobby` network. A host matches exactly
   (the port in the Host header is ignored); the request goes on whole and streamed, with
   `X-Forwarded-For`, and a game that is down gets a 502. Changes are picked up within
   5 seconds. Adding again under the same key replaces that key's hosts; `http-hosts remove
   kart` takes them away (an uninstall should); `http-hosts list` shows them. They are kept
   in `/data/http.d/<key>.txt`. Hosts under `pol.com` and `playonline.com` are refused.

This is plain HTTP only. Port 443 on an OpenLobby machine belongs to the SSL 3.0
terminator for the PlayOnline web pages, and the old clients send no SNI, so 443 cannot be
shared by host name. A game that needs HTTPS should ask.

## Updates

A game that updates itself (through its own setup program) can report it in its
`status.json` as a top-level `update` object; the panel shows it with a "What's new" list:

```json
{"update": {
  "running": {"commit": "3f2a9c1d0b7e", "built_at": "2026-10-20T12:00:00Z"},
  "latest": {"commit": "9b8e7d6c5a41", "date": "2026-10-22T08:30:00Z"},
  "behind": 3,
  "changes": [{"commit": "9b8e7d6c5a41", "date": "2026-10-22T08:30:00Z",
               "subject": "Lap times survive a restart"}],
  "update": "available",
  "how": "Run Kart Club Setup and choose Update."
}}
```

`changes` newest first, at most 50, merge commits left out. `update` is `available`,
`current` or `unknown` (with a `why` sentence when it cannot tell, for example offline).
`how` is shown instead of an Update button when the panel cannot update the game itself.

## A setup program like the others

Setup programs here are plain terminal programs: Python, standard library only, packaged
with PyInstaller (`--onefile --console`) and built in CI for Windows, Linux and macOS. They
ask numbered questions with defaults in brackets, confirm with Y/n, show one progress line
per step, check that Docker is installed and running before anything else, and keep their
state in one folder (`.env`, data, backups, logs).
