# The deployer

The deployer is a small service that installs, enables, updates and
restarts games on behalf of the admin panel. It does what you would do with
`tools/stack.py` in a shell (docs/games.md), started from a button instead.
It lives in `services/deployer/` and runs as its own container, `deployer`,
in the compose profile of the same name. A plain `docker compose up` never
starts it.

## Read this first

To run `docker compose` the deployer is given the Docker socket. Anything
that can talk to that socket can start a container that mounts the host's
root filesystem, so the deployer is as powerful as root on the host. The
service is built around that:

- It publishes no port. Only containers on the stack's own network reach
  it, as `http://deployer:8099`; the admin container is one of them.
- Every request needs `Authorization: Bearer <POL_DEPLOYER_TOKEN>`. With an
  empty token the service logs why and exits instead of starting.
- It runs nothing but `tools/stack.py` (and `git` for an update), with
  argument lists it builds itself from a fixed set of actions and the game
  keys in `games/*.json`, the client data tools the catalog names for
  the `import` action, and OpenLobby's own `.polpatch` toolkit for the
  patch actions. Nothing from a request reaches a command line except a
  key that matched the catalog, a region, product or version code of a
  fixed shape, and the path of a folder or file the deployer checked
  itself (see "Importing client data" and "Patch bundles"). The one
  exception is the updater of a game paired with the panel, a python
  command the panel's owner approved, which runs with the socket (see
  "Updaters of paired games").
- The admin container never gets the socket. It holds the token and asks.

Anyone who has the token, or who can run code in a container on that
network, can install, update and restart your games, and through a game
repository you point it at, run code as root. Treat the token like the
host's root password, keep the admin panel on 127.0.0.1 as it ships, and
leave the profile off if you do not want the panel to manage games.

## Requirements

- Docker Engine on Linux, or Docker Desktop on Windows. The deployer runs
  `docker compose` inside its container, and the Docker daemon resolves
  the paths compose hands it, so the deployer mounts your checkouts at the
  path where the daemon sees them. On Docker Engine that is their path on
  the host. Docker Desktop's daemon runs in a Linux VM, which sees the
  Windows folder `C:\Users\you\openlobby-server` as
  `/run/desktop/mnt/host/c/Users/you/openlobby-server`; "Docker Desktop"
  below has the details. Docker Desktop on macOS and Linux is not proven:
  `setup.sh` leaves the deployer off there unless you ask for it.
- The `openlobby` checkout and the game checkouts side by side in one
  directory, as docs/games.md describes, for example `/srv/pol/openlobby`,
  `/srv/pol/hippaulmaster`.
- The stack's settings in `.env` files. The deployer passes the same env
  files as `stack.py` does, but it does not see variables you exported in
  your own shell when you first started the stack.

## Setting it up

1. In OpenLobby's `.env`, set a token and the directory that holds the
   checkouts:

   ```
   POL_DEPLOYER_TOKEN=<output of: python -c "import secrets; print(secrets.token_urlsafe(32))">
   POL_DEPLOY_ROOT=/srv/pol
   ```

   `POL_DEPLOY_ROOT` is the parent of the `openlobby` checkout, as an
   absolute path where the Docker daemon sees it: the host path on Docker
   Engine, the `/run/desktop/mnt/host/...` path on Docker Desktop for
   Windows. If the checkout is not named `openlobby`, also set
   `POL_DEPLOY_CORE` to its full path, written the same way.

   `setup.sh` (Docker Engine) and `setup.ps1` (Docker Desktop on Windows)
   do this step and the next for you (docs/setup.md).

2. Start the deployer, and recreate the admin service so it picks up the
   token:

   ```
   docker compose --profile deployer up -d deployer
   docker compose up -d admin
   ```

   If you run the stack through `stack.py`, the second line is
   `python tools/stack.py compose up -d admin`.

3. `docker compose logs deployer` should end with a line saying where it is
   listening and which directories it uses. If the token or the directory
   is wrong it says so there and stops. The line before it says whether
   the Docker daemon sees the checkout where the deployer does (below).

The admin service finds the deployer through `POL_DEPLOYER_URL` (default
`http://deployer:8099`) and `POL_DEPLOYER_TOKEN`, both from `.env`. Set
`POL_DEPLOY_GHCR=1` to have every action use the published images, the
way `stack.py compose --ghcr` does.

To stop managing games from the panel, `docker compose --profile deployer
stop deployer` (or `rm -sf deployer`). To update the deployer itself after
pulling OpenLobby, run `docker compose --profile deployer up -d --build
deployer` by hand: the deployer never recreates itself, so updating the
core from the panel leaves the running deployer as it was. Running
`setup.sh` or `setup.ps1` again, or Update in OpenLobby Setup, does it for
you.

### Where Docker sees the checkouts

When it starts, the deployer starts a throwaway container from its own
image, with no network, the core checkout mounted read-only the way
`docker compose` will mount it, and compares the `docker-compose.yml` that
container reads (contents, size and time) with the one it reads itself.
When they differ, or Docker cannot mount the folder at all, it logs why,
`/api/health` and `/api/games` carry the sentence, `--healthcheck` exits 1
(so `docker compose ps` shows the deployer unhealthy), the panel shows it
on Settings > Install games, and every `enable`, `disable`, `update` and
`restart` job fails with it before changing anything. `install`, `check`
and `import` start no container and still run. When Docker cannot be asked
(the deployer runs outside a container, or its image cannot be found), the
check is skipped and jobs run as before.

### Docker Desktop

Proven on Docker Desktop for Windows with the WSL 2 backend (Docker Engine
29.3 and Compose 5.1 in Docker Desktop): from the panel, updating the core
from older published images, installing, turning on, updating and turning
off a game, and importing client data from the clients folder.

- Docker Desktop's Linux VM sees each Windows drive under
  `/run/desktop/mnt/host/<drive letter>/`. `setup.ps1` sets
  `POL_DEPLOY_ROOT` (and `POL_DEPLOY_CORE`, `POL_DEPLOY_CLIENTS`) to that
  form of the folders, and compose mounts each at that same path in the
  deployer, so the relative paths in the compose files mean the same
  folders to the daemon. A folder without a drive letter (a network share,
  a WSL path) is not supported, and `setup.ps1` then leaves the deployer
  off.
- The containers `docker compose` on Windows created name their folders as
  `C:\...`, the deployer's as `/run/desktop/mnt/host/c/...`; the first
  deployer job that runs `up` recreates them for that reason, as does the
  next `setup.ps1` run. Nothing else changes.
- Files on a Windows drive read as root's inside a container, unless a
  Linux program created them. The deployer therefore runs `git` and
  `stack.py` as root there, and clones a game as `nobody` (uid 65534), so
  the checkout belongs to nobody and an import tool, which runs as the
  checkout's owner, can write its data into it. A game cloned as root
  (by hand from a container, or by an older deployer) refuses
  imports with a sentence saying so; to fix it, `docker compose --profile
  deployer exec deployer chown -R 65534:65534 /run/desktop/mnt/host/...`
  for that checkout, or delete the folder and install the game again.
- Git for Windows checks files out with CRLF line ends; the repositories'
  `.gitattributes` (`* text=auto`) make the deployer's git see the same
  content, so `update` finds no local changes.
- Docker Desktop on macOS and Linux shares host folders differently and is
  not proven. `setup.sh` leaves the deployer off there unless given
  `--deployer`, which sets `POL_DEPLOY_ROOT` to the host path; the check
  above then says whether the daemon sees it there.

## What each action runs

Every command runs with the OpenLobby checkout as its working directory.
`compose ...` below is `stack.py compose`, with `--ghcr` added when
`POL_DEPLOY_GHCR` is on. When it is on, `--build` is left out.

| Action | Games | What it does |
| --- | --- | --- |
| install | a game | `stack.py add <key> --yes` (git clone beside openlobby), then `stack.py volumes <key>` for a game with volumes to fill, then `stack.py check --json <key>` |
| enable | a game | `stack.py volumes <key>` for a game with volumes to fill; `stack.py check --json <key>`; if the game's required data is there, `stack.py enable <key>`, then `compose up -d --build` |
| disable | a game | `stack.py disable <key>`, then `compose up -d --build --remove-orphans` |
| update | a game or `core` | refuses when the checkout has uncommitted changes to tracked files, otherwise `git -C <checkout> pull --ff-only`, then for an enabled game `stack.py volumes <key>` (when it has volumes to fill), a data check and `compose up -d --build`; for `core`, `stack.py volumes` for the enabled games that have them, since the new catalog may list a new one |
| restart | an enabled game or `core` | `compose --no-prebuild up -d --force-recreate <its services>` |
| check | a game or `core` | `stack.py check --json` for that game, or for every enabled game with `core`; changes nothing |

A few details that follow from those commands:

