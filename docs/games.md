# Running games with the core

OpenLobby on its own is the PlayOnline Viewer's side of the service: login,
the lobby, mail, friends, the portal and the patch server. Each game lives
in its own repository and is added on top. This page explains how the
pieces fit, how `tools/stack.py` runs any set of them as one stack, the
catalog that describes each game (`games/*.json`), and what each game needs
from your own client.

## How games combine

Every game repository is a compose override. Its `docker-compose.yml` is
applied after OpenLobby's, with OpenLobby's checkout as the project
directory, so the game's services run in the core's compose project: on its
network, with its PostgreSQL, its Valkey and its data volume. The paths in
those files are relative to the project directory, which is why each game
must be checked out beside `openlobby` under the directory name its compose
files use (`hippaulmaster`, `hippaulring` and so on).

A game has up to two parts:

- Its own services, such as Fantasy Earth's `fellb`, `felobby` and
  `feworld`, or the FFXI bridge and LandSandBoat. These get their own images,
  built on the core image because they use the core's database layer.
- A title plugin, a Python module that runs inside the core's `login` and
  `authsess` processes (`services/titles.py`). The core loads every module
  named in `POL_TITLES`. Tetra Master and Janhourou are played almost
  entirely through it; for the others it fills the Viewer's profile screen,
  and for FFXI it is required (without it the world connect fails with
  POL-0001).

Because a plugin is imported by the core's own process, it has to be inside
the image `login` and `authsess` run. Each game therefore ships a title
image that is the core image plus its plugin (`FROM` the core, with the base
image as the build argument `OPENLOBBY_IMAGE`). There is only one `login`
service, so to run several plugins their images are stacked: the first is
built on the core, the second on the first, and so on, and `POL_TITLES`
lists them all. That chain is what `stack.py` builds for you.

## stack.py

`tools/stack.py` needs only Python 3 and works from any directory. Game
READMEs call it as `python ../openlobby/tools/stack.py`.

```
python tools/stack.py list                 the games, which are cloned and enabled
python tools/stack.py add fe               print the git clone that puts a game beside openlobby
python tools/stack.py add fe --yes         ... and run it
python tools/stack.py enable tm,jan        record games to run (any number, comma or space separated)
python tools/stack.py disable jan
python tools/stack.py check                checkouts, data files and volumes of the enabled games
python tools/stack.py check --json --all   the same for every game in the catalog, as JSON
python tools/stack.py ps                   which games have running containers (--json too)
python tools/stack.py compose up -d --build
python tools/stack.py compose --dry-run up -d --build
python tools/stack.py compose --ghcr up -d
python tools/stack.py compose logs -f login
```

`enable` and `disable` write the game keys to `.games` in the OpenLobby
checkout (one per line, ignored by git). `compose` passes everything after
it to `docker compose`, adding:

- `--project-directory` set to the OpenLobby checkout;
- `--env-file` for OpenLobby's `.env` and then each enabled game's `.env`,
  those that exist;
- `-f` for OpenLobby's compose file and then each enabled game's files, in
  the catalog's order whatever order you enabled them in;
- `POL_TITLES`, the enabled games' plugins in the same order, and
  `POL_TITLE_BASE_IMAGE`, the image the last plugin is built on.

Before `up`, `build` or `create` it builds the core image and every title
image but the last, one after another, so each exists before the next is
built on it. Docker's build cache makes that quick when nothing changed;
`--no-prebuild` skips it. `--dry-run` prints every command with its
variables and runs nothing, which is also the way to see the by-hand
equivalent of a stack. Any `docker compose` subcommand works: `ps`, `logs`,
`exec`, `run`, `down`, `config`.

To remove a game, `disable` it and run
`python tools/stack.py compose up -d --build --remove-orphans`, which
rebuilds `login` without its plugin and removes its containers. Its data
stays in the database and volumes.

`stack.py check` exits non-zero when an enabled game is missing its
checkout, a compose file, a required data file or a required volume, and
warns about optional data and missing `.env` files.

## Deploying games from the admin panel

The admin panel can run these steps for you: install a game, enable or
disable it, pull updates and restart it, each with a button. The panel does
not hold the Docker socket itself. It asks a separate service, the
deployer, which runs `stack.py` with a fixed set of arguments and keeps a
log of each job. The deployer is off unless you start its profile:

```
docker compose --profile deployer up -d deployer
```

It needs `POL_DEPLOYER_TOKEN` and `POL_DEPLOY_ROOT` in `.env` first, and
access to the Docker socket makes it as powerful as root on the host.
docs/deployer.md explains the setup, what each action runs, and the risk.

## The image chain

The catalog's `order` fixes the chain: Tetra Master, Janhourou, Fantasy
Earth, Front Mission Online, Dirge of Cerberus, FFXI. With Tetra Master,
Fantasy Earth and FFXI enabled, `compose up -d --build` runs:

