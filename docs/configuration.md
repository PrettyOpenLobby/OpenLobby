# Configuration

The settings a server owner needs, grouped by what they control. Each is set
in `.env` next to `docker-compose.yml` (copy `.env.example` to start) and
takes effect on the next `docker compose up -d`. An empty value always means
the built-in default.

`config/server.yaml` holds the rest of the server's shape: the host names the
DNS service redirects, the upstream resolver, and the port layout. It is read
by every service and usually needs no edits.

The code reads many more `POL_*` variables than this page lists. Those are
switches for protocol experiments during development (alternative record
layouts, capture modes, timing probes), and their defaults are the settings
the server is meant to run with. Leave them alone unless you are debugging the
protocol itself. A setting that is not listed in `docker-compose.yml` does not
reach the containers from `.env`; to set one anyway, add it under the
service's `environment:` in a `docker-compose.override.yml`, which Compose
reads automatically.

## Address

`POL_ADVERTISE` is the address setting. Every reply that tells a client where
to connect next (the DNS answers, the login directory's redirect, the lobby
handoff, the patch server's download host) carries this address, so it must be
one the client can route to: the server's LAN address, its VPN address, or its
public address. It takes precedence over `stub_ip` in `config/server.yaml`,
which is only a fallback for running the services outside Docker. Default:
`127.0.0.1`, which only works for a Viewer on the same machine as Docker.

Most servers need only that. When players arrive by more than one route, the
address can be chosen per client:

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_ADVERTISE_PUBLIC` | empty (off) | Handed to every client whose address is a public internet address, in DNS answers and in every later redirect. Use it when the server sits behind NAT or an edge forwarder and internet players must dial a different address from everyone else. Needed for `POL_DNS_PUBLIC`. |
| `POL_ADVERTISE_LAN` | empty (off) | Handed to clients inside `POL_ADVERTISE_LAN_CLIENTS`. Use it when `POL_ADVERTISE` is a VPN address and consoles on the local network, which cannot reach the VPN, should be sent to the LAN address instead. |
| `POL_ADVERTISE_LAN_CLIENTS` | the private ranges | Comma-separated CIDR list of the clients that get `POL_ADVERTISE_LAN`. |

Without `POL_ADVERTISE_LAN`, the DNS service hands every allowed client
`POL_ADVERTISE`. The redirects after DNS already adapt: when `POL_ADVERTISE`
is a VPN or public address, a client on a private address that reached the
server on one of its private addresses is sent back to that address.

## DNS

The `dns` service answers the PlayOnline host names (the `dns.redirect_only`
list in `config/server.yaml`) with your address and forwards every other name
to `dns.upstream` (default `1.1.1.1`), so a client can use it as its only DNS
server. A resolver that forwards for anyone who asks is an open resolver,
which gets abused for reflection attacks within days of appearing on the
internet. So it only answers clients it has been told about.

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_DNS_BIND` | all addresses | The host address port 53 is published on. Set it to the server's LAN or VPN address when another resolver already holds port 53 (`systemd-resolved`, WSL), or to keep the resolver off a public interface. |
| `POL_DNS_CLIENTS` | loopback and the private ranges | Comma-separated CIDR list of who may query. Queries from anyone else get no reply at all, and the first one from each address is logged. The list replaces the default, so include your LAN's range if you still want it. The same list can be set as `dns.clients` in `config/server.yaml`; the environment wins. |
| `POL_DNS_PUBLIC` | `0` | `1` lets internet clients outside `POL_DNS_CLIENTS` query too, on narrow terms: they are answered only for the redirected PlayOnline names, with `POL_ADVERTISE_PUBLIC`; every other name gets REFUSED, a reply no longer than the question. Nothing is forwarded for them except names listed under `dns.public_forward` in `config/server.yaml` (none by default). Has no effect unless `POL_ADVERTISE_PUBLIC` is set. |
| `POL_DNS_PUBLIC_RATE` | `60` | Queries each internet address may make per 10 seconds under `POL_DNS_PUBLIC`. Extra queries are dropped silently. |

These rules depend on the container seeing each client's real address. Docker
Engine on Linux keeps it for traffic from other machines. Docker Desktop on
Windows and macOS usually shows every query as coming from a private address
inside Docker, so there the default list lets everything through and
`POL_DNS_PUBLIC` sees no internet clients; keep port 53 behind a firewall or
`POL_DNS_BIND` on such a host.

### Players on a VPN

Everyone joins one VPN (WireGuard, Tailscale, ZeroTier and the like) and the
server is reached at its VPN address. In this example the VPN is
`198.51.100.0/24` and the server is `198.51.100.5` on it:

```
POL_ADVERTISE=198.51.100.5
POL_DNS_BIND=198.51.100.5
POL_DNS_CLIENTS=127.0.0.0/8,198.51.100.0/24
```

PCs on the VPN can use the hosts installer and skip DNS altogether. A
PlayStation 2 cannot join a VPN itself; if it sits on the server's LAN, add
the LAN's range to `POL_DNS_CLIENTS`, publish DNS on all addresses again
(leave `POL_DNS_BIND` empty), and set `POL_ADVERTISE_LAN` to the server's LAN
address so the console is sent somewhere it can reach.

### Players over the internet

The server has a public address, here `203.0.113.10`, and the ports in
`docs/ports.md` are forwarded to it.

```
POL_ADVERTISE=203.0.113.10
```

PC players use the hosts installer (`client/`), which needs no DNS from you,
so leave port 53 closed to the internet. Only open it when PlayStation 2
players must use your resolver, and then turn on the restricted public mode
instead of widening `POL_DNS_CLIENTS`:

```
POL_ADVERTISE=203.0.113.10
POL_ADVERTISE_PUBLIC=203.0.113.10
POL_DNS_PUBLIC=1
```

A console using it resolves only the PlayOnline names; set its secondary DNS
to an ordinary public resolver if it needs anything else. Never set
`POL_DNS_CLIENTS=0.0.0.0/0` on a server with a public address.

## Content

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_WWW` | `./www` | Host directory holding your portal page tree, mounted read-only. |
| `POL_MIRRORS` | `./mirrors` | Host directory holding your patch trees, mounted read-only (`docs/patching.md`). |
| `POL_PML_FALLBACK` | `1` | Serve the built-in portal pages for any page missing from `www/`. `0` answers a missing page with an empty document. |
| `POL_GM_CALL` | `0` | `1` adds a GM Call entry to the built-in menu. Pair it with `docker compose --profile gm up -d`, and someone to answer the calls. |

Which titles the server offers is covered in `docs/games.md`.

## Patch service

`docs/patching.md` explains these in full.

| Setting | Default | Effect |
| --- | --- | --- |
| `POLP_CURRENT_ALL` | `1` | Every tree not under `mirrors/` is answered "up to date", so the Viewer and every title launch with no patch files. |
| `POLP_CURRENT` | empty | With `POLP_CURRENT_ALL=0`, the `REGION/PROD` pairs (for example `W2U/1000,W2U/0001`) that are still answered "up to date". |
| `POLP_SLIM_LIST` | empty | Leave files unchanged since the disc out of a PlayStation 2 title's file list, for example `P2U/0004`. |
| `POLP_FALLBACK_VERSION` | empty | The version told to a Viewer whose version check arrives empty, as it can on a re-login, for example `20130104_0`. Empty echoes the empty version. |
| `POLP_CONSOLE_LIST_CAP` | `20130601_E` | Leave file-list rows newer than this out of the console Viewer trees (PS2, P2U, X2U, XB2). |

## Sign-up and accounts

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_SIGNUP_ANY_CODE` | empty (off) | `1` makes the in-client "Join PlayOnline" wizard accept any registration code and grant the new account every title the server offers. For a server among friends. |
| `POL_SIGNUP_MODE` | `code` | `permissive` lets the registration code step be left empty; such an account gets `POL_SIGNUP_CONTENTS`. A code that is typed is still checked. |
| `POL_SIGNUP_CONTENTS` | every title offered | Comma list of content codes granted to a permissive sign-up with no code. |
| `POL_SIGNUP_PER_DAY` | `50` | Accounts the sign-up service creates in any 24 hours, counted since it started. `0` turns the cap off. |
| `POL_SIGNUP_PER_IP` | `3` | Accounts one public address may create in 24 hours. Loopback and private addresses are not counted, because behind the SSL terminator every caller looks local. `0` turns it off. |
| `POL_REGCODE_LOCKOUT_FAILS` | `5` | Refused registration codes from one address before it is told to come back later. `0` turns it off. |
| `POL_REGCODE_LOCKOUT_WINDOW_S` | `900` | How long, in seconds, a refused code counts against an address. |

Accounts can also be made on the admin panel, which creates whole accounts or
registration codes carrying their own title grants. With the defaults, only
codes minted there work.

## Login

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_LOGIN_LOCKOUT_FAILS` | `5` | Failed Viewer logins on one account before it is locked. `0` turns lockout off. End one early with `docker compose exec login python accounts.py unlock <POL ID or login>`. |
| `POL_LOGIN_LOCKOUT_WINDOW_S` | `900` | How long, in seconds, a failed login counts, and so how long a lockout lasts. |
| `POL_TOKEN_ARM` | `1` | A Viewer's login token can only bind to an account while that account is armed. A new account is armed when it is created; arm an older one with `docker compose exec login python accounts.py arm <POL ID or handle>` (24 hours). `0` turns the check off. |
| `POL_ACCOUNTS_DB_FAIL` | empty | What a login does when the account database cannot be read. Empty continues without an account (no games menu); `refuse` answers "server busy". |

## Connection limits

Both apply to each service process separately.

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_MAX_CONNS` | `256` | Open connections one process will handle at once. A backstop against leaks; `0` removes it. |
| `POL_MAX_CONNS_PER_IP` | `32` | Open connections one client address may hold in one process, so a single peer cannot take every slot. `0` removes it. |

## Mail to and from the internet

PlayOnline mail between members works with no settings. Outside mail is off
unless both the relay and the domain are set, and then only for members
enabled on the admin panel.

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_EXT_MAIL_RELAY` | empty | `host:port` of an SMTP relay that signs and delivers mail for your outside domain. |
| `POL_EXT_MAIL_DOMAIN` | empty | The outside domain members' addresses use. Inbound mail arrives when the domain's MX points at this server's port 25. |
| `POL_EXT_MAIL_DAILY_OUT` | `20` | Messages one member may send out per day. |
| `POL_EXT_MAIL_DAILY_IN` | `100` | Messages one member may receive from outside per day. |
| `POL_EXT_MAIL_IN_PER_IP_HOUR` | `60` | Outside messages accepted per sending address per hour. |

## Admin panel

The panel listens on `127.0.0.1:8090` on the server (`docs/ports.md` shows how
to reach it over SSH). On a first start it has no credential, and because the
container binds it on all of its own addresses it answers every request with
503 until one exists. Set one from the server:

```
docker compose exec admin python admin.py --set-password
```

That credential is kept in the database and can be changed later on the
panel's Security tab. `--show-admin` prints which user is stored, and
`--clear-password` removes it.

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_ADMIN_USER` | `admin` | User name for the break-glass login below. |
| `POL_ADMIN_PASSWORD` | empty | When set, this user and password always log in, alongside any stored credential. Meant for getting back in; prefer `--set-password` for everyday use. |

## Database

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_DB_PASSWORD` | `openlobby` | Password of the bundled PostgreSQL. Set it before the first start: the database keeps the password it was created with. Letters and digits only, since it goes into a URL. |

`docs/database.md` covers backups, the connection pool and moving an existing
install.

## Storage and logs

State lives in named Docker volumes: `pol-pgdata` (the database: accounts,
friends, mail, saves), `pol-data` (files the services write), `pol-certs`
(the terminators' certificates) and `pol-logs`. Back up `pol-pgdata` and
`pol-data`. `www/` and `mirrors/` are your own files and are only read.

| Setting | Default | Effect |
| --- | --- | --- |
| `POL_LOG_MAX_MB` | `8` | Size at which a log file in `pol-logs` is rotated. `0` never rotates. |
| `POL_LOG_KEEP` | `3` | Rotated copies kept per log. |

`docker compose logs <service>` shows each service's own output.

## Images

| Setting | Default | Effect |
| --- | --- | --- |
| `COMPOSE_FILE` | `docker-compose.yml` | Set to `docker-compose.yml:docker-compose.ghcr.yml` to run the published images instead of building. |
| `OPENLOBBY_TAG` | `latest` | With the published images, which version to run: `latest`, a release such as `0.1.0`, or `sha-<commit>`. |