- `disable` rebuilds as well, since `login` has to be rebuilt without the
  game's title plugin; `stack.py disable` gives the same advice.
- `enable` checks the data first. A game that lacks a required file is not
  enabled, so its containers never start and fail over and over.
- The data volumes a game's catalog entry says how to fill (FFXI's maps;
  docs/games.md, "Volumes") are created and filled by the job itself,
  with `stack.py volumes`, whose lines go into the job's log: the download
  the first time, then a file count per volume. A volume that already
  holds files is left alone, so after the first time this takes a second
  or two. The images come only from the catalog, which the deployer
  already trusts for everything else it runs.
- When that fails (no internet, a full disk) the job ends `failed` with
  the reason as its summary, the game is not turned on, and the job's
  `missing` lists the volumes. The games list then shows each one with
  "Setting it up by itself failed:" and the reason, followed by the
  commands to do it by hand. There is no button: run the action again
  once the cause is fixed. Until then a missing volume is shown with a
  note that turning the game on sets it up.
- With published images, `update` runs `compose pull --ignore-buildable
  --ignore-pull-failures` before `up`, since without `--build` nothing else
  would fetch a newer image.
- Untracked files, such as the data files you add to a game checkout, do
  not count as local changes for `update`.
- `restart` recreates the services listed for the game in its catalog
  entry (`services` in `games/*.json`), and for `core` the core's own list.
- The compose commands never name the `deployer` profile, and
  `--remove-orphans` leaves containers of inactive profiles alone, so the
  deployer is never touched by its own jobs. Recreating `admin` is fine:
  the panel reconnects and goes on polling the job. A `COMPOSE_PROFILES`
  setting that includes `deployer`, in `.env` or the deployer's
  environment, would change that, so a job refuses to run while it does.

`git` and `stack.py add`, `enable` and `disable` run as the user that owns
the checkout directory, so the files they write on the host belong to you
and not to root. `docker compose` runs as root inside the container. On
Docker Desktop the owners differ ("Docker Desktop" above).

## When a game needs your data

Most games need files from your own client before they can run
(docs/games.md, "What each game needs"). After `install`, `enable`,
`update` and `check`, a game whose required data is missing ends the job
in the state `needs_data`. The job's `summary` names the first missing file
and where it goes, for example

```
Fantasy Earth needs the client's data: put fe-fet-area.tsv in ../hippaulring/services/fedata (python tools/fedata_build.py --client <install>), and 2 more
```

and its `missing` list has every required file, each with a hint naming the
tool to make it. Paths are relative to the OpenLobby checkout. Make the
files on the host as the game's README says, then run the action again, or
let the deployer make them with the `import` action below.

## Importing client data

Most of those files are built by a tool in the game's repository from your
own client install. The `import` action runs that tool for you, on a client
you hand the deployer in one of two ways:

- Upload an archive (`.zip`, `.tar` or `.tar.gz`) of the install folder.
  It is unpacked into the `pol-deployer` volume and kept for a day.
- Keep your client installs in a folder on the host, set
  `POL_DEPLOY_CLIENTS` to it in `.env`, and pick one from the panel's
  folder browser. That folder is mounted read-only, at the same path, and
  nothing outside it can be browsed.

In the panel, a missing item that can be imported has an "Import from
client" button; the browser highlights the folders that look like the right
install. The job runs the tool, then `stack.py check --json <game>`, and
ends `ok` when the item is now present, `needs_data` with the `missing` list
when it (or another required item) is still absent, and `failed` when the
tool failed and the item is still missing. A game that already runs keeps
its old data until you run `enable` again, which rebuilds and restarts it.

Which items can be imported, and from what, is in each data item's `import`
block in `games/*.json`:

```
"import": {"label": "game data",
           "argv": ["python", "tools/fedata_build.py", "--client", "{input}"],
           "expects": ["DATA/dat.pak"],
           "hint": "your Fantasy Earth install folder, the one holding DATA/ and FE_Client.dll"}
```

`argv` is fixed in the catalog. The deployer replaces `{input}` with the
absolute path of the folder it checked, swaps `python` for its own Python,
and runs the result with the game's checkout as the working directory. The
API never shows `argv`. `expects` are one to three paths that must exist in
the chosen folder before anything runs; they are what tells an install from
a folder that only looks like one, and what the browser's highlighting
uses. Paths are matched exactly as written, upper and lower case included,
the way the tools open them on Linux.

| Game | Item | Tool | The folder to give it |
| --- | --- | --- | --- |
| Tetra Master | tmdata | `tools/tmdata_build.py --client` | the TetraMaster install (holds `data/CardPrm.BIN`, `data/PlPrm.BIN`) |
| Tetra Master | boardart | `tools/tm_boardart_bake.py --client` | the same PC install (holds `data/gW000.dat`, `data/gW101.dat`) |
| Janhourou | boardfont | `tools/jan_boardfont_build.py --kanji` | the install copied off the PlayStation 2 hard disk (holds `Warashi/font/kanji.bin`) |
| Fantasy Earth | fedata | `tools/fedata_build.py --client` | the install (holds `DATA/dat.pak` and `FE_Client.dll`) |
| Front Mission Online | fmodata | `tools/fmodata_build.py --client` | the install (holds `Data/AI/F32/D15.DAT`) |
| Front Mission Online | boardart | `tools/fmo_boardart_bake.py --client` | the install (holds `Data/AJ/F40/D54.DAT`) |
| Dirge of Cerberus | arena | `tools/doc_extract_arena.py` | the game data copied off the PlayStation 2 hard disk (holds `data/zone/`) |

Items the catalog marks `build_only`, items with no tool yet, Janhourou's
menu icon (made from your portal tree, not from a client) and Final
Fantasy XI (whose world needs no client files) have no `import`.

The tools need Pillow, numpy, fontTools and brotli; the deployer image
carries pinned versions of all four, so nothing has to be installed on the
host. Fantasy Earth's `fedata_build.py` recovers its archive key by
scanning the client DLL, which takes a few minutes. An import tool that
runs longer than six hours is stopped.

### How an import is kept apart from the socket

The deployer runs as root because it holds the Docker socket. An import
tool is the game repository's own code reading files you supplied, so it
never runs with either:

- It runs as the user and group that own the game's checkout, with no
  supplementary groups. If root owns the checkout it runs as `nobody`
  (uid 65534). It never runs as root.
- Before starting it, the deployer checks that this user cannot connect
  to `/var/run/docker.sock` (or the socket `DOCKER_HOST` names): on a
  standard install the socket is `root:docker` with mode 660, so a user
  outside the `docker` group cannot. If the checkout's owner can, for
  example because the checkout belongs to a group that also owns the
  socket, or the socket is world-writable, the job refuses to start and
  says so. Give the checkouts to a user that is not in the `docker` group.
- Its environment is built from nothing: `PATH`, `HOME` and `TMPDIR` (a
  scratch folder for the job), the locale and a few Python settings. No
  `DOCKER_*` or `POL_*` variable and no token reaches it.
- It reads the client folder read-only (a `POL_DEPLOY_CLIENTS` mount) or
  from the unpacked upload, and writes into the game's checkout, which its
  user owns.

- Before starting it, the deployer also checks that this user can write
  to the checkout, and refuses with a sentence when it cannot (a checkout
  root owns, whose import would run as nobody).

On Docker Desktop for Windows the socket inside the container is
`root:root` with mode 660, so `nobody` cannot use it, and the deployer
clones games as `nobody` so their imports can write ("Docker Desktop"
above). Every folder on a Windows drive that a Linux program did not
create, the core checkout among them, reads as writable by anyone in a
container there. That gives an import tool no more than it has on Linux,
where it runs as the user who owns all the checkouts.

### How uploads are unpacked

An upload is the raw request body. The deployer refuses it before reading
when `Content-Length` is missing (411; chunked uploads are not accepted) or
larger than `POL_DEPLOY_UPLOAD_MAX` (413), streams it to a file in the
volume, and unpacks it by its content, never by its name: `X-Filename` is
kept for display only. The archive is refused whole (400) when any member
has an absolute path, a drive letter or a `..` component, is a symbolic
link, a hard link, a device file, a FIFO or a sparse file, is encrypted,
appears twice, or sits under a member that is a file. Before anything is
written, the sizes the archive declares are added up and the members
counted; more than `POL_DEPLOY_UPLOAD_MAX` bytes or 200,000 files is a 413.
While writing, every byte is counted again, so an archive whose headers
understate its sizes is stopped as well. Files are written with mode 644
and folders with 755, whatever the archive says. A refused archive leaves
nothing behind.

Uploads are removed 24 hours after they were made or last imported from,
whichever is later, when the deployer starts and whenever the uploads are
listed or a new one arrives. An upload an import is reading is kept until
the job ends.

## Patch bundles