1. `build dns` on the core's file alone, which tags `openlobby:latest`;
2. `build login` with Tetra Master's file and `POL_TITLE_BASE_IMAGE=openlobby:latest`,
   which tags `hippaulmaster:latest`;
3. `build login` with Fantasy Earth's title file and
   `POL_TITLE_BASE_IMAGE=hippaulmaster:latest`, which tags `hippaulring-title:latest`;
4. the command you asked for with all the files, `POL_TITLES=tmtitle,fetitle,ffxititle`
   and `POL_TITLE_BASE_IMAGE=hippaulring-title:latest`, so `login` and
   `authsess` run `hippaulbridge-title:latest`.

Each title compose file builds `login` with
`OPENLOBBY_IMAGE: "${POL_TITLE_BASE_IMAGE:-${OPENLOBBY_IMAGE:-openlobby:latest}}"`.
The separate variable keeps the games' own images (`fellb`, `fmo`, `doc`,
the bridge) on the core image while the plugins stack. Set `OPENLOBBY_IMAGE`
yourself only to put the whole stack on a core image of your own.

When several compose files define `login`, compose keeps the last one's
image and build, so the last title file in the `-f` list must be the last
link of the chain. Every title file names its `dockerfile` for the same
reason. The services a game adds have names of their own (`tmboard`,
`janboard` and `fmoboard` for the three web boards), so no two games
override each other.

