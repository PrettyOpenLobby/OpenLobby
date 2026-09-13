"""Speak as the GM into a GM Call chat room.

The GM chat room is an IRC channel on the auth band that `gmserver --chat-room`
handed the client. authserv has no inbound API, so a GM line is spooled as a FILE
and the session loop delivers it on its next pass -- within `POL_GMCHAT_POLL`
seconds (default 2). See `services/gmchat.py` for the record language.

    python gmsay.py --room "#gmchat001" --say "Hello, how can I help?"
    python gmsay.py --room "#gmchat001" --event S --who Fox
    python gmsay.py --room "#gmchat001" --raw 'T\\x07literally these bytes'

WARNING: **The 'T' and 'U' encoders are derived from the client's PARSERS, not from a
capture** -- no SE GM chat traffic survives. If a line does not render, reach for
`--raw` and iterate: it costs a file write, not a rebuild, and `--raw` accepts
`\\xNN` escapes so any byte is reachable.

Writes into the same `/data` the containers mount, so it works from the host.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

#: On the host the compose `/data` volume lives here; inside a container it is
#: /data. POL_GMCHAT_SPOOL overrides both.
DEFAULT_SPOOL = os.environ.get(
    "POL_GMCHAT_SPOOL", os.path.join(HERE, "..", "data", "gm-chat"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", required=True, help='e.g. "#gmchat001"')
    ap.add_argument("--say", help="chat text -- built as a 'T' record")
    ap.add_argument("--event", help="membership subcode: A/E/G/R/S "
                                    "(suspended/left/joined/resumed/started)")
    ap.add_argument("--who", default="GM", help="the name an --event is about")
    ap.add_argument("--raw", help=r"send these bytes verbatim; \xNN escapes work")
    ap.add_argument("--nick", help="attribute the line to this nick. 'self' means "
                                   "the CLIENT'S OWN nick -- the probe that tells "
                                   "a malformed record apart from a speaker that "
                                   "does not resolve in the member table")
    ap.add_argument("--spool", default=DEFAULT_SPOOL)
    a = ap.parse_args()

    os.environ["POL_GMCHAT_SPOOL"] = a.spool
    import gmchat

    if a.raw is not None:
        rec = a.raw.encode("cp932", "replace").decode("unicode_escape").encode("latin1")
    elif a.event:
        rec = gmchat.encode_event(a.event, a.who)
    elif a.say:
        rec = gmchat.encode_text(a.say)
    else:
        ap.error("one of --say / --event / --raw is required")

    room = a.room.encode()
    if not gmchat.is_gm_room(room):
        print(f"warning: {a.room} does not start with {gmchat.PREFIX.decode()!r}, "
              f"so authserv will not treat it as a GM room", file=sys.stderr)
    gmchat.spool(room, rec, nick=a.nick.encode() if a.nick else None)
    print(f"spooled to {a.room}: {rec!r}"
          + (f" as {a.nick}" if a.nick else ""))
    print("authserv delivers it on its next pass (POL_GMCHAT_POLL, default 2s) "
          "-- watch `docker compose logs -f authsess`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
