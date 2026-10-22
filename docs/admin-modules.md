# Game modules in the admin panel

The admin panel knows nothing about any particular game. Everything it shows or
does for a game comes from that game's module: who is playing, the containers
behind it, the game's own tool pages, and actions such as announcing in game.
A server runs whichever games it likes, and the panel shows those.

There are two ways to give the panel a game.

## 1. The game describes itself

A game server that speaks the OpenLobby admin protocol needs no code in the
panel. `tools/olport.py` is the game side in one file (standard library,
Python 3.8+): copy it into the game server, describe the game, and decorate a
status function and the actions; its docstring has an example. Give the admin
service two settings:

    POL_ADMIN_GAME_<KEY>_URL     where the game's admin port answers
    POL_ADMIN_GAME_<KEY>_TOKEN   the token that port expects

`<KEY>` is the game's short key in capitals (`KART` for `kart`). On start, and
again every minute until it answers, the panel asks the game what it is.

Every request from the panel carries `X-Devtool-Token` (the token) and
`X-Devtool-Actor` (who in the panel asked). A port that binds beyond loopback
should refuse a request without the right token.

### GET /openlobby/module.json

```json
{
  "v": 1,
  "key": "kart",
  "title": "Kart Club",
  "short": "Kart",
  "platform": "PS2 HDD",
  "about": "Four-player kart racing.",
  "content_ids": [],
  "tools": [
    {"id": "laps", "label": "Lap times", "path": "laps", "about": "Fix a bad lap."}
  ],
  "actions": [
    {"id": "banner", "label": "Banner message", "kind": "announce",
     "fields": [{"id": "text", "label": "Message", "type": "text"}]},
    {"id": "kick", "label": "Kick a player", "kind": "kick", "danger": true,
     "fields": [{"id": "handles", "label": "Handle", "type": "text"}]},
    {"id": "mute", "label": "Mute a racer", "danger": true,
     "fields": [{"id": "racer", "label": "Racer", "type": "player"}]}
  ],
  "players": true,
  "accounts": "kartid",
  "services": [
    {"name": "Kart Club", "containers": ["kart-game-1"], "ports": {"7000": "game"},
     "warning": "Everyone in a race is disconnected."}
  ]
}
```

`key` has to match the key the panel was given. Ids are lower case letters,
digits, `-` and `_`. A tool is a web page the game serves at `<url>/<path>`;
the panel shows it inside its own page and passes the page's requests through,
so a tool owns its path and everything under it (`laps`, `laps.json`,
`laps/edit`). The panel never passes a browser's request to `/openlobby/`.

Each tool and each action is a separate moderator permission, so an owner can
let a moderator post banners without letting them kick.

A field's `type` is `text` (the default) or `player`. A `player` field is a
search box: the moderator types part of a name or id and picks from a list, and
the panel sends the picked player's `id` as the field's value. It needs
`"players": true` and the players search below; a game without it gets a plain
text box.

`accounts` is optional: the name of the account system your players' logins
belong to, the same key the player lookup's `links` uses for them (several games
that sign in with one login share it). The People pages link a person's login in
your game under it, and send it back in `links` when they ask about that person.
Left out, it is the game's `key`.

Action kinds the panel looks for:

| kind | used by |
|---|---|
| `announce` | the maintenance warning on the News page, to tell players inside the game |
| `kick` | an account's Kick, to remove the player from this game too. It is sent `{"polid": "...", "handles": "Name1,Name2"}` (the account's PlayOnline ID and its handles), whatever fields it lists. A person's page offers it too, with each field filled in: a `player` field gets the person's login in your game, any other field its name |
| `ban` | a person's page offers it as "Ban in <game>", filled in the same way as `kick` |

### GET /openlobby/status.json

What is going on right now. Every field is optional.

