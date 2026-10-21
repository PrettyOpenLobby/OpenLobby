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
  keys in `games/*.json`. Nothing from a request reaches a command line
  except a key that matched the catalog.
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
files on the host as the game's README says, then run the action again.

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

`GET /api/health` answers `{"ok": true, "version": "1.0", "busy": false}`;
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
  "required"}`, optional items included.
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
code of the command that ended the job. `missing` is `[{"path", "hint"}]`
for a `needs_data` job, the required items only, and empty otherwise.

`GET /api/jobs/<id>` is the same object plus `log`, the last 64 KB of the
log, and `log_size`, the number of bytes the job has written in all.

`GET /api/jobs/<id>/log?offset=<n>` is the log as `text/plain` from byte
`n` on, for following a running job: ask again with `n` plus the length of
what came back. The headers say `X-Job-State`, `X-Log-Offset` (the byte the
answer starts at, which is past `n` when the beginning of a long log was
dropped) and `X-Log-Size`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `POL_DEPLOYER_TOKEN` | (none) | Required. The bearer token; the admin service gets the same value. |
| `POL_DEPLOY_ROOT` | (none) | Required. Absolute host path of the directory holding the checkouts. |
| `POL_DEPLOY_CORE` | `POL_DEPLOY_ROOT/openlobby` | The OpenLobby checkout, when it has another name. |
| `POL_DEPLOY_GHCR` | empty | `1`: actions use the published images. |
| `POL_DEPLOYER_BIND` | `0.0.0.0:8099` | Address inside the container. Change `POL_DEPLOYER_URL` with it. |
| `POL_DEPLOYER_URL` | `http://deployer:8099` | Where the admin service finds the deployer. |

## The image

`services/deployer/Dockerfile` builds on the same Python image as the rest
of the stack and adds git from Debian, and the official static builds of
the docker CLI, the compose plugin and buildx, each pinned by version and
checksum in `services/deployer/fetch_docker.py`. The server is
`services/deployer/deployer.py`, standard library only. It runs as root
inside its container: it needs the socket, whose group id differs from host
to host, and it has to start git as the checkout's owner.

`python tools/deployer_test.py` tests it offline, with a stand-in for
`stack.py`, and needs only Python and git.
