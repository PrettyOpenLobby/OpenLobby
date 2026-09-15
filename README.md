# OpenLobby

A server reimplementation of the PlayOnline service backbone: the portal,
login, lobby, mail, accounts, and patch services that Square Enix's PlayOnline
Viewer (2002-2010s) talks to. With this stack running and a client pointed at
it, an unmodified Viewer can sign up, log in, browse the portal, exchange
mail, and manage friends and groups, with no connection to Square Enix.

Game titles (Tetra Master, Janhourou, Fantasy Earth, Front Mission Online,
FFXI bridging) are separate projects that plug into this core; this repository
is the part every title needs.

This project is a clean-room reimplementation based on protocol observation.
It contains no Square Enix code, art, or data. You need your own legally
obtained client and content files; see below.

## Prerequisites

- Docker with Compose v2
- A PlayOnline Viewer install (US 2003-era tested most; JP works)
- A way to point the client's DNS at your server (the stack answers DNS on
  port 53 for the `pol.com` / `playonline.com` names, or use a hosts file)

## Quick start

```
cp .env.example .env        # set POL_ADVERTISE to your server's LAN/VPN IP
docker compose up -d --build
```

All services report healthy within a minute. State (accounts, mail, logs)
lives in named Docker volumes and survives restarts.

### Without building

Every push to `main` publishes the images to the GitHub Container Registry
(`ghcr.io/prettyopenlobby/openlobby` and `openlobby-ssl3`, for amd64 and
arm64), so a server can run without a compiler or a build step:

```
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

Put `COMPOSE_FILE=docker-compose.yml:docker-compose.ghcr.yml` in `.env` and
the plain `docker compose up -d` does the same. `OPENLOBBY_TAG` picks a
version (`latest`, a release such as `0.1.0`, or `sha-<commit>`). Needs
Docker Compose 2.24 or newer. The title repositories have the same override
and are applied after this one.

## Bring your own content

Two directories are read-only inputs that this repository does NOT include,
because their contents are Square Enix's:

- `www/` - the portal page tree the Viewer browses. If you have a portal
  capture of your own, drop it here (`POL_WWW` in `.env` can point
  elsewhere).

- `mirrors/` - patch trees for the Viewer and titles. Only needed if you
  want the client's updater to work against your server.

Portal pages are optional. When a `.pml` page is requested and there is no
file for it under `www/`, the server serves a minimal built-in page instead:
a main menu with the titles the server offers (`POL_LOBBY_CONTENT_IDS`), the
Friend List and Log Out, and for each title a page with Play and a Content
ID sub-page, so a player can log in, pick a title and press Play with no
portal content at all. Real pages override the built-in ones file by file:
a `www/wh000.pol.com/pml/main/index.pml` of your own replaces the built-in
main menu while the title pages stay built in until you add those too.
`POL_PML_FALLBACK=0` turns the built-in pages off (a missing page is then an
empty document, as before). `POL_GM_CALL=1` adds a GM Call entry to the
built-in menu; it is off by default because the GM Call service needs a
person on the other end.

## Pointing a client at it

1. Make the client resolve `*.pol.com` / `*.playonline.com` to your server:
   either set the client machine's DNS to this stack (port 53), or use an
   interposer/hosts approach.
2. The Viewer speaks SSL 3.0 with a 1990s trust store. The stack mints its
   own era-appropriate CA at first start; install `pol-ca.pem` (from the
   `pol-certs` volume) into the client's certificate store with
   `tools/certdb.py`. Without it the client reports POL-1331.
3. Create an account in-client (the sign-up flow is served by the `ucs`
   service), or use the admin panel at `http://127.0.0.1:8090` (local only):
   it can mint registration codes for in-client sign-up, or create complete
   accounts directly (PlayOnline ID, handle, password, per-title grants) so
   players never touch the sign-up flow at all. Set a panel password on the
   Security tab before exposing the port beyond localhost. For a server among
   friends, `POL_SIGNUP_ANY_CODE=1` in `.env` lets the in-client wizard accept
   any code and grants every title the server offers.

## PlayStation 2 clients

Two of the titles (Janhourou, and Tetra Master's console release) are
played from a PlayStation 2 whose hard disk carries a PlayOnline install.
The core serves such a console the same way it serves the Viewer: the
console asks the DNS for names under `pol.com` (its game hosts, `gi003`
and the rest, are answered too), logs in on the same ports, and fetches
its lobby lists and saves in the console's own layouts. What you need on
the console side:

- a PlayOnline install on a PlayStation 2 hard disk: a retail install on
  real hardware, or an image of one under an emulator that boots from a
  hard-disk image. There is no installer, image, or patch here, and no way
  to make one from files this project provides;
- the console's DNAS check defeated. Before a title starts, the install
  compares the console's own id against the one recorded when it was
  installed; on an emulator, or a transplanted disk, that comparison fails
  and the title will not launch. Working around it is a change to your own
  copy of the install and is not provided here;
- the console's DNS pointed at this server (the emulator's or the router's
  setting).

This path was worked out on a private deployment with an emulated console;
it has not yet been exercised against this public stack. Treat it as an
expert route.

## Selftests

```
python tools/run_all.py
```

runs the offline test suite (no Docker needed). Every suite should pass on a
clean checkout.

## Troubleshooting

- POL-1331 on login: the client does not trust the CA yet; see step 2 above.
- Client connects but the portal is blank: `www/` is empty; that is expected
  until you supply portal content.
- Login works from the server machine but not from others:
  `POL_ADVERTISE` is unset (defaults to 127.0.0.1); set it to an address the
  client can reach and restart.

## What is not included, and why

- No Square Enix files: no client binaries, portal pages, art, game data, or
  patch content. The server serves what you supply.
- No account data: the database starts empty.

## License

AGPL-3.0 (see LICENSE). If you run a modified version as a service, the
license obliges you to offer your modifications' source to its users.

## Credits

- The PlayOnline community's preservation work over two decades.
- stunnel (GPL-2.0+) and OpenSSL 1.1.1, built inside the SSL3 terminator
  image to speak the client's SSL 3.0 dialect; their licenses accompany any
  distribution of that image.
