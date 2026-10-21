# Ports

Every port `docker-compose.yml` publishes on the host, what answers on it, and
who needs to reach it. `docker-compose.ghcr.yml` only swaps the images and
publishes nothing of its own. Title repositories add their own ports when
they are layered on; their READMEs list them, and `docs/games.md` covers
running them together.

All ports are TCP unless marked UDP.

## Ports players reach

| Port | Service | What it is |
| --- | --- | --- |
| 53/udp | `dns` | The resolver that answers the PlayOnline host names with your address. Needed by any client that uses this box as its DNS server (every PlayStation 2, and PCs without the hosts installer). See "DNS" below before opening it. |
| 80 | `http` | The portal over plain HTTP, served from `www/` or the built-in pages. |
| 443 | `ssl3web` | Portal HTTPS for the PlayStation 2's network check. The PC Viewer does not use it. |
| 25, 110 | `login` | SMTP and POP3 for a Viewer mail account set up as a generic (ISP-type) account. 25 is also where internet mail arrives when outside mail is turned on (`POL_EXT_MAIL_RELAY`, see `docs/configuration.md`). |
| 51200-51240 | `login` | The lobby band. 51240 is the login directory (`ci000.pol.com`), the first thing the Viewer dials after the patch check. |
| 51241-51250 | `authrelay` | The authentication band. After login the Viewer keeps this connection open as its session channel (chat, presence, title traffic). The relay holds the client's socket and passes the traffic to the `authsess` service, so restarting `authsess` does not disconnect players. |
| 51251-51271, 51273-51304, 51306-51310 | `login` | The rest of the lobby band. 51260 and 51261 are POP3 and SMTP for PlayOnline-type mail accounts, the kind the Viewer sets up for itself. 51272 is left out because a title plugin uses it. |
| 51305 | `ucs` | Account sign-up (`ucs.pol.com`). It carries plain portal requests and the sign-up wizard's SSL 3.0 connection on the same port, tells them apart by the first byte, and hands the encrypted ones to `sslterm`. |
| 51330 | `login` | The world handoff endpoint the lobby points a title at. |
| 53001-53015 | `patch` | Per-title version checks and downloads, 53000 plus the content id (`docs/patching.md`). |
| 54000 | `patch` | The Viewer's version check. This is the very first connection a Viewer makes; when it fails the Viewer shows POL-1160. |
| 51112/udp | `gmd` | GM Call. Only published when the `gm` profile is started (`docker compose --profile gm up -d`). |

## Ports that stay on the server

| Port | Service | What it is |
| --- | --- | --- |
| 127.0.0.1:8090 | `admin` | The admin panel. It creates accounts and registration codes. |
| 127.0.0.1:8080 | `ucs-plain` | The sign-up pages without encryption, behind `sslterm`. Published on loopback only so you can look at the pages in a browser on the server. |

Some ports are used between containers and never published: PostgreSQL
(5432), Valkey (6379), the `sslterm` routes for the sign-up wizard (8443) and
the lobby band (51443, which the band hands HTTPS connections to), the same
two on the stunnel fallbacks `ssl3ucs` and `ssl3web`, and the authentication
responder behind the relay (the relay's ports plus 200).

Both loopback ports are bound to `127.0.0.1` in the compose file, so they are
unreachable from other machines whatever your firewall says. To use the admin
panel from your own computer, tunnel it over SSH:

```
ssh -N -L 8090:127.0.0.1:8090 you@203.0.113.10
```

and open `http://127.0.0.1:8090` locally. Changing the published address to
something wider exposes a page that mints accounts; if you do, set an admin
password first (`docs/configuration.md`, "Admin panel").

## Which ports to open

On a LAN, nothing needs opening beyond what the host's own firewall blocks.

When players come from another network, they need 80, 51200-51271,
51273-51310, 51330, 53001-53015 and 54000 (plus 25 and 110 if anyone uses a
generic mail account, and 443 only for PlayStation 2 consoles). 53/udp is
needed only by clients that use your DNS; see the next section.

The SSL 3.0 terminators (`ssl3web` on 443, and `sslterm` behind 51305 and the
lobby band) speak SSL 3.0 with the old cipher suites on purpose, because that
is all the clients can speak, and `sslterm`'s certificates can be minted by
anyone (`docs/client.md`, "HTTPS"). Treat that traffic as unencrypted. Players
on a VPN avoid the question entirely; players over the internet send their
sign-up and portal HTTPS across it with that protection and no more.

### Docker and host firewalls

On Linux, Docker writes its own iptables rules for published ports, and they
are evaluated before the rules `ufw` and `firewalld` manage. A port published
by the compose file is reachable even when `ufw` says it is denied. Three ways
to control it that do work:

- Publish the port on one address. `POL_DNS_BIND` does this for 53/udp: set
  it to the server's LAN or VPN address and the resolver is not on the public
  interface at all. Other ports can be narrowed the same way by editing the
  `ports:` lines (`"198.51.100.5:80:80"`), ideally in a
  `docker-compose.override.yml` so the main file stays as shipped.
- Put rules in the `DOCKER-USER` chain, which Docker leaves to you and
  evaluates first.
- Filter in front of the host: the router, or your hosting provider's
  firewall.

## DNS

The resolver redirects the PlayOnline names and forwards every other name to
an upstream resolver, so a PC or console can use it as its only DNS server.
That also makes it an open resolver for anyone who can reach port 53, so by
default it answers only loopback and the private (RFC 1918) address ranges and
ignores everyone else. `docs/configuration.md` ("DNS") has the settings and a
recipe each for players on a VPN and players over the internet. Never set
`POL_DNS_CLIENTS=0.0.0.0/0` on a box with a public address.

## Port conflicts

- 53/udp: `systemd-resolved` and WSL often hold port 53 on the wildcard
  address. Set `POL_DNS_BIND` to the server's LAN address.
- 80 and 443: any web server already on the host.
- 25: a mail server already on the host (many Linux distributions install
  one).

`docker compose up` reports "address already in use" with the port number
when one of these is taken.
