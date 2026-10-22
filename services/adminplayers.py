"""One player across every game: what each game knows about a PlayOnline account.

A game module may answer for a player (Module.player: fn(module, who) -> dict or None; a game
that describes itself does it through /openlobby/player.json, adminmodules.remote). `who` is
{"polid", "handles": [...], "members": [...], "links": {game: [{"key", "name"}]}}: the account's
PlayOnline ID, its handle names, member ids, and the logins on other games' servers its members
have proved are theirs (keyed by the account system, an id the game server chooses), which
is how the games know their players. The answer is

    {"characters": [{"name", "detail", "online", "facts": [{"label", "value"}]}], "note"}

or None when the game has never seen this player. Each game needs its own permission (the
game's permission to see it at all); the account window needs accounts_view. Answers come back
in parallel; a game that does not answer is named and the others still show.
"""
import concurrent.futures

import adminmodules

GAME_TIMEOUT = 5.0


def _str(v, n=120):
    return " ".join(str(v if v is not None else "").split())[:n]


def _clean(m, doc):
    chars = []
    for c in (doc.get("characters") or [])[:24]:
        if not isinstance(c, dict):
            continue
        chars.append({"name": _str(c.get("name"), 60) or "?", "detail": _str(c.get("detail"), 160),
                      "online": c.get("online") if isinstance(c.get("online"), bool) else None,
                      "facts": [{"label": _str(f.get("label"), 40), "value": _str(f.get("value"), 120)}
                                for f in (c.get("facts") or [])[:12] if isinstance(f, dict)]})
    return {"game": m.key, "title": m.title, "characters": chars, "note": _str(doc.get("note"), 300)}


def lookup(who, can, owner):
    """(status, {"games": [...], "problems": [...]}) for the account `who` describes."""
    mods = [m for m in adminmodules.enabled().values()
            if getattr(m, "player", None) and (owner or adminmodules.can_see(m.key, can))]
    out, problems = [], []
    if mods:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(mods))) as pool:
            futs = {pool.submit(m.player, m, who): m for m in mods}
            done, pending = concurrent.futures.wait(futs, timeout=GAME_TIMEOUT)
            for f in done:
                m = futs[f]
                try:
                    doc = f.result()
                except Exception:          # noqa: BLE001 -- one game down is a note
                    problems.append("%s is not answering right now" % m.title)
                    continue
                if isinstance(doc, dict) and (doc.get("characters") or doc.get("note")):
                    out.append(_clean(m, doc))
            for f in pending:
                problems.append("%s took too long" % futs[f].title)
    order = list(adminmodules.enabled())
    out.sort(key=lambda g: order.index(g["game"]) if g["game"] in order else 99)
    return 200, {"games": out, "problems": sorted(problems),
                 "asked": [m.title for m in mods]}
