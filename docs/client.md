# Pointing a client at your server

The PlayOnline clients find their servers by name (`pt008.pol.com` for the
patch check, `ci000.pol.com` for login, and so on). A client plays on your
server once those names resolve to your `POL_ADVERTISE` address. There are
two ways to make that happen: a hosts file on the PC, or using the server's
DNS service as the client's DNS server. The client itself stays unmodified
either way.

## PC Viewer

The simplest route is the hosts installer in `client/`. On the player's PC,
in an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PolConnect.ps1 -Server 203.0.113.10
```

with your server's address in place of `203.0.113.10`. It writes one marked
block into the Windows hosts file, backs the old file up first, and
`-Uninstall` takes the block out again. `-DryRun` shows what it would write.
`client/README.md` covers the options and how to regenerate the host list
from `config/server.yaml` with `tools/gen_hosts.py` when titles add names.

A Viewer on the same Windows machine as Docker Desktop uses `127.0.0.1`, both
for `POL_ADVERTISE` and for `-Server`.

The alternative is to set the PC's DNS server to the machine running the
`dns` service. It redirects the PlayOnline names and forwards everything else
to an ordinary resolver, so the PC keeps working normally. The PC's address
has to be one the resolver answers (`POL_DNS_CLIENTS`, by default loopback and
the private ranges; see `docs/configuration.md`, "DNS").

There is nothing to install for HTTPS; the next section explains why.

## HTTPS

The Viewer opens a few pages over SSL 3.0: the sign-up wizard, account pages
on `userctl.pol.com`, and some portal pages. It checks the server's
certificate against the handful of root certificates built into the client
in the early 2000s, and no current certificate authority issues anything
those roots accept.

The `sslterm` service handles this. It is built from
[sslspoof](https://github.com/patapancakes/sslspoof) (AGPL-3.0) at a pinned
commit, and for each connection it presents a certificate chain that ends in
RSA Data Security's "Secure Server Certification Authority", one of the roots
the client ships with. The chain passes through an old intermediate whose
512-bit key was factored publicly long ago, which is what lets anyone sign
under it. An unmodified client accepts it, so players install nothing. It has
been seen working on the lobby band with a PC Viewer and on the sign-up path
with a PlayStation 2.

Because anyone can mint the same certificates, this HTTPS protects nothing; it
exists only so the client agrees to talk. Treat those pages as unencrypted
and keep passwords that matter elsewhere out of them. Players who reach the
server over a VPN (`docs/configuration.md`) are better off.

The login service hands the lobby band's HTTPS to `sslterm` on 51443, and the
`ucs` service hands it the wizard's on 8443 (`POL_SSL3_HOST`/`POL_SSL3_PORT`
and `POL_UCS_SSL3_HOST`/`POL_UCS_SSL3_PORT` in `docker-compose.yml`). Port 443
stays on `ssl3web`. To use stunnel and the revival CA the stack mints at first
start instead, set `POL_SSL3_HOST=ssl3web` and `POL_UCS_SSL3_HOST=ssl3ucs`; the
client then has to trust that CA, which `tools/certdb.py` and `tools/appca.py`
deal with.

`docker compose logs sslterm` prints one line per handshake. A handshake that
fails says what the client sent last, which tells a refused certificate from
a network problem.

## Accounts

Create an account: either in the Viewer through "Join PlayOnline" (the
`ucs` service runs the sign-up wizard; by default it needs a registration
code made on the admin panel), or directly on the admin panel, which can
create a complete account with its PlayOnline ID, handle, password and titles.
`docs/configuration.md` ("Sign-up and accounts") lists the switches that let
the wizard accept any code.

### HippaulMod

[HippaulMod](https://github.com/PrettyOpenLobby/HippaulMod) is an optional mod
for the PC Viewer. The server works with an unmodified Viewer, and nothing
here requires it.

## PlayStation 2

A console has no hosts file, so DNS is its only route. In the console's
network settings (or the emulator's network settings, or the router's DHCP
settings for the whole LAN), set the primary DNS server to the address of the
machine running the `dns` service. The console must be inside
`POL_DNS_CLIENTS`; a console on your LAN is, with the defaults. The console
also uses port 443 (`docs/ports.md`).

What else you need on the console side:

- a PlayOnline install on a PlayStation 2 hard disk: a retail install on
  real hardware, or an image of one under an emulator that boots from a
  hard-disk image. There is no installer, image, or patch here, and no way
  to make one from files this project provides;
- the console's DNAS check defeated. Before a title starts, the install
  compares the console's own id against the one recorded when it was
  installed; on an emulator, or a transplanted disk, that comparison fails
  and the title will not launch. Working around it is a change to your own
  copy of the install and is not provided here.

The core serves the console the same way it serves the Viewer: the console
asks the DNS for names under `pol.com` (its game hosts, `gi003` and the rest,
are answered too), logs in on the same ports, and fetches its lobby lists and
saves in the console's own layouts. This path has seen far less use than the
PC Viewer; treat it as an expert route.

When the console is on the server's LAN but `POL_ADVERTISE` is a VPN address,
set `POL_ADVERTISE_LAN` to the server's LAN address so the console is sent
somewhere it can reach. A console reaching the server over the internet needs
the public DNS mode (`POL_DNS_PUBLIC`); `docs/configuration.md` has the
recipe and the cautions.

## Checking it

From the client machine, ask the resolver it uses for the login host:

```
nslookup ci000.pol.com
```

With the hosts installer, or with your DNS service set as the PC's resolver,
the answer is your `POL_ADVERTISE` address. To test the DNS service directly
from another machine, name it: `nslookup ci000.pol.com 203.0.113.10`. No reply
at all means the asking machine is outside `POL_DNS_CLIENTS`;
`docker compose logs dns` shows each answer and each refused address.

On the server, `python tools/doctor.py` checks your setup.