```json
{
  "up": true,
  "online": 2,
  "players": [{"name": "Wren", "room": "Lap 3", "level": 12}],
  "rooms": [{"name": "Friday race", "players": 2, "max": 4, "state": "racing"}],
  "rooms_label": "races",
  "summary": "Grand prix tonight at 21:00 UTC.",
  "facts": [{"label": "laps run today", "value": 41}],
  "error": null
}
```

`online` may also be a list of players, or `{"total": n, "players": [...]}`.
A player is a name or an object with `name` and, if it has one, where they are
(`zone`, `place`, `room` or `lobby`). The panel asks at most every few seconds
and caches the answer.

A game that knows whether it runs its newest version can say so in an
`update` object, which the panel shows among its update notices when the
deployer has nothing on the game:

```json
"update": {
  "running": {"commit": "1a2b3c4d5e6f", "built_at": "2026-10-20T08:30:00Z"},
  "latest": {"commit": "9f8e7d6c5b4a", "date": "2026-10-21T09:12:00Z"},
  "behind": 2,
  "changes": [{"commit": "9f8e7d6c5b4a", "date": "2026-10-21T09:12:00Z",
               "subject": "Show lap times in the lobby"}],
  "update": "available",
  "why": null,
  "how": "Pull the Kart Club image and restart it."
}
```

`update` is `available`, `current` or `unknown` (then say why in `why`).
Commits are hex, 7 to 40 digits; times are ISO 8601; `changes` are newest
first, at most 50 are shown. The key and name are the module's own. A
report that does not fit this shape is shown as unknown.

### GET /openlobby/calendar.json?from=&lt;unix&gt;&to=&lt;unix&gt;

Optional, for a game with a schedule: say `"calendar": true` in module.json
(olport does when you use `@port.calendar`) and the panel's Calendar shows what
you answer here, to anyone with the game's permission.

```json
{"events": [{"id": "gp-20261031", "title": "Halloween Grand Prix", "start": 1793404800,
             "end": 1793491200, "kind": "cup", "about": "Pumpkin karts only.",
             "times": [[1793469600, 1793473200]]}]}
```

`start` and `end` are unix seconds; answer the entries that touch the range.
`times` is optional: the sessions inside a long entry, shown in the week view.
A game that does not answer within a few seconds is named on the Calendar and
the rest still shows.

### GET /openlobby/players.json?q=&lt;text&gt;&limit=&lt;n&gt;

Optional, for the player picker: say `"players": true` in module.json (olport
does when you use `@port.players`).

```json
{"players": [{"id": "1042", "label": "Wren", "detail": "Class A, 41 races", "online": true},
             {"id": "877", "label": "Wrenna", "detail": "Class C, 3 races", "online": false}]}
```

Match `q` against names and ids, ignoring case; put the players online now
first. `limit` is 1 to 50 (20 when left out); answer 400 for anything else.
An empty `q` may answer the players online now. `id` is what an action field
of type `player` receives, so make it whatever your actions look players up
by. `online` is `true`, `false` or `null` when the game cannot tell. Anyone
who may see the game in the panel may search its players.

### POST /openlobby/actions/&lt;id&gt;

The body is a JSON object of the action's fields. Answer
`{"ok": true, "msg": "Shown in every lobby."}` or `{"ok": false, "msg": "why"}`.
The panel writes every action to its activity log with who ran it.

## 2. A module file

For a game whose server does not speak the protocol, write a small Python file
and put it in the folder named by `POL_ADMIN_MODULES_DIR`. The games OpenLobby
ships with are written this way, in `services/adminmodules/`.

```python
from adminmodules import Action, Module, Service, Tool, sources

def _parse(doc, status):
    status["summary"] = "Track: %s" % doc.get("track", "?")

MODULE = Module(
    "kart", "Kart Club", short="Kart", platform="PS2 HDD",
    tools=[Tool("laps", "Lap times", "laps")],
    services=[Service(["kart-game-1"], {7000: "game"})],
    live=sources.board(_parse),
)
```

A module file can offer the player picker too: `Module(..., players=find)`,
where `find(module, q, limit)` returns the same rows as players.json.

