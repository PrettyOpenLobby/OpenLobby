# Setting up a server

Two scripts do the first-run setup: `setup.sh` on Linux and `setup.ps1` on
Windows with Docker Desktop. They ask a few questions, write `.env`, start
the stack, give the admin panel a password and print what to tell your
players. This page describes what they do, how to run them without
questions, and the same setup by hand. The setup program below runs them for
you.

## The setup program

OpenLobby Setup is one program to download and run, for Windows, Linux and
macOS, from the releases page:
https://github.com/PrettyOpenLobby/OpenLobby/releases (`OpenLobby-Setup.exe`,
`OpenLobby-Setup-linux-x86_64`, `OpenLobby-Setup-mac.zip`). It needs Docker
and nothing else: Docker Desktop on Windows and macOS, Docker Engine on Linux.
It never installs, starts or stops Docker itself; when Docker is missing or not
running it says so and gives the download link.

The first run asks where to install (default `openlobby-server` in your home
folder), who will play, the address players connect to and a user name for the
admin panel. Then it gets OpenLobby (with git when it is installed, otherwise
the GitHub zip of `main`), makes the shared Docker network `openlobby` when it
is missing, and runs the checkout's own `setup.ps1` or `setup.sh` without
questions, with the published images. It shows one line per step, waits for
the admin panel and ends with the panel's address and password (shown once),
the players' instructions (also saved as `What to tell players.txt` in the
install folder) and the ports to open. The script runs on its own with its
output in the install folder's `logs`, so closing the window does not stop it;
the next run shows how it went.

On Windows and Linux the install includes the deployer, so the admin panel
installs, turns on and updates games and updates OpenLobby itself (Settings >
Install games, `docs/deployer.md`). On macOS, where Docker Desktop is not
proven for it, the deployer is left off.

Every later run starts with the server's state and address, then offers
Update, Change settings, Stop (or Start), Show what to tell players, Check that
consoles can reach this server, Start over and Uninstall. Uninstall removes the
containers and keeps the data unless
you ask (and type `delete`); deleting the data also deletes the games the panel
installed beside it, and keeps the client files folder. Start over keeps a copy
of the old `.env` in `backups`, and the installed games. Neither removes the
`openlobby` network, and when another install's
game server is on this machine, this install's `dns` and `http` services stay,
since that game uses them (`docs/installs.md`). Game servers it finds there
are named, with how to pair them with the panel; it does not change them. On
Windows, Uninstall also removes the firewall rules setup made (the rule group
`OpenLobby`, after a UAC prompt), unless another install still uses this one's
DNS and port 80.

The console check runs at the end of every install and update, and from the
menu (or `--check`). It asks the DNS service for `wh000.pol.com` at the
address consoles are told to use, the way a console does, and at `127.0.0.1`,
and checks that the answer is this server's address. It checks that the `dns`
container runs, and on Windows it lists the `OpenLobby` firewall rules, the
network type of the network consoles reach this computer on, any rule that
blocks Docker Desktop, and the other networks this computer is on. Each line
reads `ok`, `note` or `FIX`, and it ends with what to set on the PlayStation 2.
A question this computer asks itself does not pass through Windows Firewall
the way a console's does, so a good answer shows that the server answers; the
firewall and network lines are the part that covers the console.

`--yes`, `--dir`, `--address`, `--admin-user`, `--dry-run` and `--update`,
`--change-settings`, `--stop`, `--start`, `--check`, `--uninstall` (with `--delete-data`)
run it without the menu; `--help` lists them. From a checkout, `python
setup/openlobby_setup.py` is the same program; `python setup/build.py` builds
it with PyInstaller, and `python tools/setup_app_test.py` tests it offline.

## Linux

On a machine with nothing on it yet:

```
curl -fsSL https://raw.githubusercontent.com/PrettyOpenLobby/OpenLobby/main/setup.sh | bash
```

or, from a checkout you already have:

```
bash setup.sh
```

Run it as your normal user. It uses `sudo` only for the install commands it
offers, and only after you agree to them.

What it does, in order:

1. It finds or makes the checkout. Run from a checkout, it uses that one.
   Otherwise it asks where to put it (default
   `~/openlobby-server/openlobby`) and clones
   `https://github.com/PrettyOpenLobby/OpenLobby.git` there. The directory
   above the checkout is where game checkouts go (`docs/games.md`) and
   becomes `POL_DEPLOY_ROOT`.
