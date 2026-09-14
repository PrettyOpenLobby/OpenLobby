"""Title plugins: how a game title's server logic attaches to the core.

The core (responders.py) owns the transport: the auth band the client keeps
open for its whole session, the lobby band's resource fetches, the member
profile, presence, rooms and the account database. A TITLE owns one game's
logic on top of that: what to answer on its game envelope, which resource blobs
it serves and patches, what its profile fields are, and what to clean up when
a player leaves. This module is the seam between the two.

A title is a module named in `POL_TITLES` (comma-separated, e.g. `tmtitle`).
At import the core calls `load()`, which imports each named module and calls
its `register()` function; the module builds a `Title` subclass instance and
hands it to `register(title)` here. The core never imports a title by name and
a title never imports `responders`: everything a title needs from the core is
reached through `core`, the handle bound by `bind_core(...)` before any title
is loaded, and enumerated in `Core.__doc__`.

Every hook has a no-op default, so a title implements only the points it
needs, and the core runs identically with no titles loaded (the hooks return
their neutral value and the generic paths stay in charge).

Dispatch helpers below (`notice`, `resource_patch`, ...) are what the core
calls; they walk the registered titles in registration order.
"""
import importlib
import os

#: Returned by a hook that does not own the message, so the core carries on
#: with its own handling. Distinct from None, which is a real "no reply".
PASS = object()


class _NoPresence:
    """Presence stub for a title running outside the core (its selftests)."""

    def sessions_for(self, member_id):
        return []

    def sessions_by_nick(self, nick):
        return []


class _NoRooms:
    """Room registry stub for the same standalone case."""

    def members(self, chan):
        return []

    def channels_of(self, sess):
        return []

    def owner(self, chan):
        return None


class Core:
    """The core plumbing a title may use, bound by responders at import.

    Bound names (the whole contract; a title uses nothing else of the core).
    They keep the core's own spelling so moved code reads unchanged:

      log(channel, text)              the rotated per-channel log file
      NoPad                           reply-line class framed without the pad
      _game_notice_line(body, target, nick, srv)   frame a game envelope
      _irc_host(srv)                  the server's IRC host string
      _session_get(field)             this thread's session record field
      _session_sid()                  this thread's session id
      _session_handle_id(db)          this thread's session handle id
      _sess_member_id(sess)           the member id behind a ChatSession
      _member_content_id(member_id, content_code)
      _member_display_name(member_id)
      _member_primary_handle(db, member_id)
      _member_still_present(remaining, member_id)
      PRESENCE                        .sessions_for(mid), .sessions_by_nick(nick)
      ROOMS                           the IRC room registry
      _live_rooms()                   the published room state (any container)
      _room_of_member(member_id)      the channel a member stands in, or None
      _resource_file(path, subject)   where a resource blob is stored
      _resource_read_file(path, subject)
      _resource_stored(path, subject)
      _fetch_subject(req_pt)          the subject id of a resource fetch
      RESOURCE_DIR                    the resource store root
      _peer_is_ps2()                  is THIS lobby connection a PS2 build
      _self_ip()                      the address this server advertises
      accounts                        the account database module (or None)
      polpro                          the POLpro plaintext channel module
      _mail_mint(...)                 mint a POL Message to a member

    The defaults below make a title importable and selftestable without the
    core: logging prints, presence is empty, sessions are unknown.
    """

    def __init__(self):
        self.bound = False
        self.PRESENCE = _NoPresence()
        self.ROOMS = _NoRooms()
        self.accounts = None
        self.polpro = None
        self.RESOURCE_DIR = os.environ.get("POL_RESOURCE_DIR", "/data/resources")

    # --- standalone defaults -------------------------------------------------
    def log(self, channel, text):
        try:
            print(f"[{channel}] {text}", flush=True)
        except UnicodeEncodeError:
            print(f"[{channel}] {text!a}", flush=True)

    def _session_get(self, field):
        return None

    def _session_sid(self):
        return None

    def _sess_member_id(self, sess):
        return getattr(sess, "member_id", None)

    def _live_rooms(self):
        return {}

    def _room_of_member(self, member_id):
        return None

    def _peer_is_ps2(self):
        return False

    def _self_ip(self):
        return "127.0.0.1"

    def _member_still_present(self, remaining, member_id):
        return False

    def __getattr__(self, name):
        # Only reached for names with no default and no binding.
        raise AttributeError(
            f"titles.core.{name} is not bound: the core has not called "
            f"bind_core(), or {name!r} is not part of the title contract")