The patch service (`patch` in the core) serves the bundles in its mirrors
folder, `POL_MIRRORS` (default `./mirrors`). The deployer can put bundles
there for you from `.polpatch` files (docs/polpatch.md), roll a bundle back
to an earlier version, and take one out, then restart `patch`, which reads
its bundles only when it starts. These actions do not depend on the game
catalog.

A `.polpatch` holds Square Enix's client files. It is for your own servers,
and the deployer has no way to hand one back out: there is no download.

The deployer mounts the same host folder as the patch service, read-write,
at `/mirrors` (`POL_DEPLOY_MIRRORS` inside the container). Both mounts are
written `${POL_MIRRORS:-./mirrors}` in `docker-compose.yml`, so they cannot
point at different folders. Like the patch service's own mount, the
deployer's creates the folder when it is missing.

There are three ways to give it a bundle:

- Upload the `.polpatch` (`POST /api/uploads`). An upload whose name ends in
  `.polpatch`, or a ZIP with `polpatch.json` at its top, is kept whole as one
  file, not unpacked.
- Put the `.polpatch` in your `POL_DEPLOY_CLIENTS` folder and pick it in the
  folder browser.
- Build one on the server from a client install, uploaded as an archive or
  in the clients folder (`patch_build`). This runs `polpatch.py build-full`
  and then imports the result.

What each patch action runs:

| Action | What it does |
| --- | --- |
| patch_import | the full `.polpatch` check, `polpatch.py apply` into the mirrors folder, `compose --no-prebuild restart patch`, then `polselfcheck.py` against the restarted service |
| patch_build | `build-full` into a scratch file in the `pol-deployer` volume, then everything `patch_import` does |
| patch_rollback | takes out every version after the one you name (see below), restarts `patch` and runs the self-check |
| patch_remove | moves the bundle's folder out of the mirrors folder into the trash folder (below) and restarts `patch` |
| patch_check | the self-check of every bundle against the running patch service |

`polpublish.py --revert` is never used: it removes every version published
since the first publish at once.

A player's client cannot pick up an update where it left off, so a restart
of `patch` in the middle of a download makes them start over. Before each
restart the deployer asks Valkey how many updates the patch service is
sending (its `live:patch` count, which it reports every ten seconds) and
waits while that is above zero, for up to `POL_DEPLOY_PATCH_DRAIN` seconds
(30 minutes). If downloads are still running then, it restarts anyway and
the job's summary says how many were cut off. When the count is older than
a minute, or Valkey does not answer, it restarts straight away.

### Checks

A `.polpatch` is checked twice. When it is uploaded, the deployer reads its
`polpatch.json` and runs the quick check (`polpatch.py verify --quick`):
every file's size and SHA-256, the archive's names and the bundle's
structure, without unpacking the blobs. That keeps the upload request short
for a large title. The import job then runs the full check, which also
unpacks every blob and compares it with `patch.cfg`, against the bundle it
would be applied onto, so an update whose `base_version` is not the
server's latest version is refused there with a sentence. `apply` hashes
every file once more as it unpacks, stages the result beside the bundles
and moves it into place in one step. A file that fails any of this changes
nothing in the mirrors folder.

