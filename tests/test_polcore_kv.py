#!/usr/bin/env python3
"""polcore.kv: the same assertions against MemoryKV and against a real Valkey.

    python tests/test_polcore_kv.py

MemoryKV is always checked. The Valkey half starts a throwaway
valkey/valkey:8-alpine in Docker (or uses POL_TEST_VALKEY_URL) and is skipped,
with a line saying so, when neither is available or the `valkey` package is
missing. POL_TEST_REQUIRE_DB=1 turns that skip into a failure.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

from polcore import kv  # noqa: E402
import pgtest  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def suite(s, other):
    """`other` is a second client on the same store with a different prefix."""
    s.flush()
    other.flush()
    chk("ping", s.ping(), True)

    print(" strings, TTL, SET NX")
    chk("missing key", s.get("nope"), None)
    chk("set", s.set("a", "1"), True)
    chk("get", s.get("a"), "1")
    chk("numbers come back as text", (s.set("n", 42), s.get("n")), (True, "42"))
    chk("ttl with no expiry", s.ttl("a"), -1)
    chk("ttl of a missing key", s.ttl("nope"), -2)
    s.set("t", "x", ttl=0.3)
    chk("ttl set", 0 < s.ttl("t") <= 1, True)
    chk("present before expiry", s.get("t"), "x")
    time.sleep(0.45)
    chk("gone after expiry", (s.get("t"), s.exists("t")), (None, False))
    chk("setnx on a free key", s.setnx("lock", "p1", ttl=0.3), True)
    chk("setnx on a held key", s.setnx("lock", "p2"), False)
    chk("holder unchanged", s.get("lock"), "p1")
    time.sleep(0.45)
    chk("setnx after the holder's TTL ran out", s.setnx("lock", "p2"), True)
    chk("expire on a present key", s.expire("a", 0.3), True)
    chk("expire on a missing key", s.expire("nope", 5), False)
    time.sleep(0.45)
    chk("expire took effect", s.get("a"), None)
    s.set("b", "1", ttl=5)
    s.set("b", "2")
    chk("a plain set clears the TTL", s.ttl("b"), -1)
    chk("incr", (s.incr("c"), s.incr("c", 5)), (1, 6))
    chk("delete counts what existed", s.delete("b", "c", "nope"), 2)
    chk("json round trip",
        (s.set_json("j", {"x": [1, "y"]}), s.get_json("j")), (True, {"x": [1, "y"]}))
    chk("json default", s.get_json("nope", {}), {})

    print(" prefix")
    s.set("shared", "mine")
    chk("another prefix does not see it", other.get("shared"), None)
    chk("keys() strips the prefix", s.keys("sh*"), ["shared"])

    print(" hashes")
    chk("hset field", s.hset("h", "zone", "123"), 1)
    chk("hset mapping (one new)", s.hset("h", mapping={"zone": "124", "status": 2}), 1)
    chk("hget", s.hget("h", "zone"), "124")
    chk("hget missing field", s.hget("h", "nope"), None)
    chk("hgetall", s.hgetall("h"), {"zone": "124", "status": "2"})
    chk("hgetall missing key", s.hgetall("nope"), {})
    chk("hdel", s.hdel("h", "zone", "nope"), 1)
    s.expire("h", 0.3)
    time.sleep(0.45)
    chk("a hash expires as a whole", s.hgetall("h"), {})

    print(" lists (FIFO)")
    chk("push returns the length", s.push("q", "1", "2"), 2)
    chk("push more", s.push("q", 3), 3)
    chk("llen", s.llen("q"), 3)
    chk("pop in order", [s.pop("q"), s.pop("q"), s.pop("q")], ["1", "2", "3"])
    chk("pop empty, no wait", s.pop("q"), None)
    t0 = time.monotonic()
    chk("pop empty with timeout", s.pop("q", timeout=0.5), None)
    chk("...and it waited", time.monotonic() - t0 >= 0.4, True)
    threading.Timer(0.2, lambda: s.push("q", "late")).start()
    t0 = time.monotonic()
    chk("blocking pop wakes on a push", s.pop("q", timeout=3), "late")
    chk("...before the timeout", time.monotonic() - t0 < 2.5, True)

    print(" publish / subscribe")
    got = []
    sub = s.subscribe(["presence", "rooms"], lambda ch, msg: got.append((ch, msg)))
    # A Valkey SUBSCRIBE is registered asynchronously; wait until it counts.
    chk("subscriber registered",
        wait_for(lambda: s.publish("presence", "hello") >= 1), True)
    s.publish("rooms", 7)
    s.publish("elsewhere", "not for us")
    chk("messages arrive with unprefixed channel names",
        wait_for(lambda: ("rooms", "7") in got) and ("presence", "hello") in got, True)
    chk("other channels are not delivered",
        [m for m in got if m[0] not in ("presence", "rooms")], [])
    print("  (the ZeroDivisionError line below is expected)")
    boom = s.subscribe("err", lambda ch, msg: 1 / 0)
    s.publish("err", "x")
    time.sleep(0.2)
    s.publish("rooms", "after-error")
    chk("a raising callback does not stop others",
        wait_for(lambda: ("rooms", "after-error") in got), True)
    boom.close()
    sub.close()
    time.sleep(0.3)
    got.clear()
    chk("after close, publish reaches nobody", s.publish("presence", "gone"), 0)
    time.sleep(0.2)
    chk("...and nothing is delivered", got, [])
    s.flush()
    other.flush()


print("MemoryKV")
mem = kv.MemoryKV(prefix="test:")
suite(mem, kv.MemoryKV(prefix="other:"))

print("MemoryKV is thread-safe")
m = kv.MemoryKV(prefix="t:")
ths = [threading.Thread(target=lambda: [m.incr("c") for _ in range(2000)])
       for _ in range(8)]
for t in ths:
    t.start()
for t in ths:
    t.join()
chk("8 x 2000 increments", m.get("c"), "16000")
popped, lock = [], threading.Lock()


def consumer():
    while True:
        v = m.pop("jobs", timeout=0.5)
        if v is None:
            return
        with lock:
            popped.append(v)


cs = [threading.Thread(target=consumer) for _ in range(4)]
for t in cs:
    t.start()
for i in range(500):
    m.push("jobs", i)
for t in cs:
    t.join()
chk("500 jobs, each popped exactly once", sorted(map(int, popped)) == list(range(500)), True)

print("default() follows POL_VALKEY_URL")
os.environ.pop("POL_VALKEY_URL", None)
kv.reset()
chk("unset -> memory", kv.default().backend, "memory")
chk("module functions reach it", (kv.set("x", "1"), kv.get("x")), (True, "1"))
kv.reset()

print("Valkey")
url = os.environ.get("POL_TEST_VALKEY_URL", "").strip()
why = None
try:
    import valkey  # noqa: F401
except ImportError:
    why = "the valkey package is not installed"
if not why and not url:
    if pgtest.docker_available():
        cid, port = pgtest.container("valkey/valkey:8-alpine", 6379,
                                     cmd=["valkey-server", "--save", "",
                                          "--appendonly", "no"])
        url = "valkey://127.0.0.1:%d/0" % port
    else:
        why = "no Docker daemon and POL_TEST_VALKEY_URL is unset"
if why:
    if os.environ.get("POL_TEST_REQUIRE_DB") == "1":
        print("  FAIL: Valkey half not run (%s) and POL_TEST_REQUIRE_DB=1" % why)
        bad += 1
    else:
        print("  SKIP: %s" % why)
else:
    vk = kv.ValkeyKV(url, prefix="test:")
    deadline = time.monotonic() + 30
    while True:
        try:
            vk.ping()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    suite(vk, kv.ValkeyKV(url, prefix="other:"))
    os.environ["POL_VALKEY_URL"] = url
    kv.reset()
    chk("set -> valkey", kv.default().backend, "valkey")
    kv.reset()
    os.environ.pop("POL_VALKEY_URL", None)

print("\n%s" % ("all passed" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
