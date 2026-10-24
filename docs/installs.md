# Installs that find each other

A game server installed on its own (with its own setup program) can be run from an
OpenLobby admin panel on the same machine. The setup programs find each other through
Docker labels, talk over one shared Docker network, and pair with a one-time code, so no
token is ever copied by hand and nothing joins a panel without its owner's say.

The game side of the admin protocol itself (what a game serves to the panel) is in
[admin-modules.md](admin-modules.md) and `tools/olport.py`. This page is about installing.

Status: the labels, network and update object are agreed. The panel has its labels, joins
the network, and answers `/api/pair` and `/api/pair/update` (a game's updater included, see
"Updates"); its owner can also add a game by
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
   It may also send an `updater`, for the panel's Update button (see "Updates" below).
3. Answers:
   - `200 {"ok": true, "title": "...", "pairing": "<secret>"}`: keep the secret (for example
     as `OPENLOBBY_PAIRING` in the game's `.env`); the panel keeps only its hash. With an
     `updater` in the request the answer also has `"updater": "requested"` (or `"approved"`
     when the panel had already approved exactly that updater).
   - `400 {"error": "..."}`: a bad request, or the probe failed.
   - `403 {"error": "..."}`: a wrong, expired or used code.
   - `429`: too many tries (5 a minute from one address).
4. When the setup program changes the game's token later, it tells the panel:

       POST <panel.url>/api/pair/update
       {"key": "kart", "pairing": "<secret>", "token": "<new token>", "url": "<optional>",
        "updater": <optional>}

   `200 {"ok": true}` (with `"updater"` as above when one was sent), `403` for a wrong
   pairing, `404` for a key the panel does not have. Unpairing in the panel ends the secret
   and removes the updater.

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
`current` or `unknown` (with a `why` sentence when it cannot tell, for example offline). A game that
answers `unknown` may leave `running` and `latest` out, or send them as `null`; its `why` is shown
as it is.

The panel can also start the update itself, in one of two ways. For each game it uses the
first that applies:

1. the game applies the update itself: its `update` object has `"apply": true`;
2. the deployer runs the game's updater: the setup program sent one when it paired, the
   owner approved it, and a deployer is set up on this server ([deployer.md](deployer.md));
3. neither: the panel shows the `how` sentence where an Update button would be.

OpenLobby's core and the games the deployer installed from its catalog keep their own Update
button, which the deployer runs as before.

### The game applies it (native installs)

A game that can update itself where it runs says so with `"apply": true` in its `update`
object. The Update button then appears while the update is `available`, for the owner and
for a moderator given "Kart Club: update it" (the permission `game.kart.update`, listed with
the game's own permissions). The panel asks "Everyone playing Kart Club is disconnected
while it updates." and then sends, from the panel server:

    POST <admin url>/openlobby/update
    X-Devtool-Token: <the game's token>
    X-Devtool-Actor: <who pressed it>
    (empty body)

The game answers at once and does the work afterwards:

- `202 {"ok": true, "msg": "..."}`: started;
- `409 {"ok": false, "msg": "..."}`: it is already updating;
- `503 {"ok": false, "msg": "..."}`: it cannot update now.

The panel shows `msg` as it is. Any other status reads "The game did not take the update
request (HTTP n)".

While it works, the game's `update` object carries `"state": "updating"` and a `"message"`
saying what it is doing; when it fails, `"state": "failed"` and a `"why"` sentence. The panel
reads the status every 5 seconds and shows the message as it changes. The update has worked
when `running.commit` equals `latest.commit` and `update` is `current`. A status that does
not answer for 10 minutes during an update reads "Kart Club has not come back after
updating; check its server". The activity log gets "updated Kart Club" with who pressed it,
and "Kart Club: update failed: <why>" when the panel sees a failure.

A Python game gets all of this from `tools/olport.py` with `@port.update`
([admin-modules.md](admin-modules.md)).

### The deployer runs its updater (Docker installs)

A setup program that pairs (`/api/pair`) or tells the panel a new token (`/api/pair/update`)
may send:

```json
"updater": {"folder": "C:\\Users\\you\\Kart Club",
            "command": ["python", "setup.py", "--update", "--yes"],
            "timeout_minutes": 60}
```

- `folder`: the install folder as the host sees it, a Windows path such as
  `C:\Users\you\Kart Club` or a Linux path such as `/srv/kart`. Not a drive root, no `.` or
  `..`, no control characters, and not a system folder such as `/usr` or `/etc`.
- `command`: the argument list, never a shell line. The first item is exactly `python` or
  `python3`; at most 16 items, each a string under 200 characters, no NUL.
- `timeout_minutes`: a whole number from 5 to 240, 60 when left out.

Nothing else is allowed in the object. A block that does not fit is refused with `400` and
a sentence saying what is wrong, and nothing is paired or changed. `"updater": null` takes
an updater away; leaving the field out changes nothing.

The panel stores it as `requested`. Settings, Modules, "Games added here" shows the folder,
the command (shell-quoted, as it will run) and the timeout under the game, with Approve for
the owner. Approve asks "This runs python setup.py --update --yes in C:\Users\you\Kart Club
with full Docker access when you press Update. Approve?", and Revoke takes the approval
back. Any change to the folder, the command or the timeout, from a new pairing or from
`/api/pair/update`, makes it `requested` again. Sending the same updater again (the folder
compared as the Docker daemon sees it, so `C:/Games/Kart/` is `C:\Games\Kart`) keeps its
state, answers it (`"updater": "approved"` or `"requested"`) and writes nothing to the
activity log, so a setup program can send it on every start until the answer carries
`updater`. A `/api/pair/update` that changes nothing at all is not logged either. Unpairing or removing the game removes its updater. The activity log gets "Kart
Club asked to update itself with <command>" when a setup program sends a new or changed one,
and "approved the updater of Kart Club" or "revoked the updater of Kart Club" from the owner.

Once it is approved and a deployer is set up, the owner's Update button for the game hands
the deployer the stored record (nothing from the browser), and the deployer runs the command
as a job, with its log in Settings, Install games. The job ends `ok` when the command exits
0 and `failed` otherwise, with the last line it printed as the reason. It is stopped after
`timeout_minutes`, or after 15 minutes without a line of output.

The command runs in a container of the deployer's own image (Python 3.12, git, the docker
CLI and compose), with the install folder mounted where the Docker daemon sees it, at that
same path, as its working directory. On Docker Desktop for Windows a folder
`C:\Users\you\Kart Club` is `/run/desktop/mnt/host/c/Users/you/Kart Club` there; a Linux path
stays as it is. Stdin is closed, and the environment is the image's own plus `HOME=/tmp`,
`LANG=C.UTF-8`, `PYTHONUNBUFFERED=1` and `PYTHONIOENCODING=utf-8`. The Docker socket is mounted
too, so the command can rebuild and restart the game's containers; that is why it runs only
once the owner approved it ([deployer.md](deployer.md), "Updaters of paired games").

## A setup program like the others

Setup programs here are plain terminal programs: Python, standard library only, packaged
with PyInstaller (`--onefile --console`) and built in CI for Windows, Linux and macOS. They
ask numbered questions with defaults in brackets, confirm with Y/n, show one progress line
per step, check that Docker is installed and running before anything else, and keep their
state in one folder (`.env`, data, backups, logs).

## Trying an updater on a throwaway panel

To test a game's update against a real panel and deployer without touching anything else on
the machine, run a second OpenLobby with its own project name, a port nothing else uses, and
only the panel, its database and the deployer:

1. Clone this repository into an empty folder, for example `C:\olupdtest\openlobby`.
2. Next to `docker-compose.yml`, add `updtest.override.yml`:

   ```yaml
   services:
     admin:
       environment:
         POL_ADMIN_CORE: "none"
       ports: !override
         - "127.0.0.1:28090:8090"
       labels:
         org.openlobby.panel.url: "http://127.0.0.1:28090"
   ```

   Pick a port that is free on this machine (`netstat -ano | findstr :28090` shows nothing).
3. Add `.env` there (on Linux use `:` between the compose files and plain paths):

   ```
   COMPOSE_PROJECT_NAME=olupdtest
   COMPOSE_FILE=docker-compose.yml;docker-compose.ghcr.yml;updtest.override.yml
   POL_DEPLOYER_TOKEN=<32 or more random characters>
   POL_DEPLOY_ROOT=/run/desktop/mnt/host/c/olupdtest
   POL_DEPLOY_CORE=/run/desktop/mnt/host/c/olupdtest/openlobby
   POL_DEPLOY_GHCR=1
   ```

4. Make the shared network if it is missing (`docker network create openlobby`), then
   `docker compose --profile deployer up -d postgres valkey admin deployer`. Naming the
   services keeps the resolver and the web server, and so ports 53, 80 and 443, out of it.
5. Set the owner's password:
   `docker compose exec -T -e POL_ADMIN_NEW_PASSWORD=<password> admin python admin.py --set-password admin`,
   then sign in at the port you chose.
6. Pair the game with a code from Settings > Modules, approve its updater there, and press
   Update in Settings > Install games when its report says `available`.

To remove it: `docker compose --profile deployer down -v` in that folder (only that project
goes; leave the `openlobby` network, other installs use it), then delete the folder.
