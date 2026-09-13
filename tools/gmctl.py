"""The GM desk from a shell: what callers are being told, and change it.

The admin panel's GM tab is the comfortable way to do this; the file it writes is
`<ticket dir>/gm-control.json` and this is the same thing without a browser --
useful over ssh, from a script, and when the panel itself is the thing you are
trying to rule out.

    python gmctl.py                       what a caller is told right now
    python gmctl.py on-duty               offer Start AND Join
    python gmctl.py off-duty              withdraw Join
    python gmctl.py clear-duty            hand the decision back to compose
    python gmctl.py pin --queue 3         say there are 3 people ahead
    python gmctl.py pin --flags 0x40      set the flag word outright
    python gmctl.py unpin                 drop both pins
    python gmctl.py log --room "#gmchat001" [-n 50]     the room's transcript

WARNING: WHAT THIS DOES AND DOES NOT CONTROL. The queue count and the two screen flags
are measured -- queue is `0x801` body +0x02, Join is flag 0x40, Start is 0x20,
all confirmed against a live client on 2026-08-16 -- so these really are what the
caller sees. `gmd` re-reads the file on each caller's next status poll, so
nothing here needs a restart, and nothing here takes effect the instant you press
it either.

To SPEAK in the room, use `gmsay.py`; to find out which piece of the path is
broken, `gmdoctor.py`.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

#: On the host the compose `/data` volume lives beside the repo; inside a
#: container it is /data. Same resolution `gmsay.py` uses.
os.environ.setdefault("POL_GMD_TICKET_DIR",
                      os.path.join(HERE, "..", "data", "gm-calls"))
os.environ.setdefault("POL_GMCHAT_SPOOL",
                      os.path.join(HERE, "..", "data", "gm-chat"))

import gmchat  # noqa: E402
import gmd  # noqa: E402

#: Matches admin.py's own lease. A claim to be at the desk EXPIRES, because an
#: admin who walks away is the normal case and a sticky "a GM is available"
#: invites a caller into an empty room.
DUTY_TTL = int(os.environ.get("POL_ADMIN_GM_DUTY_TTL", "180"))


def show():
    ctl = gmd.read_control()
    flags, why = gmd.effective_flags(ctl)
    print(f"control file : {gmd.CONTROL_PATH}"
          + ("" if os.path.exists(gmd.CONTROL_PATH) else "  (absent)"))
    state = ("on duty" if gmd.on_duty(ctl)
             else "nobody has claimed the desk" if ctl.get("duty") is None
             else "off duty")
    print(f"status       : {state}"
          + (f", until {time.strftime('%H:%M:%S', time.localtime(ctl['on_duty_until']))}"
             if gmd.on_duty(ctl) else ""))
    print(f"flags        : {flags:#x}  ({why})")
    print(f"               Join  {'YES' if flags & 0x40 else 'no'}   "
          f"Start {'YES' if flags & 0x20 else 'no'}")
    if ctl.get("queue") is not None:
        print(f"queue        : {ctl['queue']}  (pinned)")
    else:
        print(f"queue        : live"
              + (f", or POL_GMD_QUEUE={gmd.QUEUE_OVERRIDE}" if gmd.QUEUE_OVERRIDE else ""))
    if ctl.get("by"):
        print(f"last set by  : {ctl['by']}")

    # What gmd actually served, as opposed to what it would serve now. They
    # differ until the next poll, and that gap is the usual reason a change
    # "did not work".
    try:
        with open(gmd.SERVING_PATH, encoding="utf-8") as f:
            sv = json.load(f)
        when = time.strftime("%H:%M:%S", time.localtime(sv.get("at", 0)))
        print(f"last served  : flags {sv.get('flags', 0):#x}, queue "
              f"{sv.get('queue')} at {when}"
              + ("   <- differs from the above; the caller has not polled since"
                 if sv.get("flags") != flags else ""))
        print(f"room         : {sv.get('room') or '(none)'}")
    except (OSError, ValueError):
        print("last served  : nothing has polled gmd yet")

    rooms = gmchat.rooms()
    if rooms:
        print("rooms        : " + ", ".join(
            f"{r} ({gmchat.pending(r.encode())} undelivered)" for r in rooms))
    return 0


def save(ctl, what):
    ctl["by"] = os.environ.get("USER") or os.environ.get("USERNAME") or "gmctl"
    ctl["at"] = time.time()
    gmd.write_control(ctl)
    print(f"{what} -- callers see it on their next status poll")
    return show()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("show", help="what a caller is told right now (the default)")
    sub.add_parser("on-duty", help="a GM is here: offer Start and Join")
    sub.add_parser("off-duty", help="withdraw Join")
    sub.add_parser("clear-duty",
                   help="say nothing either way, so POL_GMD_STATUS_FLAGS decides")
    p = sub.add_parser("pin", help="force the queue count and/or the flag word")
    p.add_argument("--queue", type=lambda v: int(v, 0))
    p.add_argument("--flags", type=lambda v: int(v, 0))
    sub.add_parser("unpin", help="drop both pins")
    p = sub.add_parser("log", help="a room's transcript, oldest first")
    p.add_argument("--room", help="default = the room gmd is handing out")
    p.add_argument("-n", type=int, default=50)
    p.add_argument("--hex", action="store_true", help="show the raw bytes too")
    a = ap.parse_args()

    if a.cmd in (None, "show"):
        return show()
    if a.cmd == "log":
        room = a.room
        if not room:
            try:
                with open(gmd.SERVING_PATH, encoding="utf-8") as f:
                    room = json.load(f).get("room")
            except (OSError, ValueError):
                room = None
            rooms = gmchat.rooms()
            room = room or (rooms[0] if rooms else None)
        if not room:
            print("no room -- is gmd running, and has anyone been in one?",
                  file=sys.stderr)
            return 1
        rows = gmchat.transcript(room.encode(), a.n)
        if not rows:
            print(f"{room}: nothing recorded yet")
            return 0
        for r in rows:
            when = time.strftime("%H:%M:%S", time.localtime(r.get("at", 0)))
            arrow = "<-" if r.get("dir") == "in" else "->"
            print(f"{when} {arrow} {r.get('nick', '?'):<12} {r.get('text', '')}")
            if a.hex:
                print(f"{'':>11}    {r.get('raw', '')}")
        return 0

    ctl = gmd.read_control()
    if a.cmd == "on-duty":
        ctl["duty"], ctl["on_duty_until"] = True, time.time() + DUTY_TTL
        # SAY THE EXPIRY OUT LOUD. Unlike the panel, nothing renews this: a shell
        # is not a heartbeat, so an admin who runs this and walks off must
        # know it lapses rather than discover it later from a caller.
        print(f"on duty for {DUTY_TTL}s -- nothing renews this from a shell; "
              f"re-run it, or keep the admin panel's GM tab open")
        return save(ctl, "on duty")
    if a.cmd == "off-duty":
        ctl["duty"], ctl["on_duty_until"] = False, None
        return save(ctl, "off duty")
    if a.cmd == "clear-duty":
        ctl["duty"], ctl["on_duty_until"] = None, None
        return save(ctl, "duty cleared")
    if a.cmd == "pin":
        if a.queue is None and a.flags is None:
            ap.error("pin needs --queue and/or --flags")
        if a.queue is not None:
            if not 0 <= a.queue <= 0xFFFF:
                ap.error("--queue must be 0-65535")
            ctl["queue"] = a.queue
        if a.flags is not None:
            if not 0 <= a.flags <= 0xFFFFFFFF:
                ap.error("--flags must fit in 32 bits")
            ctl["flags"] = a.flags
        return save(ctl, "pinned")
    if a.cmd == "unpin":
        ctl["queue"] = ctl["flags"] = None
        return save(ctl, "pins cleared")
    return show()


if __name__ == "__main__":
    sys.exit(main())