`sources.board` reads the JSON at `POL_ADMIN_GAME_KART_LIVE` (a URL or a file)
in the status shape above, then calls `_parse` for anything only this game has.
Tools are served from `POL_ADMIN_GAME_KART_URL`, as with the protocol.

A container name may say `{core}` for the core stack's compose project
(`POL_ADMIN_CORE_PROJECT`, `openlobby` unless set).

## Which games a server shows

`POL_ADMIN_MODULES` is a comma list of keys. Left unset, the panel shows every
module that finds its game: one with a tool URL, a status source, or a
live-session marker some service has published. The owner's Modules page in
Settings lists any module that did not load, and why.

## Running the panel for one game

The panel can run on its own for a game that has nothing to do with
PlayOnline: one game server that describes itself (section 1), with the panel
beside it for status, actions, tool pages, logs, the calendar and players'
help requests. Set `POL_ADMIN_CORE=none` and the PlayOnline parts are switched
off: player accounts and registration codes, GM calls, player and bug reports,
news, the PlayOnline Viewer and PML Preview, the notice before Play and the
sessions count. Their routes answer 404, their permissions are not offered to
moderators, and the built-in PlayOnline title modules load only if
`POL_ADMIN_MODULES` names them. The News section keeps only the maintenance
warning, which goes into each game's `announce` action. The panel's own login,
moderators, activity log, settings and Overview stay as they are.

It needs PostgreSQL and a `/data` volume, nothing else: no Valkey (live state is
kept in memory), no web tree and no other OpenLobby service. On first start
the panel brings the database schema up to date by itself, the same migrations
as a full server; the PlayOnline tables are created but stay empty, and nothing
reads them in this mode.

```yaml
services:
  postgres:
    image: postgres:17-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: openlobby
      POSTGRES_PASSWORD: change-me
      POSTGRES_DB: openlobby
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "openlobby"]
      interval: 5s
  admin:
    image: ghcr.io/prettyopenlobby/openlobby:latest
    restart: unless-stopped
    entrypoint: ["python", "admin.py"]
    environment:
      POL_DATABASE_URL: postgresql://openlobby:change-me@postgres:5432/openlobby
      POL_ADMIN_CORE: none
      POL_ADMIN_MODULES: kart
      POL_ADMIN_GAME_KART_URL: http://kart:8820
      POL_ADMIN_GAME_KART_TOKEN: a-long-random-token
      POL_ADMIN_USER: admin
      POL_ADMIN_PASSWORD: a-long-password
    volumes:
      - admin-data:/data
    ports:
      - "127.0.0.1:8090:8090"
    depends_on:
      postgres:
        condition: service_healthy
volumes:
  pgdata:
  admin-data:
```

- `POL_ADMIN_CORE=none` turns the PlayOnline parts off. Unset, the panel is
  the full PlayOnline one.
- `POL_ADMIN_MODULES` lists the game keys to show (here `kart`).
- `POL_ADMIN_GAME_<KEY>_URL` and `POL_ADMIN_GAME_<KEY>_TOKEN` point at the
  game's admin port and its token, as in section 1. Use one pair per game.
- `POL_ADMIN_PASSWORD` (with `POL_ADMIN_USER`, `admin` unless set) is the
  first login. Once in, set a password under Settings, My login, which is kept
  in the database, and clear the variable again if you like.
- `/data` holds remembered sign-ins and the panel's files; the database holds
  the moderators, settings, calendar entries and the activity log.
- Port 8090 is the panel. Keep it on loopback or a private network and reach
  it through a tunnel; a panel with no password refuses every request unless
  it is bound to loopback (`POL_ADMIN_BIND`).

## Server checks

A host ops runner (not part of this repository) can read the module files'
`Service(...)` lists to know which containers make up each game, and offer a
restart button per game. It should read the files as text and run nothing from
them, since it runs with the rights to restart the stack. The status and
request files it shares with the panel are described in `services/adminops.py`,
under "Server checks and fixes".