core = Core()


def bind_core(**names):
    """Called once by responders with the names listed in `Core.__doc__`."""
    for k, v in names.items():
        setattr(core, k, v)
    core.bound = True
    for t in _TITLES:
        t.core_bound()


class Title:
    """One game title. Subclass, override what the title needs, register."""

    #: the three-character service tag of the game envelope (`G<tag>G...`)
    tag = b""
    #: the content-profile code (the N of `prof_00N.pfb`), or None
    content_code = None
    #: `path -> declared payload length` merged into the core's fetch table
    fetch_pathlen = {}
    #: `path -> fresh blob` merged into the core's RESOURCE_INIT
    resource_init = {}
    #: extra POLpro reply-template files (merged after /config/polpro.json)
    polpro_spec_files = ()

    def core_bound(self):
        """The core handle has just been (re)bound; refresh any aliases."""

    def describe(self):
        return ""

    # --- the auth band -------------------------------------------------------
    def notice(self, cls, payload, text, target, nick, srv, sess):
        """A game envelope `G<tag><cls><payload>` addressed to `target`.

        Return PASS to let the core handle it (the shared POLpro classes),
        None for "handled, nothing to send", or a list of framed lines."""
        return PASS

    def polpro_reply(self, cls, payload):
        """A POLpro (plaintext) request on this title's tag: (reply, handled)."""
        return None, False

    def polpro_noted(self, cls, payload, target):
        """A POLpro request was answered (or not): record what it told us."""

    def roster_sequence(self):
        """The `$SERIAL` a POLpro reply should carry for this session, or None."""
        return None

    def part_echo(self, nick, srv, sess):
        """Acknowledgement lines for a channel-less PART, or None."""
        return None

    def room_parted(self, chan):
        """A member PARTed `chan` (an affirmative departure)."""

    def room_notice(self, body, sess, nick):
        """Rewrite a room NOTICE body before relay: None, or
        ("rebroadcast", body) to send to every member including the sender,
        or ("filled", body) for the same with a name filled in."""
        return None

    def rooms_changed(self, state):
        """The room registry was published (a join, a part, a topic)."""

    def session_closed(self, member_id):
        """A member's last auth connection closed."""

    def band_role(self, cmd_txt):
        """A diagnostic label for what this in-session line says the band is."""
        return None

    def idle_pushes(self, member_id, peers):
        """Unprompted bodies due for `member_id`: [(peer_nick or None, body)]."""
        return []

    def requeue_pushes(self, member_id, items, why=""):
        """Bodies from idle_pushes that could not be sent; keep them."""

    # --- the lobby band ------------------------------------------------------
    def resource_length(self, path):
        """The declared payload length for a fetch of `path`, or None."""
        return None

    def resource_nodata(self, path, subject):
        """True to answer this fetch with File Not Found instead of a blob."""
        return False

    def resource_template(self, path):
        """The bytes to serve when nothing is stored for `path`, or None."""
        return None

    def resource_patch(self, path, data, subject):
        """Live values patched into a blob about to be served."""
        return data

    def store_patch(self, path, data):
        """A blob the client just wrote, before it is kept."""
        return data

    # --- the member profile --------------------------------------------------
    def profile_fields(self, cid, member_id):
        """`{schema slot: value}` for this title's content profile."""
        return {}

    def character_name(self, cid):
        """The game character's display name for a Content ID, or None."""
        return None

    def character(self, cid):
        """(name, info, member_id) for a Content ID from the title's own
        character pool, or None. `name` may be None when the pool holds no
        real name; `info` is the client's status line."""
        return None

    def describe_line(self, body):
        """A one-line description of a game-envelope body, for the log."""
        return repr(bytes(body[:64]))


