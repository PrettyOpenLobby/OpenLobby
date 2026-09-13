"""The presence PUSH must stamp the SAME guid the 2:3 ROW served, or the client
drops the push silently and the online icon never paints.

Mechanism (RE 2026-08-23, both binaries): the friend-list online icon is painted
ONLY by the auth-band presence push -- app.dll's renderer (0x0488efc5) tests slot
+0x08 class (bits 13..15)==1 gated on bit 11, and the 2:3 record-copy (polcore
0x037deeb0) never writes those bits, so NO wire row byte can set online. The SE
capture agrees: the same friend online vs offline flips no field the client
parses. So the push is the sole painter, and the client keys the pushed record to
a friend slot by the guid it stored from that slot's 2:3 record (+0x10). If the
push guid disagrees, the identity compare fails and nothing repaints.

That is what broke on 2026-08-22: POL_FRIEND_GUID_CLIENT defaulted ON
(9bd29667/2ad043b4), flipping the 2:3 ROW to the peer's client_guid, while
_broadcast_presence kept stamping handle_guid -- so presence stopped landing while
the icon row push (which carries the SERVED guid) kept working ("icons render,
presence doesn't"). The account holder's "it worked great until today" is what
isolated the flip.

Pinned here: `responders._push_identity_guid` returns EXACTLY what `_db_friends`
serves at record +0x10, under BOTH values of the flag --

  flag ON  (default): a peer WITH a learned client_guid -> client_guid;
                      a peer WITHOUT one              -> handle_guid (fallback).
  flag OFF (revert):  always handle_guid, matching the row's revert.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="presence-pushid-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    line = "  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                            "  --  " + detail if detail else "")
    enc = sys.stdout.encoding or "ascii"
    print(line.encode(enc, "backslashreplace").decode(enc))
    if not ok:
        FAILS.append(label)


CLIENT_GUID = 0x000000860FB3E2A2       # a real-shape client_guid (LaptopTest2)


def _primary_handle(db, member_id):
    return int(db.execute(
        "SELECT id FROM handle WHERE member_id = ? "
        "ORDER BY is_primary DESC, id ASC LIMIT 1", (int(member_id),)
    ).fetchone()["id"])


def seed():
    """Two handles: one that has told us its client_guid, one that has not.
    `ensure_member` already mints a primary handle, so reuse it (a PlayOnline ID
    is capitals + digits, hence the uppercase names)."""
    db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    m1 = int(accounts.ensure_member(db, "PEERWITHCG")["id"])
    h_cg = _primary_handle(db, m1)
    accounts.learn_client_guid(db, h_cg, CLIENT_GUID)
    m2 = int(accounts.ensure_member(db, "PEERNOCG")["id"])
    h_nocg = _primary_handle(db, m2)
    db.close()
    return h_cg, h_nocg


def row_guid(db, handle_id):
    """What `_db_friends` writes into the 2:3 record +0x10 for this peer -- the
    identity the client stores and later matches a push against. Reproduces the
    _db_friends branch verbatim so the test tracks the real serve, not a copy."""
    guid = accounts.handle_guid(int(handle_id))
    if os.environ.get("POL_FRIEND_GUID_CLIENT", "1") == "1":
        cg = db.execute("SELECT client_guid FROM handle WHERE id = ?",
                        (int(handle_id),)).fetchone()
        if cg and cg["client_guid"]:
            guid = int(cg["client_guid"])
    return guid


h_cg, h_nocg = seed()
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])

# --------------------------------------------------------------------------- #
print("1. flag ON (prod default): push identity == row identity, per peer")
os.environ["POL_FRIEND_GUID_CLIENT"] = "1"

pg = R._push_identity_guid(db, h_cg)
rg = row_guid(db, h_cg)
check(pg == rg == CLIENT_GUID,
      "peer WITH client_guid: push == row == client_guid",
      f"push=0x{pg:X} row=0x{rg:X} want=0x{CLIENT_GUID:X}")

pg = R._push_identity_guid(db, h_nocg)
rg = row_guid(db, h_nocg)
check(pg == rg == accounts.handle_guid(h_nocg),
      "peer WITHOUT client_guid: push == row == handle_guid (fallback)",
      f"push=0x{pg:X} row=0x{rg:X}")

# --------------------------------------------------------------------------- #
print("2. flag OFF (revert): push follows the row back to handle_guid")
os.environ["POL_FRIEND_GUID_CLIENT"] = "0"

for h, who in ((h_cg, "with client_guid"), (h_nocg, "without")):
    pg = R._push_identity_guid(db, h)
    rg = row_guid(db, h)
    check(pg == rg == accounts.handle_guid(h),
          f"peer {who}: push == row == handle_guid when flag OFF",
          f"push=0x{pg:X} row=0x{rg:X}")

# --------------------------------------------------------------------------- #
print("3. the regression itself: OLD push (handle_guid) MISSED the flipped row")
os.environ["POL_FRIEND_GUID_CLIENT"] = "1"
old_push = accounts.handle_guid(h_cg)          # what _broadcast_presence sent pre-fix
rg = row_guid(db, h_cg)                         # the client_guid the row now serves
check(old_push != rg,
      "pre-fix: handle_guid push != client_guid row (the silent-drop cause)",
      f"old_push=0x{old_push:X} row=0x{rg:X}")
check(R._push_identity_guid(db, h_cg) == rg,
      "post-fix: aligned push == row (lands)")

db.close()

print()
if FAILS:
    print("FAILED:", len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
