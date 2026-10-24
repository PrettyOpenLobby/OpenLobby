"""The updater of a paired game: a command its setup program asks the panel to run, in the
game's install folder, when the panel's owner presses Update (docs/installs.md, "Updates";
docs/deployer.md, "Updaters of paired games").

    {"folder": "C:\\Users\\x\\Kart Club",          the install folder as the host sees it
     "command": ["python", "setup.py", "--update"],  first item exactly python or python3
     "timeout_minutes": 60,                          5 to 240, default 60
     "channels": ["stable", "dev"]}                  optional: the update channels it can switch to

"channels", when given, is a list of 1 to 4 names of 1 to 12 lower-case letters, "stable" among
them. The panel then offers the game a channel switch, which runs the same command with
"--channel <name>" appended (with_channel below); a regular update of a game that is not on
"stable" gets "--channel <its channel>" too. A dev access code for a switch goes to the
command in the environment variable DEV_CODE_ENV, from a file docker reads (--env-file), never
in an argument (docs/installs.md, "Update channels").

The panel checks a setup program's block with check() before it stores it, and the deployer
checks the record the panel sends it again with the same function before it runs anything:
both read this one file (the panel through services/deployer on its path, as adminupdates
does for updatecheck).

Standard library only.
"""
import hashlib
import json
import re
import shlex

FIELDS = ("folder", "command", "timeout_minutes", "channels")
PROGRAMS = ("python", "python3")
MAX_ITEMS = 16
MAX_ITEM = 199            # each argument is under 200 characters
MAX_FOLDER = 400
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 5, 240, 60
DESKTOP_MNT = "/run/desktop/mnt/host/"
MAX_CHANNELS = 4
CHANNEL_RE = re.compile(r"^[a-z]{1,12}$")
#: the environment variable that carries a dev access code into a channel switch
DEV_CODE_ENV = "OPENLOBBY_DEV_CODE"

_WIN = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WIN_BAD = re.compile(r'[<>:"|?*]')
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
#: folders a container needs for itself: mounting the install there would hide them
SYSTEM = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys",
          "/usr", "/var/run", "/var/lib/docker")


def _parts(path):
    return [p for p in path.split("/") if p]


def daemon_path(folder):
    """The folder as a Docker daemon on that host sees it: a Windows path is under Docker
    Desktop's /run/desktop/mnt/host/<drive>/, a Linux path is itself. Separators are made
    "/", doubled ones and a trailing one taken out. The folder must have passed check()."""
    m = _WIN.match(folder)
    if m:
        rest = _parts(m.group(2).replace("\\", "/"))
        return DESKTOP_MNT + m.group(1).lower() + ("/" + "/".join(rest) if rest else "")
    return "/" + "/".join(_parts(folder))


def _folder(v):
    if not isinstance(v, str) or not v:
        return None, "updater.folder must be the game's install folder, as the host sees it."
    if len(v) > MAX_FOLDER:
        return None, "updater.folder is longer than %d characters." % MAX_FOLDER
    if _CTRL.search(v):
        return None, "updater.folder has a control character in it."
    if v != v.strip():
        return None, "updater.folder starts or ends with a space."
    m = _WIN.match(v)
    if m:
        rest = m.group(2).replace("\\", "/")
        if any(_WIN_BAD.search(p) for p in _parts(rest)):
            return None, 'updater.folder has a character Windows does not allow in a folder name (<>:"|?*).'
    elif v.startswith("/"):
        rest = v
    else:
        return None, ("updater.folder must be a full path: a Windows path like C:\\Users\\you\\Game, "
                      "or a Linux path starting with /.")
    parts = _parts(rest)
    if any(p in (".", "..") for p in parts):
        return None, "updater.folder must not have . or .. in it."
    if not parts:
        return None, "updater.folder must be the game's install folder, not the root of a drive."
    seen = daemon_path(v)
    if not seen.startswith(DESKTOP_MNT) and any(seen == s or seen.startswith(s + "/") for s in SYSTEM):
        return None, "updater.folder is a system folder (%s); it must be the game's install folder." % seen
    return v, None


def _command(v):
    if not isinstance(v, list) or not v:
        return None, 'updater.command must be a list of arguments, starting with "python" or "python3".'
    if len(v) > MAX_ITEMS:
        return None, "updater.command has more than %d items." % MAX_ITEMS
    if v[0] not in PROGRAMS:
        return None, 'updater.command must start with "python" or "python3".'
    for a in v:
        if not isinstance(a, str):
            return None, "Every item of updater.command must be a string."
        if len(a) > MAX_ITEM:
            return None, "Every item of updater.command must be under 200 characters."
        if "\x00" in a:
            return None, "updater.command has a NUL character in it."
    return list(v), None


def _timeout(v):
    if v is None:
        return TIMEOUT_DEFAULT, None
    if not isinstance(v, int) or isinstance(v, bool) or not TIMEOUT_MIN <= v <= TIMEOUT_MAX:
        return None, "updater.timeout_minutes must be a whole number from %d to %d." % (TIMEOUT_MIN, TIMEOUT_MAX)
    return v, None


def _channels(v):
    if v is None:
        return None, None
    if not isinstance(v, list) or not v or len(v) > MAX_CHANNELS:
        return None, "updater.channels must be a list of 1 to %d channel names." % MAX_CHANNELS
    if not all(isinstance(c, str) and CHANNEL_RE.match(c) for c in v):
        return None, "Every item of updater.channels must be 1 to 12 lower-case letters."
    if len(set(v)) != len(v):
        return None, "updater.channels names a channel twice."
    if "stable" not in v:
        return None, 'updater.channels must include "stable".'
    return list(v), None


def check(block):
    """(record, None) for a block that fits, with timeout_minutes filled in; else (None, a
    sentence saying what does not fit)."""
    if not isinstance(block, dict):
        return None, "updater must be an object: folder, command, timeout_minutes and, optionally, channels."
    extra = sorted(k for k in block if k not in FIELDS)
    if extra:
        return None, "updater has fields the panel does not know: %s." % ", ".join(str(k)[:40] for k in extra[:5])
    folder, bad = _folder(block.get("folder"))
    if bad:
        return None, bad
    command, bad = _command(block.get("command"))
    if bad:
        return None, bad
    minutes, bad = _timeout(block.get("timeout_minutes"))
    if bad:
        return None, bad
    channels, bad = _channels(block.get("channels"))
    if bad:
        return None, bad
    rec = {"folder": folder, "command": command, "timeout_minutes": minutes}
    if channels is not None:
        rec["channels"] = channels
    return rec, None


def with_channel(rec, channel):
    """The command to run for `channel`: the record's own when `channel` is None, else with
    "--channel <name>" appended; None for a channel the record does not declare."""
    if channel is None:
        return list(rec["command"])
    if channel not in (rec.get("channels") or ()):
        return None
    return list(rec["command"]) + ["--channel", channel]


def shown(command):
    """The argument list as one line, each argument shell-quoted (for showing, never run)."""
    return " ".join(shlex.quote(a) for a in command)


def fingerprint(rec):
    """A short id of exactly this folder, command, timeout and channels: the panel's Approve names it, so
    an owner approves what they saw and not a command that changed in between."""
    parts = [rec["folder"], rec["command"], rec["timeout_minutes"]]
    if rec.get("channels"):
        parts.append(rec["channels"])      # left out when absent, so older ids stay the same
    raw = json.dumps(parts, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
