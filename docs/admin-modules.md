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
     "fields": [{"id": "handles", "label": "Handle", "type": "text"}]}
  ],
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

Action kinds the panel looks for:

| kind | used by |
|---|---|
| `announce` | the maintenance warning on the News page, to tell players inside the game |
| `kick` | an account's Kick, to remove the player from this game too. It is sent `{"polid": "...", "handles": "Name1,Name2"}` (the account's PlayOnline ID and its handles), whatever fields it lists |

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

## Server checks

A host ops runner (not part of this repository) can read the module files'
`Service(...)` lists to know which containers make up each game, and offer a
restart button per game. It should read the files as text and run nothing from
them, since it runs with the rights to restart the stack. The status and
request files it shares with the panel are described in `services/adminops.py`,
under "Server checks and fixes".