After an import or a roll back, and when you ask with
`GET /api/patches?recheck=1`, the deployer runs `polselfcheck.py` against
the patch service the way a client would talk to it: the version check, the
patch list (it must decompress to the bundle's `patch.cfg`), and every
file's blob, checked against the sizes and checksums in that list. The
result is kept with the versions it was run against, so a bundle that has
changed since shows no result. `POLP_CONSOLE_LIST_CAP` and `POLP_SLIM_LIST`
change the list the patch service sends for the bundles they name, on
purpose, and the self-check of those bundles fails while they are set.

### Rolling back

A roll back to version `to` removes exactly the versions after it, oldest
first, the same list `GET /api/patches` shows after `to` in `versions`. It
is refused when `to` is not one of the bundle's versions or is already the
latest. The job log names the versions it removes.

The bundle's list files (`patch.cfg`, `patchlist.raw`, `meta.json`) are
taken from a copy `apply` kept in `mirrors/.polpatch-backup/` (the one made
before the first removed version was added) when that copy holds exactly
the rows that stay. Otherwise they are rebuilt from the current ones with the removed
rows taken out, and `latest_version` set to `to`. The current list files
are copied to `.polpatch-backup/` first. The removed versions' blobs go to
the trash folder.

### The trash folder

Nothing is deleted from the mirrors folder. `patch_remove` and
`patch_rollback` move what they take out to `patch-trash/` in the
`pol-deployer` volume, in a folder named after the time and the bundle, and
the job log gives its full path. Empty it yourself when you no longer need
it, for example with `docker compose --profile deployer exec deployer ls
/deployer/patch-trash` to look and `rm -r` to remove.

### Who runs what

The steps that read a `.polpatch` or a client install (the quick and the
full check, and `build-full`) run as the user that owns the OpenLobby
checkout, with the same rules as an import tool (see "How an import is
kept apart from the socket"): never root, never a user that can use the
Docker socket, and a bare environment.

`polpatch.py apply` and the self-check run as the owner of the mirrors
folder, with the same bare environment, so the files written there belong
to that user. Apply only runs on a file that has just passed the full
check. If root owns the mirrors folder (Docker creates it as root when it
is missing), they run as root. The list files a roll back writes are given
to the bundle folder's owner.

In this process the deployer only reads a `.polpatch`'s ZIP directory and
its `polpatch.json`, with the toolkit's size limits, for the upload answer
and the folder browser.

## Jobs and logs

One action runs at a time. Each is a job with an id, kept with its output
in the `pol-deployer` volume (`/deployer` in the container), so the history
survives a restart. The newest 50 jobs are kept. A job log is capped at
1 MB on disk; past that its beginning is dropped and the tail kept. A job
that was still running when the deployer stopped is marked `failed` when it
starts again.

The token is removed from the environment of every command the deployer
starts, and any occurrence of it in their output is replaced with
`[token]` before it is written, so it never reaches a log.

## Updaters of paired games

A game installed by its own setup program and paired with the panel
(docs/installs.md, "Pairing") is not in the catalog, so the deployer has no
`update` action for it. Its setup program may name an updater instead: an
install folder, a python command and a timeout (docs/installs.md,
"Updates"). The panel keeps it, and the owner approves it in Settings,
Modules. When the owner presses Update, the panel sends the deployer the
record it stored and the owner approved:

    POST /api/updater
    {"game": "kart", "title": "Kart Club",
     "updater": {"folder": "C:\\Users\\you\\Kart Club",
                 "command": ["python", "setup.py", "--update", "--yes"],
                 "timeout_minutes": 60}}

The browser sends only the game's key; the folder, command and timeout come
from the panel's database. The deployer checks every field again with the
same rules (`services/deployer/gameupdater.py`, shared with the panel): no
other fields, the command an argument list starting with `python` or
`python3`, at most 16 items under 200 characters each, no NUL, a full path
that is not a drive root or a system folder, and a timeout of 5 to 240
minutes. Anything else is a 400 and starts nothing. A good request starts a
job (`"action": "updater"`, with `title`), one at a time like every other
job: 202 `{"job"}`, or 409 while another runs.

A game whose updater declares `channels` (docs/installs.md, "Update
channels") may be sent three more fields:

    {"game": "kart", "title": "Kart Club", "updater": {..., "channels": ["stable", "dev"]},
     "channel": "dev", "switch": true, "code": "ABCD-EFGH-JKMN"}

`channel` must be one of the updater's `channels`; the command then runs
with `--channel <channel>` appended. `switch` is true for a channel switch
(the panel's switch button) and false or absent for a regular update of a
game that is not on `stable`. `code` goes only with a switch to a channel
other than `stable`: an `olx1.` access code, or a short dev code that the
deployer redeems first, as for its own channel ("Dev access codes" below).
A short code that cannot be redeemed is a 400 with the issuer's sentence,
or a 502 when the issuer does not answer, and nothing starts. The job's
`channel` is in its public fields.

The job:

1. asks Docker for the deployer's own image (`docker inspect` of its own
   container) and stops any updater container an earlier deployer left
   running;
2. turns the folder into the path the Docker daemon sees: a Windows path
   `C:\Users\you\Kart Club` becomes `/run/desktop/mnt/host/c/Users/you/Kart Club`
   (Docker Desktop), a Linux path stays as it is;
3. checks, as the start-up check does ("Where Docker sees the checkouts"),
   that Docker sees a folder there: a throwaway container of its image, no
   network, the folder mounted read-only. A folder Docker does not see fails
   the job with Docker's own line;
4. runs `docker run --rm` of its own image, named `openlobby-updater-<job>`
   and labelled `org.openlobby.updater.job=<job>` and
   `org.openlobby.updater.game=<key>`, with the folder mounted at that same
   path as the working directory, `/var/run/docker.sock` mounted, the first
   item of the command as the entrypoint and the rest as its arguments. No
   `-i`, so stdin is closed. Its environment is the image's own plus
   `HOME=/tmp`, `LANG=C.UTF-8`, `PYTHONUNBUFFERED=1` and
   `PYTHONIOENCODING=utf-8`: none of the deployer's settings, and no token.
   Its output goes into the job log as it comes. With a `code`, the
   deployer also writes `OPENLOBBY_DEV_CODE=<the olx1. code>` to
   `updater-env/<job>.env` in its volume (mode 600, in a folder of mode 700)
   and adds `--env-file` with that file; the file is deleted as soon as
   `docker run` returns, whatever the outcome. The code is not in the
   arguments, and the job log blanks it (and the token inside it, and the
   short code it came from) if the command ever prints it. A container's
   environment shows in `docker inspect` while it runs, so the code is
   visible there to anyone who can already use the Docker socket, for as
   long as the `--rm` container lives.

Exit 0 ends the job `ok` ("The updater of Kart Club finished.", or for a
switch "The updater of Kart Club switched it to the dev channel."); any other
exit ends it `failed`, with the last line the command printed as the
summary. The deployer kills the container (`docker kill`, by its label)
after `timeout_minutes` ("stopped after 60 minutes") or when it has printed
nothing for 15 minutes ("no output for 15 minutes"), so an updater should
print a line now and then while it waits on something slow.

What approving one means: the deployer's own container mounts only the
checkouts, so the updater runs in a container of its own, and that container
gets the Docker socket, because a game's update builds images and restarts
its containers. A command with the socket can start a container that mounts
the host's root filesystem, so an approved updater has full Docker
access, which is root on the host, exactly like the deployer itself. The
command is fixed when you approve it (any change from the setup program
makes it wait for your approval again), but the files it runs are the
game's: whoever can change the scripts in that folder, or the release the
game's update fetches, can run code as root through it. Approve an updater
only for a game whose source you trust as you trust OpenLobby's, and revoke
it when you stop trusting it.

## HTTP API

JSON in and out, except the raw log. Every endpoint, `/api/health`
included, needs the bearer token and answers 401 without it. Errors carry
an `error` sentence.

`GET /api/health` answers `{"ok": true, "version": "3.0", "busy": false,
"paths": "ok", "problem": null}`; `busy` is true while a job runs. `paths`
is the start-up check's result ("Where Docker sees the checkouts"): `ok`,
`wrong` or `unchecked`; `problem` is its sentence when `wrong`, else null.

`GET /api/games` lists every game in the catalog:

```
{"games": [{"key": "tm", "name": "Tetra Master", "admin_module": "tm",
            "installed": true, "enabled": true, "running": true,
            "data_ok": true, "missing": [],
            "admin_env": ["POL_ADMIN_GAME_TM_URL", "POL_ADMIN_GAME_TM_TOKEN"],
            "ports": [...], "notes": "...", "services": {"tmrank": "running"}},
           ...],
 "core": {"running": true, "services": {...}},
 "options": {"ghcr": false, "channel": "stable", "problem": null}}
```

`options.problem` is the same as in `/api/health`. `options.channel` is
`stable` or `dev` ("Dev channel" below).

- `installed`: the checkout directory exists. `enabled`: the key is in
  `.games`.
- `running`: at least one of the game's services (profiles included) has a
  running container, from `stack.py ps --json`; `null` when that could not
  be asked, and then `running_error` says why. `services` has the state of
  each of its containers.
- `data_ok`: from `stack.py check --json --all`; `null` for a game that is
  not installed. `missing` lists what it lacks as `{"path", "hint",
  "required", "item"}`, optional items included. `item` is the catalog
  data item the path belongs to (several paths can share one). When that
  item can be imported the entry also has `"import": {"hint", "expects"}`.
- `admin_env`: the two settings the panel needs to reach the game's own
  admin port (services/admingames.py), from `admin_module` upper-cased;
  empty when the game has no admin module.
- `ports`: the catalog entry's `ports`, as written there.

The answer is reused for 5 seconds, and refreshed when a job ends.

`POST /api/actions` with `{"action": "<action>", "game": "<key>"}` starts a
job and answers 202 `{"job": "<id>"}`. It answers 400 for an unknown action
or game, or `core` with `install`, `enable` or `disable`, and 409 `{"job":
"<id of the running job>"}` while another job runs. Problems found while
the job runs (a game not installed, a dirty checkout, a failed build) are
not HTTP errors: the job ends `failed` with a sentence in `summary`.

`POST /api/updater` starts the updater of a paired game (body and checks in
"Updaters of paired games" above): 202 `{"job"}`, 400 for a body that does
not fit or a short code the issuer refused, 502 when the issuer did not
answer, 409 while another job runs.

`GET /api/jobs?limit=20` lists jobs: queued and running ones first, then
the rest newest first, at most 50. Each job is

```
{"id": "20261021T120000-1a2b3c", "action": "enable", "game": "tm",
 "state": "ok", "created": "2026-10-21T12:00:00Z",
 "started": "2026-10-21T12:00:00Z", "ended": "2026-10-21T12:03:10Z",
 "exit": 0, "summary": "Tetra Master is enabled and started.", "missing": []}
```

`state` is `queued`, `running`, `ok`, `failed` or `needs_data`. Times are
UTC; `started` and `ended` are null until they happen. `exit` is the exit
code of the command that ended the job. `missing` is `[{"path", "hint",
"item"}]` (with `import` as in `/api/games`) for a `needs_data` job, the
required items only, the volumes it could not set up for a job that failed
there (`path` is `volume <name>`, `hint` the reason), and empty otherwise. An import job also has `item`
and `source`, as they were asked for; patch jobs are described under
"Patch bundles" below.

`GET /api/jobs/<id>` is the same object plus `log`, the last 64 KB of the
log, and `log_size`, the number of bytes the job has written in all.

`GET /api/jobs/<id>/log?offset=<n>` is the log as `text/plain` from byte
`n` on, for following a running job: ask again with `n` plus the length of
what came back. The headers say `X-Job-State`, `X-Log-Offset` (the byte the
answer starts at, which is past `n` when the beginning of a long log was
dropped) and `X-Log-Size`.

### Client data

`GET /api/browse?path=<folder>` lists a folder under `POL_DEPLOY_CLIENTS`;
`path` is relative to it and empty for its top.

```
{"root": "/srv/clients", "path": "FantasyEarth", "parent": "",
 "dirs": ["DATA", "Model"], "files": [{"name": "FE_Client.dll", "size": 6291456}],
 "truncated": false,
 "matches": [{"game": "fe", "item": "fedata", "label": "Fantasy Earth: game data"}]}
```

`parent` is the folder above (`""` for the top), or null at the top.
At most 500 entries are listed; past that `truncated` is true. `files` is
always there, empty or not. Entries that are symbolic links leading out of
the root are not listed. `matches` are the importable items whose
`expects` are all in this folder. It answers 400 for a path that is
absolute, has a `..` component or leads out of the root through a symbolic
link, 404 for a folder that is not there, and 409 when `POL_DEPLOY_CLIENTS`
is unset or not mounted.

`POST /api/uploads` takes the archive as the raw body, with
`Content-Length` and an optional `X-Filename` (percent-encoded UTF-8, for
display). It answers once the archive is stored, unpacked and listed:

```
201 {"upload": "20261021T120000-1a2b3c", "id": "20261021T120000-1a2b3c",
     "name": "FantasyEarth.zip", "created": "2026-10-21T12:00:00Z",
     "last_import": null, "size": 2147483648, "files": 5120,
     "matches": [{"game": "fe", "item": "fedata", "label": "Fantasy Earth: game data",
                  "subdir": "FantasyEarth"}]}
```

`size` is the unpacked size. An upload's `matches` are searched for in its
top folder and a few levels below it, each with the `subdir` to import
from. Errors: 411 without `Content-Length`, 413 `{"error", "max"}` when the
archive or its contents are over `POL_DEPLOY_UPLOAD_MAX` (`max` is that
cap in bytes) or it holds too many files, 400 for anything else wrong with
the archive, 507 when the volume has no room. Upload ids match
`[0-9]{8}T[0-9]{6}-[0-9a-f]{6}`.

`GET /api/uploads` lists them, newest first, as `{"uploads": [{"id",
"name", "created", "last_import", "size", "files", "matches"}]}`.
`DELETE /api/uploads/<id>` removes one and answers 204, 404 for an unknown
id, and 409 while an import reads it.

`POST /api/actions` with

```
{"action": "import", "game": "fe", "item": "fedata",
 "source": {"upload": "<id>", "subdir": "FantasyEarth"}}
```

or `"source": {"path": "FantasyEarth"}` (a folder under
`POL_DEPLOY_CLIENTS`) starts an import job (202). Before that it answers
400 for an unknown item, an item without `import`, a source that is not
one of those two shapes, a folder that leads outside the upload or the
clients folder, or an unknown upload; 409 for a `path` source while
browsing is off, or while another job runs; and 400 with the files the
folder lacks when it is not the right one:

```
400 {"error": "This folder is not your Fantasy Earth install folder, ...: it has no DATA/dat.pak.",
     "missing": [{"path": "DATA/dat.pak", "hint": "your Fantasy Earth install folder, ..."}]}
```

## Updates

The deployer also tells the panel whether the core and each installed game
run the newest version of their code, and what changed since. It checks
when it starts, every six hours, after an `install` or `update` job, and
when the panel asks for a fresh check. A check never waits for a job and
no job waits for a check; a checkout a job is working in is not fetched
while it runs.

What runs and what is newest depend on how the stack runs:

- Built from the checkouts (the default): what runs is `git rev-parse
  HEAD` in the checkout. The deployer runs `git fetch --quiet` there (as
  the checkout's owner, stopped after a minute, never asking for a
  password) and compares with the branch the checkout follows, or
  `origin/<branch>` from the catalog when it follows none. `changes` are
  `git log --no-merges HEAD..<that branch>`. A checkout with commits of
  its own says so in `why`, since `update` (`git pull --ff-only`) refuses
  then.
- Published images (`POL_DEPLOY_GHCR=1`): what runs is the commit in the
  `org.opencontainers.image.revision` label of the image the tag names on
  this host (`docker image inspect`), and for the core also
  `OPENLOBBY_REVISION` in the image's environment, which the image build
  sets. What is newest comes from the GitHub repository in the catalog
  entry's `source` (`{"repo": "owner/name", "branch": "main"}`; the
  core's default is the public OpenLobby repository), through the GitHub
  API: `GET /repos/<repo>/commits?sha=<branch>` and `GET
  /repos/<repo>/compare/<running>...<newest>`.

GitHub is asked without a token, so its limit for unauthenticated callers
applies: 60 requests an hour from one address. A full check asks about
two requests per published image. Answers are kept for an hour, and a
comparison of two commits for good, in the `pol-deployer` volume
(`github-cache.json`), so a restart asks nothing new. With the limit used
up, or GitHub out of reach, the items say `unknown` and why, and the next
check tries again.

A version pin is followed. When the image's tag variable (`OPENLOBBY_TAG`
for the core, the `tag_var` of a game's image) is set to anything but
`latest`, the newest commit is looked for on that pin only: `1.2.3` is git
tag `v1.2.3`, `1.2` the newest `v1.2.x` tag, `sha-<commit>` that commit.
When the branch has newer commits outside the pin, `why` says so.

`GET /api/updates` answers at once with the last results:

```
{"checked_at": "2026-10-21T12:00:00Z", "checking": false,
 "items": [{"key": "core", "name": "OpenLobby core (PlayOnline Viewer services)",
            "running": {"commit": "1a2b3c4d5e6f...", "built_at": "2026-10-20T08:30:00Z"},
            "latest": {"commit": "9f8e7d6c5b4a...", "date": "2026-10-21T09:12:00Z"},
            "behind": 3,
            "changes": [{"commit": "9f8e7d6c5b4a...", "date": "2026-10-21T09:12:00Z",
                         "subject": "Kart Club: show lap times in the lobby"}, ...],
            "update": "available", "why": null,
            "how": "Update it from Settings > Install games; ..."},
           {"key": "tm", "module": "tm", ...}]}
```

- `items` holds the core and every installed game, the core first. A game
  item also has `module`, its `admin_module` from the catalog.
- `update` is `available`, `current` or `unknown`. `unknown` comes with a
  one-sentence `why`: the checkout or the image does not say which commit
  it is, the remote or GitHub could not be reached, the limit is used up,
  or the running commit is not in the repository (a build of your own).
- `behind` is how many commits the newest version has that the running one
  lacks, null when unknown. `changes` lists them newest first, merge
  commits left out, at most 50.
- Commits are 40 hex digits; times are ISO 8601 UTC. `built_at` is null
  for a checkout and for an image that does not say.
- `how` is set when an update is available.
- `checking` is true while a check runs.
- `channel` (top level) is `stable` or `dev`; on the dev channel an item
  that runs a dev image has `"channel": "dev"` ("Dev channel" below).

`GET /api/updates?refresh=1` starts a check now, past the hour's cache,
and answers at once with `checking` true; ask again until it is false.
One forced check is made a minute at most: another within the minute
answers the last results with `"limited": true` and `retry_after`, the
seconds to wait.

The image the public workflow publishes carries the commit it was built
from twice: in the `org.opencontainers.image.revision` label and in
`OPENLOBBY_REVISION` (with `OPENLOBBY_BUILT_AT`), from the build
arguments of the same names in `services/Dockerfile`. An image you build
yourself without them reads as `unknown`, unless it runs from a checkout.

### In the admin panel

The panel (`services/adminupdates.py`) shows one list built from three
sources, the first that has an item for a key winning:

1. the deployer's items, when a deployer is set up and answers;
2. for a game the deployer has no item for, the game's own report: the
   `update` object of its status JSON, in the same shape
   (docs/admin-modules.md);
3. for the core, when no deployer answers, the panel's own check: its
   image's `OPENLOBBY_REVISION` and `OPENLOBBY_BUILT_AT` against the
   newest commits of `POL_ADMIN_UPDATE_REPO` (default
   `PrettyOpenLobby/OpenLobby`) on `POL_ADMIN_UPDATE_BRANCH` (default
   `main`), from the GitHub API, kept an hour, forced at most once a
   minute. Its `how` is `POL_ADMIN_UPDATE_HOW`, by default "Run docker
   compose pull and docker compose up -d."

Whatever a game sends is checked before it is shown: strings are trimmed,
commits must be hex and times ISO 8601, at most 50 changes are kept, and a
report that does not fit the shape shows as `unknown` with a sentence
saying so. `POL_ADMIN_GITHUB_API` points the panel's check at another
GitHub API, as the tests do.

## Dev channel

An install follows the stable channel by default: the images published to
ghcr.io, and the upstream images the games run (FFXI's world server is the
LandSandBoat team's own image). The dev channel follows preview builds
instead. The developers hand out an access code; the code says where the
builds are, so you never type an address, and the panel calls them "the
dev builds". Only the images that have a dev build change; the rest stay
on the published images, and the panel lists which.

The catalog says which images have a dev build. A `games/*.json` entry may
carry a `dev` list:

```
"dev": [{"replaces": "ghcr.io/landsandboat/server", "image": "lsb-server", "tag": "stock",
         "services": ["db-update", "connect", "search", "world", "map"],
         "source": "lsb-server", "license": "GPL-3.0-or-later",
         "what": "The world server: ...", "back": "..."}]
```

`replaces` is the stable image (no tag), `image` and `tag` its name under
the dev registry, `services` every service that runs it, `source` its
repository under the dev source address (or a full `https://` address of
its own), `license` the licence of that source, and `back`, when given,
what switching back to Stable does not undo. On the dev channel the image
becomes `<registry>/<image>:<tag>`. The core's images and each game's own
image follow the `dev` tag, which the developers' CI moves on every push
to their main branch; the panel shows each as its short name, such as
`lsb-server (dev)`.

A dev build of an image OpenLobby itself publishes (the core's images, a
game's own image) is used with published images only (`POL_DEPLOY_GHCR=1`):
a stack built from its checkouts builds those images itself. A dev build of
an upstream image, such as the world server above, is used either way.
With games that add a title (`login` and `authsess` run the title chain's
last link), the dev core image is the base the chain is built on rather
than the image those two services run.

### Access codes

An access code is `olx1.` followed by a base64url string (no padding) of
compact JSON:

```
{"r": "registry.example.org/team", "t": "<token>", "s": "https://code.example.org/team"}
```

`r` is the registry, host and path, where the dev images are. `t` is the
token that reads them; a code without one is for public images and signs
in as nobody. `s` is where their source repositories are (default
`https://<r>`). `n` is optional: a name a game's own setup uses for its
repository and image, 1 to 64 of `a-z`, `0-9`, `.`, `_` and `-`, starting
with a letter or digit; OpenLobby itself ignores it, and an issuer hands it
on as its base code has it. Nothing else may be in it: a code with another field, a
field of the wrong kind, a damaged string or padding is refused with "This
access code is damaged or incomplete; copy it again, whole.", and one that
starts `olx2.` or later with "This access code is for a newer version of
OpenLobby; update OpenLobby first, then enter it again."

A code that also opens the developers' patch feed ("Patches from the dev
channel" below) starts `olx2.` and has one more field, `p`, the feed's
`host/owner`. An OpenLobby too old to know `olx2.` says to update first.

The developers make codes with `python tools/stack.py dev-code --registry
HOST/PATH [--token-file FILE] [--source URL] [--patches HOST/OWNER] [--name NAME]`. The token is read from the
first line of FILE, or from stdin with `--token-file -`, never from the
command line. `stack.py dev-code --decode` reads a code on stdin and says
what is in it, the token left out.

Advanced: a plain registry token still works as the code when the deployer
knows the registry, from `POL_DEV_REGISTRY` or a `registry` given beside
the code to `POST /api/channel`, `deployer.py --channel dev --registry`
or the setup scripts' `--registry`. That is how codes worked before; an
`olx1.` code always uses its own registry.

### Dev access codes

The developers can also hand out short dev codes, such as
`ABCD-EFGH-JKMN`: 12 characters, no 0, O, 1, I or L, typed with or
without the dashes in any case. A short code unlocks one server. The
first server that enters it keeps it, any other server is refused, and
the developers can revoke it for that one server.

Each deployer makes a random install id once (`ol-` and 32 hex digits)
and keeps it in `install_id` in its volume, mode 600. Switching channel
keeps it. It is not a secret, but the panel shows it only to the owner
(`GET /api/channel` has it as `install_id`).

Wherever the deployer takes an access code (its own switch to Dev, and a
paired game's switch), it takes a short code too. It sends the code and
its install id to the issuer named by `POL_DEV_ISSUER`, the developers'
panel:

    POST <POL_DEV_ISSUER>/api/dev-code/redeem
    {"code": "ABCDEFGHJKMN", "install_id": "ol-..."}

and uses the `olx1.` (or `olx2.`) code it answers with
(`{"ok": true, "access_code": "olx1...."}`) as if it had been typed. With
`POL_DEV_ISSUER` empty, the default, a short code is refused with a
sentence saying so. `GET /api/channel` says `"short_codes": true` when it
is set.

The issuer is any OpenLobby admin panel set up to hand out codes. Its
owner makes 1 to 100 codes at a time with a note in Settings, Modules,
"Dev access codes", lists them (unused, in use, revoked, with when and
which install redeemed them) and revokes them; each of those is in the
activity log.

#### Kinds of code

One panel can hand out codes for several things, such as its own dev
builds and a paired game's. Each thing is a kind, and every code is made
for one kind:

    POL_DEV_ISSUER_KINDS=kart:Kart Club,derby:Derby Night

A key is a lower-case letter then up to 11 letters or digits; the label
is what the owner's page shows. Each kind has its own settings, named
with its key in capitals:

- `POL_DEV_ISSUER_KART_CODE`: the access code (`olx1.` or `olx2.`) that
  kind's codes redeem to. Its registry, source and patch feed are what
  every redeemed code of the kind carries. Unset, or not a code the panel
  can read: the kind hands out nothing, and its codes are refused with 503.
- `POL_DEV_ISSUER_KART_FORGEJO_URL`, `_FORGEJO_USER` and `_FORGEJO_AUTH`:
  a token of its own per code (below).
- `POL_DEV_ISSUER_KART_SCOPES`: the scopes of those tokens, a comma list
  of `read:package` and `read:repository` (`read:package` when unset). A
  kind whose updater reads a repository branch as well as images needs
  `read:repository,read:package`. Any other scope stops the kind, and the
  owner's page says why.

Without `POL_DEV_ISSUER_KINDS` the panel has one unnamed kind (key `''`),
set by the same settings without the key: `POL_DEV_ISSUER_CODE`,
`POL_DEV_ISSUER_FORGEJO_URL` and so on. That is how a panel from before
kinds works, unchanged. Codes made then are of the unnamed kind, so once a
panel names its kinds they list as "kind not configured".

The owner picks the kind when making codes (a choice shows only when more
than one kind is ready), and the list shows each code's kind. A server
holds at most one live code of each kind, so it can hold a Kart Club code
and a Derby Night code at once, but not two Kart Club codes.

When a kind is taken out of the settings, its codes stay in the list as
"kind not configured" and redeeming one answers 503. Revoking one still
works, but the panel no longer has the Forgejo settings to delete its
token, so the answer says the token is left and names it, for deleting by
hand (or put the settings back and press Revoke again).

#### A token per code

By default every short code of a kind redeems to the kind's code itself,
which suits public packages that need no token: the short code then only
records who has dev, and revoking one stops that server from redeeming it
again but not from using the access code it already has. With the kind's
`_FORGEJO_URL`, `_FORGEJO_USER` and `_FORGEJO_AUTH` set, the issuer
instead makes a token of its own for each code on Forgejo, for that user,
with the kind's scopes, named `openlobby-dev-<kind>-<code>`
(`openlobby-dev-<code>` for the unnamed kind), and answers with the kind's
registry, source and patch feed and that token. Revoking the code deletes
the token, so the server's next pull fails with "The access code no longer
works" while every other server keeps working.

Forgejo makes tokens only for a password sign-in, so `_FORGEJO_AUTH` is
`name:password`: that user's own, or a site admin's, who then acts for the
user (the `Sudo` header). The user must be one that can only read what the
kind's codes open, and each kind needs a user of its own: a Forgejo token
cannot be limited to one organization, so a token of a user shared by two
kinds would read both. A second kind on the same Forgejo user is refused
with a sentence on the owner's page. The page says, for each kind, whether
its codes get their own token or all share the kind's code.

The table is `dev_codes` (polcore migrations 0021 and 0022): `code` (the
key), `kind`, `note`, `made_by`, `created_at`, `redeemed_at`,
`install_id`, `token_ref`, `token` and `revoked_at`, with one live code per
install and kind.

#### Redeeming a code (for setup programs)

A deployer redeems codes itself, but any program can, for example a game's
setup program that takes a short code from its user. The request needs no
panel login:

    POST <issuer>/api/dev-code/redeem
    Content-Type: application/json

    {"code": "ABCD-EFGH-JKMN", "install_id": "ol-0123456789abcdef0123456789abcdef"}

`code` is the short code as the user typed it: 12 characters of A to Z
and 2 to 9 without O, I or L, in any case, with or without dashes or
spaces. `install_id` names the server the code is for: 16 to 64 of A to Z,
a to z, 0 to 9, `_` and `-`. Make it once at random, keep it, and send the
same one every time; a deployer's is `ol-` and 32 hex digits. The code is
bound to the first install id that redeems it. Send a `User-Agent` of your
own: a front such as Cloudflare may refuse an HTTP library's default one
(Python's `Python-urllib` gets error 1010), and the deployer sends
`openlobby-deployer`.

Every answer is JSON. A refusal carries `error`, a sentence to show the
user as it is; no sentence repeats the code.

| Status | When | Body |
| --- | --- | --- |
| 200 | the code is now bound to this install | `{"ok": true, "access_code": "olx1...."}` |
| 200 | this install redeemed it before | `{"ok": true, "access_code": "olx1....", "again": true}`, the same access code as the first time |
| 400 | the body is not JSON with a `code` that fits and an `install_id` that fits | `{"error": "..."}` |
| 403 | the code was revoked | `{"error": "That dev code was revoked. Ask for a new one."}` |
| 404 | the code is not known, or the panel hands out no codes at all | `{"error": "..."}` |
| 409 | another install redeemed the code, or this install already holds another live code of the same kind | `{"error": "..."}` |
| 429 | too many tries from this address (`POL_ADMIN_PAIR_TRIES` a minute, 5 by default) | `{"error": "Too many tries from this address. Try again in N seconds."}`, and a `Retry-After` header in seconds |
| 502 | the code is fine, but the issuer could not make its token on Forgejo | `{"error": "..."}`; try again later, nothing was bound |
| 503 | the code's kind is not set up on the panel now, or the panel has no admin credential and is not on loopback | `{"error": "..."}` |

`access_code` is an `olx1.` or `olx2.` code, as described in "Access codes"
above: use it as if the user had typed it. Treat it as a secret
(with a token per code it holds one), and do not log it.

#### Taking redeems from the internet

Installs run by other people reach the issuer over the internet, and the
panel itself should not be on the internet. Put a reverse proxy in front of
it that passes on exactly one request, `POST /api/dev-code/redeem`: no other
method, no other path (not even one that starts with this one or adds a
query string), a small body (4 KB is plenty) and short timeouts. Have it
limit tries per client address too, and send only the JSON body and an
`X-Forwarded-For` header it writes itself, never the client's own.

The panel counts tries (the 429 above) and writes the activity log by client
address. Behind a proxy every request comes from the proxy, so one client
would use up the tries for everyone. Set `POL_ADMIN_TRUSTED_PROXY` on the
panel to the one address the proxy connects from: for that route only, and
only on a connection from exactly that address, the panel takes the client's
address from the last entry of `X-Forwarded-For`. Unset, the header is never
read. It takes a single address, not a range; anything else is ignored with a
line in the panel's log. Anything else on the same host that connects from
that address can name its own client address on this route, so pick an
address only the proxy uses where you can.

Give users the proxy's public base URL as the issuer (`POL_DEV_ISSUER`), for
example `https://example.org`.

### Switching

In the admin panel, Settings > Install games > Update channel (the owner
only): choose Dev and paste the access code, or a short dev code. The panel hands the code to
the deployer and keeps nothing; the field is cleared as soon as it is sent.
The deployer then

1. asks the registry, the way docker does (`/v2/`, then its token
   endpoint with the code's token, or as nobody), whether the code reads
   the dev images this install would run, and answers with a sentence when
   it does not: a wrong code, a code without read access, an image that is
   not there, or a registry that does not answer. Nothing has changed at
   that point.
2. starts a `channel` job: with a token, `docker login --password-stdin`
   with the token on stdin, into a Docker client config of its own
   (`DOCKER_CONFIG` is `channel/docker` in the deployer's volume, never the
   host's); keeps the code in `channel/code`, mode 600, in a folder of mode
   700; runs `stack.py channel dev --registry <registry>` (and `--source`
   from the code or `POL_DEV_SOURCE`), which writes `.channel` in the
   checkout (no secret in it); then pulls and restarts the stack as an
   update does.

No answer the panel reads and no line of a job log names the registry: the
deployer's sentences say "the dev builds", and a job log shows
`[dev builds]` where a tool printed its address. The activity log says
"switched to the dev channel" or "tried to switch to the dev channel" with
the deployer's sentence. The code is in no log, job file, answer or
argument: the job log blanks it, and the token inside it, if a tool ever
prints them.

From then on every docker command the deployer runs uses that client
config, so updates and installs pull the dev images with the same buttons.
Before an update pulls anything, and before enabling a game, the deployer
asks the registry again. A code that has been revoked or has expired fails
the job with "The access code no longer works; ask for a new one or switch
back to Stable." and nothing is pulled. The update list says "dev channel",
marks the items that run a dev image, and shows such an item as having an
update when the registry's digest for its tag is one this host does not
have. "Enter a new access code" in the same panel replaces the code.

Switching back to Stable runs `stack.py channel stable` (which removes
`.channel`), `docker logout`, deletes the code and the client config, then
pulls and restarts with the published images.

A dev image may change a game's database in ways the stable image does
not undo. FFXI's dev world server runs its own migrations at start, so the
world database keeps them after a switch back. Back that database up
before switching to Dev (`docker compose exec db mariadb-dump ...` in the
FFXI checkout, or a copy of the `crystalbridge_lsb-db` volume), and restore it to go
back exactly.

### Source

On the dev channel the panel shows the owner a link to the source of each
dev image, named after its licence (for example "GPL-3.0 source"). The
link goes through the panel (`/api/deploy/channel/source`), which answers
with a redirect to the address: the catalog's own `https://` address, or
the image's `source` under the code's `s` (or `POL_DEV_SOURCE`, or
`https://<registry>`), so the page itself never holds it.

### Patches from the dev channel

The developers may also keep patch bundles for the servers they hand codes
to. An access code with a patch feed in it opens them. Patches have their
own switch, "Patches from the developers" under Patch bundles on the Install
games tab, apart from the update channel: they work on Stable as well as
Dev, and they are off until the owner turns them on, whatever code the
install holds. Turning them on takes a code with a feed in it, or uses one
already kept (from an earlier time, or the dev channel's own code when it
has a feed), after the feed has answered to it. When on, the section lists
each title the feed has, the version this server has, the feed's newest,
and a Get or Update button. When off, nothing asks the feed.

The patch code is kept apart from the channel's, in `patchfeed/code` in the
deployer's volume (mode 600), with `patchfeed/state.json` saying whether
patches are on. Switching the channel back to Stable, which forgets the
channel's code, leaves it alone; turning patches off keeps it too, unless
the owner asks to forget it. A short dev code that redeems to an `olx2.`
code is offered to the patch store (kept when none is kept yet), never
turned on by itself.

The feed is a Forgejo or Gitea package registry the code's token can read.
Each bundle is a generic package `polpatch-<REGION>-<PRODUCT>`, each patch
version a version of it, holding `full.polpatch` (the whole bundle up to
that version), `update-from-<VERSION>.polpatch` (that version on top of
VERSION), or both. To bring a title up to date the deployer takes the chain
of updates from the version the server has when every link is there, else
the newest full file and the updates after it. A server that has versions
the feed does not know ("Own versions") is not offered anything, since a
full file would leave them out.

Each file is downloaded into the deployer's volume, its size and SHA-256
checked against what the registry lists, then checked and applied exactly
as an imported `.polpatch` is. The patch service is restarted once at the
end, after any downloads in progress ("Patch bundles" above). The job log
never names the feed's address or the token.

### By hand and from the setup scripts

`stack.py channel [--json]` shows the channel and what each image is on
it (`--json` includes the registry); `stack.py channel dev --registry
HOST/PATH` and `stack.py channel stable` set it. They only write
`.channel`: signing in to the registry is up to you (`docker login`) when
you run the stack without the deployer. `stack.py compose` applies the
channel by adding a compose file that sets `image:` for the services of
the dev images, last in the `-f` list and removed after the command;
`--dry-run` prints it.

`setup.sh --channel dev`, `setup.ps1 -Channel dev` and the setup program's
`--channel dev` ask for the code without showing it (or read it from
`--code-file` / `-CodeFile` with `--yes`) and hand it to the running
deployer on stdin: `docker compose --profile deployer exec -T deployer
python /app/deployer.py --channel dev`. `--channel stable` switches back
the same way.

`POST /api/channel` with `{"channel": "dev", "code": "olx1...."}` answers
202 with the job, 400 with a sentence when the request or the code is
refused, 502 when the registry does not answer, and 409 while another job
runs. `{"channel": "stable"}` starts the switch back. `GET /api/channel`
answers

```
{"channel": "dev", "since": "2026-10-23T09:14:00Z", "code_stored": true,
 "ghcr": true, "problem": null, "install_id": "ol-5f0c...", "short_codes": true,
 "images": [{"key": "ffxi", "name": "...", "stable": "ghcr.io/landsandboat/server",
             "dev_name": "lsb-server (dev)", "applies": true, "license": "GPL-3.0-or-later",
             "what": "...", "note": null, "enabled": true, "has_source": true}, ...]}
```

with one row per stable image of the core and each installed game; a row
with no dev build has `dev_name` null and a `note`. `has_source` is only
ever true on the dev channel. `GET /api/channel/source?stable=<image>`
answers `{"url", "license"}` for such a row, and 404 on the stable channel
or for an image without a source address.
A client data import from an upload that is a `.polpatch` is a 400.

### Patch bundles

Every upload, in the 201 answer and in `GET /api/uploads`, also has `kind`,
`"archive"` or `"polpatch"`, and `polpatch`, which is null for an archive.
For a `.polpatch` upload it is what the manifest says:

```
201 {"upload": "20261021T120000-1a2b3c", "id": "20261021T120000-1a2b3c",
     "name": "W2U-0002-20030429_0.polpatch", "created": "2026-10-21T12:00:00Z",
     "last_import": null, "size": 52428800, "files": 412, "matches": [],
     "kind": "polpatch",
     "polpatch": {"region": "W2U", "product": "0002", "kind": "full",
                  "versions": ["20030429_0"],
                  "title": "Tetra Master (Windows, North America and Europe)"}}
```

`size` is the size of the file and `files` the number of files in its
bundle. A `.polpatch` whose manifest cannot be read or that fails the quick
check is refused with 400 `{"error", "problems"}` (`problems`: up to ten
sentences) and leaves nothing behind. The 409 answer means the check could
not be started safely (see "Who runs what").

In `GET /api/browse`, an entry of `files` whose name ends in `.polpatch`
also has `polpatch`, the same five fields read from its manifest only, or
`{"error": "<sentence>"}` when the manifest cannot be read.

`GET /api/patches` lists the bundles in the mirrors folder:

```
{"bundles": [{"region": "W2U", "product": "0002",
              "title": "Tetra Master (Windows, North America and Europe)",
              "aliases": [], "versions": ["20030429_0", "20040714_0"],
              "latest": "20040714_0", "served": true,
              "selfcheck": "ok", "selfcheck_at": "2026-10-21T12:03:10Z",
              "selfcheck_why": null, "size": 52690944}]}
```

- `versions` runs oldest to newest, and `latest` is its last entry.
- `served`: the running patch service answers a version check for this
  bundle with the bundle's own latest version, so it has loaded it.
- `selfcheck` is `"ok"`, `"failed"` or null (never run on these versions),
  `selfcheck_at` when it ran (UTC), and `selfcheck_why` one sentence when it
  failed, else null.
- `size` is the bundle folder's size in bytes.
- A bundle whose list files cannot be read also has `error`.

With `?recheck=1` the answer is 202 and adds `"job"`: a `patch_check` job
that runs the self-check of every bundle. Follow it like any job and ask
for the list again when it ends. While another job runs it is 409 with the
list, the running job's id as `job`, and `error`. Without `POL_DEPLOY_MIRRORS`
the endpoint answers 409 `{"error"}`.

`POST /api/actions` takes the patch actions, without `game`:

```
{"action": "patch_import", "source": {"upload": "<id>"}}
{"action": "patch_import", "source": {"path": "updates/W2U-0002-20040714_0.polpatch"}}
{"action": "patch_build", "region": "W2U", "product": "0002",
 "source": {"path": "TetraMaster"}}
{"action": "patch_build", "region": "W2U", "product": "0002",
 "source": {"upload": "<id>", "subdir": "TetraMaster"},
 "version": "20030429_0", "walk": false, "aliases": ["W20"]}
{"action": "patch_rollback", "region": "W2U", "product": "0002", "to": "20030429_0"}
{"action": "patch_remove", "region": "W2U", "product": "0002"}
{"action": "patch_check"}
{"action": "patch_fetch", "region": "W2U", "product": "0002"}
```

`patch_fetch` brings the bundle to the patch feed's newest version ("Patches
from the dev channel"). It answers 409 `{"error", "feed": false}` while
patches from the developers are off. `POST /api/patches/feed` turns them on
or off:

```
{"enabled": true, "code": "olx2...."}     (code optional: the kept one is used)
{"enabled": false}                        (the code is kept)
{"enabled": false, "forget": true}        (and removed)
```

and answers 200 `{"feed", "enabled", "can_enable"}` (on: also `bundles`, the
feed's count), 400 for a body of another shape, no code to use, or a code
with no feed in it, and 502 `{"error"}` when the feed refuses the code or
does not answer, in which case nothing is changed.
`GET /api/patches/feed[?refresh=1]` lists what the feed offers (the listing
is reused for five minutes unless `refresh=1`):

```
200 {"feed": true, "bundles": [{"region": "W2U", "product": "0002",
     "title": "Tetra Master (Windows, North America and Europe)",
     "latest": "20040714_0", "have": "20030429_0", "status": "behind",
     "files": 1, "size": 2318, "why": null}]}
```

`status` is `new` (not here), `behind`, `current`, `ahead` (newer here),
`custom` (versions here the feed lacks) or `unavailable` (no file reaches
the newest); `files` and `size` are what fetching takes, and `why` says why
for the last two. While patches are off it answers 409 `{"error", "feed":
false, "enabled": false, "can_enable"}`, `can_enable` saying whether a kept
code would turn them on with nothing typed, and
502 `{"error"}` when the feed refuses the code or does not answer.

`patch_build` takes three optional fields. `version` is the version to
build (default `auto`, read from the install's `patch.ver`). With `walk`
true it takes every file in the folder instead of the list in `file.txt`,
which an install normally has. `aliases` are other region codes the bundle
answers for.

They answer 202 `{"job"}`, or before any job starts:

- 400 for a `region` that is not `[A-Z0-9]{3}`, a `product` that is not
  `[0-9]{4}`, a `to` or `version` that is not `[0-9]{8}_[0-9A-Za-z]`, a
  `patch_build` for a region or product the patch service does not know, a
  source that is not one of the shapes above or leads outside its folder, a
  `path` that is not a `.polpatch` file (import) or not a folder (build), an
  upload of the wrong kind, a `.polpatch` whose manifest cannot be read, and
  a `to` that is not one of the bundle's versions or is its latest;
- 404 when there is no bundle for that region and product (roll back,
  remove);
- 409 while another job runs, when `POL_DEPLOY_MIRRORS` is unset, when a
  `path` source is used while browsing is off, or when the core checkout has
  no `tools/patch` toolkit.

A patch job has `"game": null` and, as far as they apply, `region`,
`product`, `source`, `to`, and `removes` (the versions a roll back takes
out, oldest first). It ends `ok`, or `failed` with a sentence: a refused
`.polpatch`, a failed restart, or a self-check that failed after the
bundle was installed, which the summary says. Paths in a summary are
written from the folder they are in (`mirrors/`, `clients/`,
`deployer-volume/`); the log has them in full.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `POL_DEPLOYER_TOKEN` | (none) | Required. The bearer token; the admin service gets the same value. |
| `POL_DEPLOY_ROOT` | (none) | Required. Absolute path of the directory holding the checkouts, as the Docker daemon sees it (on Docker Desktop for Windows, `/run/desktop/mnt/host/<drive>/...`). |
| `POL_DEPLOY_CORE` | `POL_DEPLOY_ROOT/openlobby` | The OpenLobby checkout, when it has another name, written the same way. |
| `POL_DEPLOY_GHCR` | empty | `1`: actions use the published images. |
| `POL_DEPLOY_CLIENTS` | empty | Absolute path of a folder of client installs to browse for imports, as the daemon sees it, mounted read-only. Empty: uploads only. |
| `POL_DEPLOY_UPLOAD_MAX` | `8G` | The most an uploaded archive may be, and may unpack to (`K`, `M`, `G` suffixes, powers of 1024). |
| `POL_MIRRORS` | `./mirrors` | The patch service's mirrors folder, which the deployer mounts as well for the patch actions. |
| `POL_DEPLOY_MIRRORS` | `/mirrors` | Set in `docker-compose.yml`: where that folder is inside the deployer's container. Empty: the patch actions are off. |
| `POL_VALKEY_URL` | `valkey://valkey:6379/0` | Set in `docker-compose.yml`: where the patch service reports downloads in progress. Empty: patch actions restart the patch service without waiting. |
| `POL_DEPLOY_PATCH_DRAIN` | `1800` | The most seconds a patch action waits for downloads to finish before it restarts the patch service. |
| `POL_DEPLOYER_BIND` | `0.0.0.0:8099` | Address inside the container. Change `POL_DEPLOYER_URL` with it. |
| `POL_DEPLOYER_URL` | `http://deployer:8099` | Where the admin service finds the deployer. |
| `POL_DEPLOY_GITHUB_API` | `https://api.github.com` | The GitHub API the update check asks. Tests point it at a stand-in. |
| `POL_DEV_REGISTRY` | empty | Advanced: the registry, `host/path`, a plain token is for. An access code (`olx1.`...) carries its own and does not need it. |
| `POL_DEV_SOURCE` | `https://<registry>` | Where the dev images' source repositories are, when the access code does not say. |
| `POL_DEV_USER` | `dev` | The user name an access code without one (`name:code`) signs in under. |
| `POL_DEV_REGISTRY_HTTP` | empty | `1`: the registry speaks plain http (tests, a registry on your network). |
| `POL_DEV_ISSUER` | empty | The panel that redeems short dev codes for this install, its base URL ("Dev access codes"). Empty: only full access codes are taken. |

The issuer's own settings are the admin service's, not the deployer's:
`POL_DEV_ISSUER_KINDS`, and for each kind `POL_DEV_ISSUER_<KEY>_CODE`,
`_FORGEJO_URL`, `_FORGEJO_USER`, `_FORGEJO_AUTH` and `_SCOPES` (without
the key for a panel with one unnamed kind; "Dev access codes" above).

## The image

`services/deployer/Dockerfile` builds on the same Python image as the rest
of the stack and adds git from Debian, and the official static builds of
the docker CLI, the compose plugin and buildx, each pinned by version and
checksum in `services/deployer/fetch_docker.py`, and pinned Pillow, numpy,
fontTools and brotli for the import tools. The server is
`services/deployer/deployer.py` with `clientdata.py` (archives,
paths and uploads), `devchannel.py` (the dev channel, which `stack.py` and
the admin panel use too), `updatecheck.py` (the update check, which the
admin panel uses too), `patches.py` (bundles, self-check results, roll
backs) and `patchtool.py` (the `.polpatch` steps that run unprivileged)
beside it, standard library only. The `.polpatch` toolkit comes from the
OpenLobby checkout's `tools/patch`. It runs as root inside its
container: it needs the socket, whose group id differs from host to host,
and it has to start git and the import tools as the checkout's owner.

The import tools run in the deployer's container rather than in each
game's image because the game images carry only the server (`services/`)
and not the repository's `tools/`, and starting another container for them
would hand a request's folder to the Docker daemon.

`python tools/deployer_test.py` tests it offline, with stand-ins for
`stack.py`, an import tool, the docker CLI and the GitHub API, and needs
only Python and git. It
builds its malicious archives itself. Its docker stand-in also acts out
the start-up path check: a daemon that sees the folder, one that sees
another, and one that cannot mount it. For the patch actions it builds a
small bundle in three versions with the toolkit, and its stand-in
`stack.py` restarts a real patch server on loopback ports.
