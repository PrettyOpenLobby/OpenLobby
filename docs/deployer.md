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
  itself (see "Importing client data" and "Patch bundles").
- The admin container never gets the socket. It holds the token and asks.

Anyone who has the token, or who can run code in a container on that
network, can install, update and restart your games, and through a game
repository you point it at, run code as root. Treat the token like the
host's root password, keep the admin panel on 127.0.0.1 as it ships, and
leave the profile off if you do not want the panel to manage games.

## Requirements

- A Linux host with Docker Engine. The deployer mounts your checkouts at
  the same path inside its container as on the host (see below), which is
  what makes the relative paths in the game compose files mean the same
  thing to the Docker daemon. With Docker Desktop on Windows or macOS the
  daemon sees a different filesystem from the host, and that is not
  supported.
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
   absolute host path. If the checkout is not named `openlobby`, also set
   `POL_DEPLOY_CORE` to its full path.

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
   is wrong it says so there and stops.

The admin service finds the deployer through `POL_DEPLOYER_URL` (default
`http://deployer:8099`) and `POL_DEPLOYER_TOKEN`, both from `.env`. Set
`POL_DEPLOY_GHCR=1` to have every action use the published images, the
way `stack.py compose --ghcr` does.

To stop managing games from the panel, `docker compose --profile deployer
stop deployer` (or `rm -sf deployer`). To update the deployer itself after
pulling OpenLobby, run `docker compose --profile deployer up -d --build
deployer` by hand: the deployer never recreates itself.

## What each action runs

Every command runs with the OpenLobby checkout as its working directory.
`compose ...` below is `stack.py compose`, with `--ghcr` added when
`POL_DEPLOY_GHCR` is on. When it is on, `--build` is left out.

| Action | Games | What it does |
| --- | --- | --- |
| install | a game | `stack.py add <key> --yes` (git clone beside openlobby), then `stack.py check --json <key>` |
| enable | a game | `stack.py check --json <key>`; if the game's required data is there, `stack.py enable <key>`, then `compose up -d --build` |
| disable | a game | `stack.py disable <key>`, then `compose up -d --build --remove-orphans` |
| update | a game or `core` | refuses when the checkout has uncommitted changes to tracked files, otherwise `git -C <checkout> pull --ff-only`, then for an enabled game a data check and `compose up -d --build` |
| restart | an enabled game or `core` | `compose --no-prebuild up -d --force-recreate <its services>` |
| check | a game or `core` | `stack.py check --json` for that game, or for every enabled game with `core`; changes nothing |

A few details that follow from those commands:

- `disable` rebuilds as well, since `login` has to be rebuilt without the
  game's title plugin; `stack.py disable` gives the same advice.
- `enable` checks the data first. A game that lacks a required file is not
  enabled, so its containers never start and fail over and over.
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
and not to root. `docker compose` runs as root inside the container.

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

With Docker Desktop the checkout can appear owned by root inside the
container, so imports run as `nobody`, which then needs write access to
the checkout's data folders.

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

## HTTP API

JSON in and out, except the raw log. Every endpoint, `/api/health`
included, needs the bearer token and answers 401 without it. Errors carry
an `error` sentence.

`GET /api/health` answers `{"ok": true, "version": "3.0", "busy": false}`;
`busy` is true while a job runs.

`GET /api/games` lists every game in the catalog:

```
{"games": [{"key": "tm", "name": "Tetra Master", "admin_module": "tm",
            "installed": true, "enabled": true, "running": true,
            "data_ok": true, "missing": [],
            "admin_env": ["POL_ADMIN_GAME_TM_URL", "POL_ADMIN_GAME_TM_TOKEN"],
            "ports": [...], "notes": "...", "services": {"tmrank": "running"}},
           ...],
 "core": {"running": true, "services": {...}},
 "options": {"ghcr": false}}
```

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
required items only, and empty otherwise. An import job also has `item`
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
```

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
| `POL_DEPLOY_ROOT` | (none) | Required. Absolute host path of the directory holding the checkouts. |
| `POL_DEPLOY_CORE` | `POL_DEPLOY_ROOT/openlobby` | The OpenLobby checkout, when it has another name. |
| `POL_DEPLOY_GHCR` | empty | `1`: actions use the published images. |
| `POL_DEPLOY_CLIENTS` | empty | Absolute host path of a folder of client installs to browse for imports, mounted read-only. Empty: uploads only. |
| `POL_DEPLOY_UPLOAD_MAX` | `8G` | The most an uploaded archive may be, and may unpack to (`K`, `M`, `G` suffixes, powers of 1024). |
| `POL_MIRRORS` | `./mirrors` | The patch service's mirrors folder, which the deployer mounts as well for the patch actions. |
| `POL_DEPLOY_MIRRORS` | `/mirrors` | Set in `docker-compose.yml`: where that folder is inside the deployer's container. Empty: the patch actions are off. |
| `POL_DEPLOYER_BIND` | `0.0.0.0:8099` | Address inside the container. Change `POL_DEPLOYER_URL` with it. |
| `POL_DEPLOYER_URL` | `http://deployer:8099` | Where the admin service finds the deployer. |

## The image

`services/deployer/Dockerfile` builds on the same Python image as the rest
of the stack and adds git from Debian, and the official static builds of
the docker CLI, the compose plugin and buildx, each pinned by version and
checksum in `services/deployer/fetch_docker.py`, and pinned Pillow, numpy,
fontTools and brotli for the import tools. The server is
`services/deployer/deployer.py` with `clientdata.py` (archives, paths and
uploads), `patches.py` (bundles, self-check results, roll backs) and
`patchtool.py` (the `.polpatch` steps that run unprivileged) beside it,
standard library only. The `.polpatch` toolkit comes from the OpenLobby
checkout's `tools/patch`. It runs as root inside its
container: it needs the socket, whose group id differs from host to host,
and it has to start git and the import tools as the checkout's owner.

The import tools run in the deployer's container rather than in each
game's image because the game images carry only the server (`services/`)
and not the repository's `tools/`, and starting another container for them
would hand a request's folder to the Docker daemon.

`python tools/deployer_test.py` tests it offline, with a stand-in for
`stack.py` and for an import tool, and needs only Python and git. It
builds its malicious archives itself. For the patch actions it builds a
small bundle in three versions with the toolkit, and its stand-in
`stack.py` restarts a real patch server on loopback ports.