_TITLES = []


def register(title):
    if not isinstance(title, Title):
        raise TypeError("register() wants a titles.Title")
    _TITLES.append(title)
    if core.bound:
        title.core_bound()
    return title


def all():
    return list(_TITLES)


def loaded():
    return bool(_TITLES)


def for_tag(tag):
    for t in _TITLES:
        if t.tag == tag:
            return t
    return None


def for_code(code):
    for t in _TITLES:
        if t.content_code == code:
            return t
    return None


def load(names=None):
    """Import and register the title modules named in POL_TITLES."""
    if names is None:
        names = os.environ.get("POL_TITLES", "")
    out = []
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        mod = importlib.import_module(name)
        reg = getattr(mod, "register", None)
        if reg is None:
            raise ImportError(f"title module {name!r} has no register()")
        out.append(reg())
    return out


def describe():
    return "; ".join(d for d in (t.describe() for t in _TITLES) if d)


# --- dispatch, in registration order ----------------------------------------
def notice(cls, tag, payload, text, target, nick, srv, sess):
    t = for_tag(tag)
    if t is None:
        return PASS
    return t.notice(cls, payload, text, target, nick, srv, sess)


def polpro_reply(cls, tag, payload):
    t = for_tag(tag)
    if t is None:
        return None, False
    return t.polpro_reply(cls, payload)


def polpro_noted(cls, tag, payload, target):
    t = for_tag(tag)
    if t is not None:
        t.polpro_noted(cls, payload, target)


def roster_sequence():
    for t in _TITLES:
        s = t.roster_sequence()
        if s is not None:
            return s
    return None


def part_echo(nick, srv, sess):
    for t in _TITLES:
        acks = t.part_echo(nick, srv, sess)
        if acks:
            return acks
    return None


def room_parted(chan):
    for t in _TITLES:
        t.room_parted(chan)


def room_notice(body, sess, nick):
    for t in _TITLES:
        r = t.room_notice(body, sess, nick)
        if r is not None:
            return r
    return None


def rooms_changed(state):
    for t in _TITLES:
        t.rooms_changed(state)


def session_closed(member_id):
    for t in _TITLES:
        t.session_closed(member_id)


def band_role(cmd_txt):
    for t in _TITLES:
        r = t.band_role(cmd_txt)
        if r:
            return r
    return None


def idle_pushes(member_id, peers):
    """[(title, peer_nick, body)] across every title."""
    out = []
    for t in _TITLES:
        for peer, body in t.idle_pushes(member_id, peers) or []:
            out.append((t, peer, body))
    return out


def resource_length(path):
    for t in _TITLES:
        n = t.resource_length(path)
        if n is not None:
            return n
    return None


def resource_nodata(path, subject):
    return any(t.resource_nodata(path, subject) for t in _TITLES)


def resource_template(path):
    for t in _TITLES:
        b = t.resource_template(path)
        if b is not None:
            return b
    return None


def resource_patch(path, data, subject):
    for t in _TITLES:
        data = t.resource_patch(path, data, subject)
    return data


def store_patch(path, data):
    for t in _TITLES:
        data = t.store_patch(path, data)
    return data


def profile_fields(code, cid, member_id):
    t = for_code(code)
    if t is None:
        return {}
    return t.profile_fields(cid, member_id)


def character_name(cid):
    for t in _TITLES:
        n = t.character_name(cid)
        if n:
            return n
    return None


def character(cid):
    for t in _TITLES:
        rec = t.character(cid)
        if rec and rec[0]:
            return rec
    return None


def fetch_pathlen():
    out = {}
    for t in _TITLES:
        out.update(t.fetch_pathlen)
    return out


def resource_init():
    out = {}
    for t in _TITLES:
        out.update(t.resource_init)
    return out


def polpro_spec_files():
    out = []
    for t in _TITLES:
        out.extend(t.polpro_spec_files)
    return out