2. It checks for git, Docker Engine and the Compose v2 plugin. When one is
   missing on Debian, Ubuntu, Raspberry Pi OS or Fedora, it prints Docker's
   own install commands for that system (from
   https://docs.docker.com/engine/install/) and runs them only if you answer
   yes. On other systems it points you to that page and stops.
3. It checks that your user can talk to the Docker daemon. If it cannot
   because you are not in the `docker` group, it explains that membership
   is equivalent to root, offers `sudo usermod -aG docker <you>`, and asks
   you to log in again and rerun. If the daemon is not running it offers
   `sudo systemctl enable --now docker`.
4. It asks who will play and proposes the address settings for that case,
   from the recipes in `docs/configuration.md`. You can accept them or edit
   each one. See "Who plays" below.
5. It asks whether to run the published images (the default, faster) or
   build them here. Published images set
   `COMPOSE_FILE=docker-compose.yml:docker-compose.ghcr.yml` in `.env`, which
   needs Compose 2.24 or newer; with an older Compose it builds instead.
6. It generates `POL_DB_PASSWORD` (32 letters and digits from
   `/dev/urandom`) unless the database volume already exists, since the
   database keeps the password it was created with.
7. It sets up the deployer unless you say no (`docs/deployer.md`): a
   generated `POL_DEPLOYER_TOKEN`, `POL_DEPLOY_ROOT` set to the directory
   above the checkout, `POL_DEPLOY_CORE` when the checkout is not named
   `openlobby`, and `POL_DEPLOY_GHCR=1` with the published images. It also
   asks for an optional folder of your own client installs,
   `POL_DEPLOY_CLIENTS` (default `clients` beside the checkout), and creates
   it, since the deployer only mounts a folder that exists. When a game needs
   data from your client, the admin panel can import it from an install in
   that folder; the folder is mounted read-only. Without it, the panel takes
   uploaded archives instead (`docs/deployer.md`). When Docker is Docker
   Desktop (macOS, or a Linux or WSL machine using it), where this is not
   proven, the deployer is left off unless you give `--deployer`; on
   Windows, `setup.ps1` sets it up for Docker Desktop.
8. It writes `.env`: a copy of `.env.example` with those keys filled in, so
   every comment stays, readable by your user only (`chmod 600`).
9. It starts the stack with `docker compose pull` and `docker compose up -d`
   (or `docker compose up -d --build`), then the deployer with
   `docker compose --profile deployer up -d deployer`, and waits up to three
   minutes for every service to report healthy. When games are enabled in
   `.games`, it starts them through `python3 tools/stack.py compose` instead,
   so their title plugins stay in `login`, and before that runs
   `python3 tools/stack.py volumes`, which creates and fills the data
   volumes those games need (FFXI's maps, a download of about 570 MB the
   first time; `docs/games.md`, "Volumes"). When that fails the script says
   why and starts the rest anyway.
10. It sets the admin panel's password. It asks
    `docker compose exec -T admin python admin.py --show-admin` whether one
    is stored; if not, it generates 20 letters and digits and passes them on
    standard input to `admin.py --set-password`, so the password never
    appears on a command line. The password is kept in the database, not in
    `.env`.
11. It runs `python3 tools/doctor.py` (skipped with a note when there is no
    `python3`).
12. It prints a summary: the admin panel address, or the `ssh -N -L` line to
    run on your own computer when you are logged in over SSH; the admin user
    and the new password, which is shown this once; the hosts installer
    command for players' PCs; the DNS server for a PlayStation 2; where to
    add games (on the admin panel, Settings > Install games); and the ports
    to open for your case (`docs/ports.md`).

The script never stops or removes containers and never deletes a volume
(nor writes into one that already holds files).

### Who plays

| Answer | What it proposes |
| --- | --- |
| This network only | `POL_ADVERTISE` = the address of the interface that has the default route. If something already holds port 53 on the host (often `systemd-resolved`), `POL_DNS_BIND` = that same address. |
| Friends over a VPN | `POL_ADVERTISE` and `POL_DNS_BIND` = the address on the first `wg*`, `wt*`, `tailscale*` or `zt*` interface, and `POL_DNS_CLIENTS` = loopback plus that VPN's range (for a single-address Tailscale interface, Tailscale's whole range). If a PlayStation 2 on the local network will play too, it adds the LAN's range to `POL_DNS_CLIENTS`, leaves `POL_DNS_BIND` empty and sets `POL_ADVERTISE_LAN` to the LAN address. |
| Anyone over the internet | `POL_ADVERTISE` = the public address. Without PlayStation 2 players, `POL_DNS_BIND` keeps the resolver on the LAN address (or 127.0.0.1). With them, `POL_ADVERTISE_PUBLIC` = the public address and `POL_DNS_PUBLIC=1`. |

For the internet case the public address comes from `--public-address`, or
from an interface that has one. When the machine is behind a router, the
script can ask `https://api.ipify.org` for it, but only after saying so and
getting a yes (or with `--lookup-public`). Otherwise it asks you to type it.

### Running it again

With a `.env` already there, the script keeps every value in it. It shows
the current `POL_ADVERTISE` and offers to redo the address questions, and
nothing else. Secrets are never regenerated, a stored admin password is
left alone, and a changed `.env` is first copied to `.env.bak`. The
deployer is only started again if `.env` has its token; `--deployer` adds it
to an existing `.env`.