A title image inherits the user of the image below it. The Tetra Master and
Janhourou images switch to uid 1000 and the FFXI title image back to root,
so the last of those in the chain decides which user `login` runs as, the
same user it runs as with that game alone. `hippaulbridge/lsb/Dockerfile.title`
says why the FFXI one stays root (the core's mail ports, 25 and 110).

## Settings and addresses

OpenLobby's `.env` is read first and each game's `.env` after it, so a game
can override a core value. An empty `KEY=` in a later file overrides an
earlier value with nothing, which every compose file then reads as "use the
default"; `stack.py` warns when that happens. `POL_TITLES` and
`POL_TITLE_BASE_IMAGE` in an env file are ignored, since `stack.py` sets
them.

The address clients reach the host on is `POL_ADVERTISE` in OpenLobby's
`.env`. The games use it too, unless a game's own setting is filled in:

| Game | Own setting | Empty means |
| --- | --- | --- |
| Tetra Master | `POL_TM_ZONE_HOST` | `POL_ADVERTISE` |
| Fantasy Earth | `FE_ADVERTISE` | `POL_ADVERTISE`, then 127.0.0.1 |
| FFXI | `LSB_ADVERTISE_IP` | `POL_ADVERTISE`, then 127.0.0.1 |
| Front Mission Online, Dirge of Cerberus | none | `POL_ADVERTISE` |

## Published images

`compose --ghcr` uses the images published to `ghcr.io/prettyopenlobby/`
instead of building the core and the games' own services. A title image is
still a chain: the Tetra Master and Janhourou title images are published,
built on the published core, so whichever of them comes first in the chain
is pulled, and every later link is built locally on top of it. The other
games publish their service images only, so their plugin is always built
locally, on the published core or on the link before it. `OPENLOBBY_TAG`
and each game's `*_TAG` variable pick versions.

## By hand

The same stack without `stack.py`, from the OpenLobby checkout:

1. Build the core: `docker compose build`.
2. For each title but the last, in chain order, build its title image on
   the one before it, for example
   `POL_TITLE_BASE_IMAGE=openlobby:latest docker compose -f docker-compose.yml -f ../hippaulmaster/docker-compose.yml build login`.
3. Run the stack with every file in chain order, both kinds of env file,
   and the two variables:

```
POL_TITLES=tmtitle,fetitle,ffxititle POL_TITLE_BASE_IMAGE=hippaulring-title:latest \
docker compose --env-file .env --env-file ../hippaulring/.env --env-file ../hippaulbridge/.env \
    -f docker-compose.yml \
    -f ../hippaulmaster/docker-compose.yml \
    -f ../hippaulring/docker-compose.yml -f ../hippaulring/docker-compose.title.yml \
    -f ../hippaulbridge/docker-compose.yml -f ../hippaulbridge/docker-compose.title.yml \
    up -d --build
```

Compose refuses an `--env-file` that does not exist, so list only the
`.env` files you have. Each game's README has the command for that game
alone.

## What each game needs

| Game | Key | Content ID | Client | From your copy | Tool | Without it |
| --- | --- | --- | --- | --- | --- | --- |
| Tetra Master | `tm` | 2 | PC, PS2 | the parameter tables (`services/tmdata/*.BIN`) | `tools/tmdata_build.py --client <install>` | does not run |
| | | | | board art (optional) | `tools/tm_boardart_bake.py --client <install>` | the board has no art |
| Janhourou | `jan` | 3 | PS2 | nothing | | |
| | | | | the board's bitmap font (optional) | `tools/jan_boardfont_build.py --kanji <install>/Warashi/font/kanji.bin` | text board only |
| Fantasy Earth | `fe` | 11 | PC | cipher tables (no client needed; the published image has them) | `tools/gen_blowfish_tables.py` | the image does not build |
| | | | | world data (`services/fedata/`) | `tools/fedata_build.py --client <install>` | no playable world |
| Front Mission Online | `fmo` | 4 | PC, PS2 | cipher tables (no client needed; the published image has them) | `tools/gen_blowfish_tables.py` | the image does not build |
| | | | | game tables (`services/fmodata/`) | `tools/fmodata_build.py --client <install>` | runs with gaps |
| Dirge of Cerberus | `doc` | 10 | PS2 (Japanese disc) | arena data (optional) | `tools/doc_extract_arena.py <game data>` | missions have no enemy spawns |
| FFXI | `ffxi` | 1 | PC, PS2 | nothing; two mesh volumes from LandSandBoat's image | the `docker volume create` and `docker run` lines in its README | the map server does not start |

Each tool runs on the host, in that game's checkout, with Python 3.10 or
newer. `stack.py check` reports which of these are present.

## Ports

| Game | Ports published on the host |
| --- | --- |
| core | 53/udp, 80, 443, 25, 110, 51200-51240, 51241-51250, 51251-51271, 51273-51304, 51305, 51306-51310, 51330, 54000, 53001-53015; 8080 and 8090 on 127.0.0.1; 51112/udp with `--profile gm` |
| Tetra Master | none; the board on 127.0.0.1:8793 with `--profile board` |
| Janhourou | 51272; the board on 127.0.0.1:8791 with `--profile board` |
| Fantasy Earth | 54848, 54849, 54850; the map on 127.0.0.1:8797 with `--profile map` |
| Front Mission Online | 61300 (TCP and UDP), 127.0.0.1:8798; the board on 127.0.0.1:8792 with `--profile board` |
| Dirge of Cerberus | 55040/udp |
| FFXI | 54001, 54002, 54004, 54230 (TCP and UDP); 127.0.0.1:8088 |

No two games publish the same port, so any set of them can run together.

## The catalog

`games/<key>.json` describes one game (and `games/core.json` the core) as
plain data, so other tools can read it: `stack.py`, its test
(`tools/stack_test.py`, which checks every entry against the game
checkouts), and the admin panel's planned list of games to install. Paths
are relative to the game's checkout.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | number | Always 1. |
| `key` | string | Short name, the same as the file name. |
| `kind` | string | `core` or `game`. |
| `name` | string | Display name. |
| `order` | number | Position in the compose file list and the image chain. |
| `content_id` | number or null | The PlayOnline content code (the one granted on the admin panel). |
| `platforms` | list | `pc`, `ps2`. |
| `repo` | string | The repository's URL (clone it with `.git` appended). |
| `source` | object | Where the update check looks for newer versions: `repo` (`owner/name` on GitHub) and `branch` (docs/deployer.md, "Updates"). |
| `checkout` | string | Directory name beside `openlobby`; the compose files expect exactly this. |
| `compose` | list | Compose files applied, in order, when building. |
| `compose_ghcr` | list | Overrides applied after them for published images. |
| `env_example` | string | The settings template to copy to `.env`. |
| `title` | object or null | The title plugin: `module` (its `POL_TITLES` name), `compose` (the file that swaps it into `login` and `authsess`), `image` (the local tag it builds), `ghcr_image` and `ghcr_tag_var` (the published title image and its version variable, or null). |
| `images` | list | Images the game builds: `name`, `context` and `dockerfile` (relative to the checkout), `ghcr` and `tag_var` (or null). |
| `services` | list | Services the game adds (beyond `login` and `authsess`). |
| `profiles` | object | Optional services by compose profile, `{"board": ["tmboard"]}`. |
| `env` | list | Settings its compose files read: `name`, `default` (an empty string where the default is in the code), `description`. |
| `data` | list | Files to produce from your own copy: `id`, `description`, `source`, `tool`, `paths` (glob patterns; empty when the result is not in the checkout), `required`, and `build_only` (needed only when building, not with published images). |
| `volumes` | list | External Docker volumes to create first: `name`, `description`, `required`, `setup` (the commands). |
| `ports` | list | Published ports: `port` (a number or a range, as a string), `protocol`, `service`, `host` (the bind address), and `profile` when the service is optional. |
| `admin_module` | string or null | The admin panel module for the game (a module key in `services/adminmodules/`, which also names the `game.<key>` permissions and the `POL_ADMIN_GAME_<KEY>_URL` setting; see `docs/admin-modules.md`), or null when the game has none. An installed game whose module is enabled shows up in the panel's Games page. |
| `notes` | string | Anything else a person installing it should know. |
