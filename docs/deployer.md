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
  keys in `games/*.json`, and the client data tools the catalog names for
  the `import` action. Nothing from a request reaches a command line
  except a key that matched the catalog and, for an import, the path of a
  folder the deployer checked itself (see "Importing client data").
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

## HTTP API

JSON in and out, except the raw log. Every endpoint, `/api/health`
included, needs the bearer token and answers 401 without it. Errors carry
an `error` sentence.

`GET /api/health` answers `{"ok": true, "version": "2.0", "busy": false,
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
 "options": {"ghcr": false, "problem": null}}
```

`options.problem` is the same as in `/api/health`.

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
and `source`, as they were asked for.

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

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `POL_DEPLOYER_TOKEN` | (none) | Required. The bearer token; the admin service gets the same value. |
| `POL_DEPLOY_ROOT` | (none) | Required. Absolute path of the directory holding the checkouts, as the Docker daemon sees it (on Docker Desktop for Windows, `/run/desktop/mnt/host/<drive>/...`). |
| `POL_DEPLOY_CORE` | `POL_DEPLOY_ROOT/openlobby` | The OpenLobby checkout, when it has another name, written the same way. |
| `POL_DEPLOY_GHCR` | empty | `1`: actions use the published images. |
| `POL_DEPLOY_CLIENTS` | empty | Absolute path of a folder of client installs to browse for imports, as the daemon sees it, mounted read-only. Empty: uploads only. |
| `POL_DEPLOY_UPLOAD_MAX` | `8G` | The most an uploaded archive may be, and may unpack to (`K`, `M`, `G` suffixes, powers of 1024). |
| `POL_DEPLOYER_BIND` | `0.0.0.0:8099` | Address inside the container. Change `POL_DEPLOYER_URL` with it. |
| `POL_DEPLOYER_URL` | `http://deployer:8099` | Where the admin service finds the deployer. |
| `POL_DEPLOY_GITHUB_API` | `https://api.github.com` | The GitHub API the update check asks. Tests point it at a stand-in. |

## The image

`services/deployer/Dockerfile` builds on the same Python image as the rest
of the stack and adds git from Debian, and the official static builds of
the docker CLI, the compose plugin and buildx, each pinned by version and
checksum in `services/deployer/fetch_docker.py`, and pinned Pillow, numpy,
fontTools and brotli for the import tools. The server is
`services/deployer/deployer.py` with `clientdata.py` (archives,
paths and uploads) and `updatecheck.py` (the update check, which the admin
panel uses too) beside it, standard library only. It runs as root inside its
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
another, and one that cannot mount it.