### Without questions

`--yes` takes the default answer everywhere and never prompts. Combine it
with the answers you want:

```
bash setup.sh --yes --reach vpn --no-ps2 --images published
curl -fsSL https://raw.githubusercontent.com/PrettyOpenLobby/OpenLobby/main/setup.sh | bash -s -- --yes --reach lan
```

| Option | Environment | Meaning |
| --- | --- | --- |
| `--yes` | `OPENLOBBY_YES=1` | Take every default, ask nothing. |
| `--dry-run` | | Print every command and the `.env` it would write (secrets hidden), change nothing. |
| `--dir DIR` | `OPENLOBBY_DIR` | The checkout to use or create. |
| `--repo URL` | `OPENLOBBY_REPO` | Clone from another URL. |
| `--reach lan\|vpn\|internet` | `OPENLOBBY_REACH` | Who plays. Default: `vpn` when a VPN interface is found, else `lan`. |
| `--address ADDR` | `OPENLOBBY_ADDRESS` | The address to advertise instead of the detected one. |
| `--public-address ADDR` | `OPENLOBBY_PUBLIC_ADDRESS` | The public address, for `internet`. |
| `--lookup-public` | `OPENLOBBY_LOOKUP_PUBLIC=1` | Allow the public address lookup. |
| `--ps2`, `--no-ps2` | `OPENLOBBY_PS2=yes\|no` | Whether PlayStation 2 consoles will play. Default: no. |
| `--images published\|build` | `OPENLOBBY_IMAGES` | Published images (default) or a local build. |
| `--deployer`, `--no-deployer` | `OPENLOBBY_DEPLOYER=yes\|no` | Run the deployer. Default: yes on a new `.env`. |
| `--clients DIR\|none` | `OPENLOBBY_CLIENTS` | The client installs folder, or none. |
| `--change-address` | | Redo the address questions on an existing `.env`. `--reach` and `--address` imply it. |
| `--admin-user NAME` | `OPENLOBBY_ADMIN_USER` | The admin panel's user name on a new `.env`. Default: `admin`. |
| `--install-docker` | `OPENLOBBY_INSTALL_DOCKER=1` | With `--yes`, allow the Docker and git install commands. Without it, `--yes` prints them and stops. |
| `--no-start` | | Write `.env` and stop. |
| `--channel stable\|dev` | `OPENLOBBY_CHANNEL` | The update channel. `dev` follows the developers' preview builds; it asks for the access code without showing it and needs the deployer. |
| `--registry HOST/PATH` | `OPENLOBBY_DEV_REGISTRY` | Advanced: the registry a plain token is for, kept in `.env` as `POL_DEV_REGISTRY`. An access code (`olx1.`...) carries its own, so this is not needed with one. |
| `--code-file FILE` | `OPENLOBBY_DEV_CODE_FILE` | With `--yes`, read the access code from the file's first line; it is never taken on the command line. |

The dev channel (docs/deployer.md, "Dev channel") is switched by the
deployer: the script hands it the code on stdin once the stack is up, and
`--channel stable` switches back. Only the code is asked for. setup.ps1
takes the same as `-Channel`, `-Registry` and `-CodeFile`, and the setup
program as `--channel`, `--registry` and `--code-file`. Switching from the admin panel, Settings >
Install games > Update channel, does the same.

`python tools/setup_test.py` tests both scripts offline, with stand-ins for
`docker`, `git` and the address tools.

## Windows

From a checkout, in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It follows the same steps with these differences:

- Missing git or Docker Desktop are offered through `winget` (after you
  agree). After installing Docker Desktop, start it once and run the script
  again.
- Addresses come from `Get-NetIPAddress`. Docker Desktop usually shows the
  DNS service every query as coming from inside Docker, so `POL_DNS_CLIENTS`
  and the public DNS mode cannot tell clients apart there
  (`docs/configuration.md`, "DNS"); keep port 53 behind Windows Firewall.
  When something already holds port 53 on every address (Internet Connection
  Sharing and the Mobile hotspot do), the DNS service is pinned to the LAN
  address with `POL_DNS_BIND`, which works beside it. When a start fails on a
  taken port, the script names the program that holds it.
- After starting the stack it sets up Windows Firewall, in a step of its own
  ("Windows Firewall"). It adds inbound allow rules for the ports players
  reach (`docs/ports.md`): 53/udp and the TCP list, with 443 where a
  PlayStation 2 may play (always on a local network). They apply to Private
  networks and to senders on the local network; for players on a VPN also to
  the VPN's range on any network type, and for the internet case to everyone.
  All of them are in one rule group, `OpenLobby`, which a run replaces as a
  whole, so `Get-NetFirewallRule -Group OpenLobby` lists them and
  `Remove-NetFirewallRule -Group OpenLobby` removes them. When the network
  consoles reach this PC on is set to Public, it says so and offers to switch
  it to Private (the default for players on this network only). When Windows
  has block rules for Docker Desktop's port forwarder, made when its "Allow
  access" question was cancelled, it turns them off: a block rule wins over
  every allow rule. All of this needs administrator rights, so it runs
  `tools\firewall.ps1` elevated, one UAC prompt, and only when something is
  missing; a run that finds the rules in place asks nothing. `-NoFirewall`
  leaves the firewall alone.
- The published images are selected with
  `COMPOSE_FILE=docker-compose.yml;docker-compose.ghcr.yml`, because Compose
  separates that list with `;` on Windows.
- The deployer is set up as in step 7, with its folders written as Docker
  Desktop's VM sees them: the folder above the checkout,
  `C:\Users\you\openlobby-server`, becomes
  `POL_DEPLOY_ROOT=/run/desktop/mnt/host/c/Users/you/openlobby-server`
  (`docs/deployer.md`, "Docker Desktop"). A checkout on a network share or
  in WSL has no such path, and the deployer is then left off. After
  starting it, the script asks the deployer whether Docker sees the
  checkout where it does and prints the answer.
- When games are enabled in `.games`, it starts them through
  `python tools\stack.py compose`, which needs Python 3; without Python it
  leaves the stack as it is and says so.
- It offers to run `client\Install-PolConnect.ps1 -Server <address>` for
  this same PC, so a Viewer here can play. That edits the system hosts file,
  so Windows shows a UAC prompt for it.

Its options are `-Yes`, `-DryRun`, `-Dir`, `-Repo`, `-Reach`, `-Address`,
`-PublicAddress`, `-LookupPublic`, `-Ps2`, `-NoPs2`, `-Images`, `-AdminUser`,
`-ChangeAddress`, `-Deployer`, `-NoDeployer`, `-Clients`, `-InstallTools`,
`-InstallClient`, `-NoClient`, `-NoFirewall` and `-NoStart`, with the meanings
above.

### When a PlayStation 2 cannot connect

A console that stops at "checking PlayOnline version" and then reports that
the DNS server did not respond is not getting answers from this PC. In OpenLobby
Setup choose "Check that consoles can reach this server" (or run
`python tools\doctor.py` in the checkout) and fix what it marks. The usual
causes, in order:

1. Windows Firewall stops the console. Choose Update in OpenLobby Setup (or run
   `setup.ps1` again) and accept the UAC prompt; it adds the rules.
2. The network is set to Public. Settings > Network and internet > (the
   network) > Network profile type > Private network.
3. The DNS service is not running because something else holds port 53, often
   the Mobile hotspot. Turn the hotspot off, or choose Change settings and keep
   the same answers, which pins the DNS service beside it.
4. The console has the wrong DNS address. It must be the address setup prints
   for the PlayStation 2, on the same network as the console. A PC with Wi-Fi
   and a cable, a VPN or WSL has several addresses; the check lists them.

## By hand

The same setup without the scripts:

1. Get the code and make a settings file:

   ```
   git clone https://github.com/PrettyOpenLobby/OpenLobby.git openlobby
   cd openlobby
   cp .env.example .env
   ```

2. In `.env`, set `POL_ADVERTISE` to the address clients will reach this
   machine on: its LAN address, its VPN address, or its public address. Left
   empty it is `127.0.0.1`, which only works for a Viewer on the same machine.
   `docs/configuration.md` has the DNS settings for players on a VPN or over
   the internet. Set `POL_DB_PASSWORD` too, before the first start.

3. Start it. The admin panel joins the Docker network `openlobby`, which every
   OpenLobby install on the machine shares ([installs.md](installs.md)); the
   first line creates it if it is not there yet (the setup scripts do this too):

   ```
   docker network inspect openlobby >/dev/null 2>&1 || docker network create openlobby
   docker compose up -d --build
   ```

   Every service reports healthy within a minute. To skip the build and pull
   the published images instead, see "Running the published images" in the
   README.

4. Give the admin panel a password, then open `http://127.0.0.1:8090` on the
   server (or over an SSH tunnel, `docs/ports.md`):

   ```
   docker compose exec admin python admin.py --set-password
   ```

5. Point a client at the server: the hosts installer in `client/` for a PC
   Viewer, or the DNS setting for a PlayStation 2. `docs/client.md` walks
   through both.
   There are no certificates to install (`docs/client.md`, "HTTPS").

6. Create an account on the admin panel, or a registration code for the
   in-client sign-up, and log in.

7. To manage games from the admin panel, set up the deployer
   (`docs/deployer.md`).

8. If something does not work, run `python tools/doctor.py` on the server.
